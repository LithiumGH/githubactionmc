#!/usr/bin/env bash
# start.sh — launched by the GitHub Action (or locally). Boots Xvfb, VNC,
# noVNC, tunnels, launcher UI, optionally auto-installs + launches the game.
set -e
: "${MC_VERSION:=1.21}" "${LOADER:=fabric}" "${LOADER_VERSION:=latest}"
: "${USERNAME:=Player}" "${RAM:=4096M}" "${RESOLUTION:=1280x720}"
: "${MODPACK_URL:=}" "${MODRINTH_MODS:=sodium,ferrite-core}" "${AUTO_LAUNCH:=false}"
export MC_DIR="${MC_DIR:-$HOME/minecraft}"
mkdir -p "$MC_DIR/logs"

if [ -f /etc/fakegpu/env ]; then set -a; . /etc/fakegpu/env; set +a; fi
export DISPLAY=:99
mkdir -p /tmp/runtime-"$USER"; export XDG_RUNTIME_DIR=/tmp/runtime-"$USER"

# 1. Xvfb + window manager
if ! pgrep -f "Xvfb :99" >/dev/null; then
  echo "[start] Xvfb $RESOLUTION..."
  W=$(echo "$RESOLUTION" | cut -dx -f1); H=$(echo "$RESOLUTION" | cut -dx -f2)
  Xvfb :99 -screen 0 "${W}x${H}x24" +extension GLX +render -noreset &
  sleep 3
fi
(openbox --sm-disable >/tmp/openbox.log 2>&1 &) || true
(pulseaudio --start --exit-idle-time=-1 >/dev/null 2>&1) || true

# 2. VNC + noVNC
if ! pgrep -f "x11vnc.*:99" >/dev/null; then
  echo "[start] x11vnc..."
  x11vnc -display :99 -forever -shared -nopw -rfbport 5900 -bg -o /tmp/x11vnc.log || \
    (vncserver :99 -geometry "$RESOLUTION" -depth 24 || true)
fi
NOVNC=$(test -d /opt/novnc && echo /opt/novnc || echo /usr/share/novnc)
if ! pgrep -f "websockify.*6080" >/dev/null; then
  echo "[start] noVNC on :6080..."
  "$NOVNC/utils/novnc_proxy" --vnc localhost:5900 --listen 6080 >/tmp/novnc.log 2>&1 &
  sleep 2
fi

# 3. Launcher UI
pip install -q -r "$(dirname "$0")/../launcher/requirements.txt" 2>&1 | tail -1 || true
if ! pgrep -f "launcher/server.py" >/dev/null; then
  echo "[start] launcher UI on :8080..."
  python3 "$(dirname "$0")/../launcher/server.py" --port 8080 >"$MC_DIR/logs/launcher.log" 2>&1 &
  sleep 3
fi

# 4. Tunnels (browser access without port-forwarding)
open_tunnel() { # $1 port $2 name
  cloudflared tunnel --url "http://localhost:$1" --no-autoupdate >"/tmp/cf-$2.log" 2>&1 &
  for i in $(seq 1 30); do
    URL=$(grep -Eo 'https://[a-z0-9-]+\.trycloudflare\.com' "/tmp/cf-$2.log" | head -1)
    [ -n "$URL" ] && { echo "$URL"; return; }
    sleep 2
  done
  echo "(tunnel $2 failed — see /tmp/cf-$2.log)"
}
echo "[start] opening tunnels..."
LAUNCHER_URL=$(open_tunnel 8080 launcher || true)
VNC_URL=$(open_tunnel 6080 vnc || true)
export LAUNCHER_URL VNC_URL
echo "================================================================"
echo " Launcher UI (install mods, hit Launch): $LAUNCHER_URL"
echo " Game desktop in browser (noVNC):        $VNC_URL/vnc.html"
echo "================================================================"

# 5. Optional auto-install + launch (non-interactive from workflow inputs)
if [ "$AUTO_LAUNCH" = "true" ]; then
  echo "[start] auto install $MC_VERSION/$LOADER..."
  python3 - <<EOF
import sys; sys.path.insert(0, "launcher")
import mc_core
r = mc_core.install_loader("$MC_VERSION", "$LOADER", "$LOADER_VERSION", progress=print)
print("installed:", r.get("version"), r.get("loader"))
for slug in "$MODRINTH_MODS".split(","):
    slug=slug.strip()
    if slug:
        try: mc_core.modrinth_install(slug, r["version"], "$LOADER", progress=print)
        except Exception as e: print("mod skip", slug, e)
EOF
  if [ -n "$MODPACK_URL" ]; then
    python3 -c "import sys; sys.path.insert(0,'launcher'); import mc_core; print(mc_core.install_mrpack('$MODPACK_URL'))"
  fi
  python3 -c "import sys; sys.path.insert(0,'launcher'); import mc_core; print(mc_core.launch('$MC_VERSION','$LOADER','$USERNAME','$RAM','$RESOLUTION'))"
  echo "[start] game launched — watch it at $VNC_URL/vnc.html"
fi

# 6. Keepalive (Actions kills idle jobs; heartbeat to $GITHUB_STEP_SUMMARY too)
echo "[start] holding job (Ctrl-C to stop locally)..."
i=0
while true; do
  sleep 300
  i=$((i+5))
  echo "[keepalive] ${i}m elapsed. Launcher: $LAUNCHER_URL | Game: $VNC_URL/vnc.html | $(date -u)"
  pgrep -f "Xvfb :99" >/dev/null || echo "[warn] Xvfb died"
done
