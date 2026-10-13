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

# 2. VNC + noVNC (fail loudly — a dead VNC means a dead tunnel later)
if ! pgrep -f "x11vnc.*:99" >/dev/null; then
  echo "[start] x11vnc..."
  x11vnc -display :99 -forever -shared -nopw -rfbport 5900 -bg -o /tmp/x11vnc.log
  sleep 2
fi
for i in $(seq 1 15); do
  (ss -ltn 2>/dev/null || netstat -ltn 2>/dev/null) | grep -q ":5900 " && break
  echo "[start] waiting for VNC :5900... ($i)"
  sleep 2
  if [ "$i" = 15 ]; then
    echo "[start] FATAL: nothing listening on 5900. x11vnc log:"; tail -30 /tmp/x11vnc.log || true
    echo "[start] (Not starting conflicting vncserver on :99 — Xvfb owns it.)"
    exit 1
  fi
done
NOVNC=$(test -d /opt/novnc && echo /opt/novnc || echo /usr/share/novnc)
if ! (ss -ltn 2>/dev/null || netstat -ltn 2>/dev/null) | grep -q ":6080 "; then
  echo "[start] noVNC on :6080..."
  if [ -x "$NOVNC/utils/novnc_proxy" ]; then
    "$NOVNC/utils/novnc_proxy" --vnc localhost:5900 --listen 6080 >/tmp/novnc.log 2>&1 &
  else
    websockify 6080 localhost:5900 --web "$NOVNC" >/tmp/novnc.log 2>&1 &
  fi
  sleep 3
fi
for i in $(seq 1 15); do
  curl -sf -o /dev/null "http://localhost:6080/vnc.html" && break
  echo "[start] waiting for noVNC :6080... ($i)"
  sleep 2
  if [ "$i" = 15 ]; then
    echo "[start] FATAL: noVNC not responding. log:"; tail -30 /tmp/novnc.log || true
    exit 1
  fi
done

# 3. Launcher UI (+ health check so we never tunnel a dead port)
pip install -q -r "$(dirname "$0")/../launcher/requirements.txt" 2>&1 | tail -1 || true
if ! pgrep -f "launcher/server.py" >/dev/null; then
  echo "[start] launcher UI on :8080..."
  python3 "$(dirname "$0")/../launcher/server.py" --port 8080 >"$MC_DIR/logs/launcher.log" 2>&1 &
  sleep 3
fi
for i in $(seq 1 15); do
  curl -sf -o /dev/null "http://localhost:8080/api/status" && break
  echo "[start] waiting for launcher :8080... ($i)"
  sleep 2
  if [ "$i" = 15 ]; then
    echo "[start] FATAL: launcher UI not responding. log:"; tail -30 "$MC_DIR/logs/launcher.log" || true
    exit 1
  fi
done

summarize() { # $1 text — also pin to the Actions job summary page
  echo "$1"
  if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then echo "$1" >>"$GITHUB_STEP_SUMMARY"; fi
}

# 4. Tunnels (browser access without port-forwarding)
# NOTE: must NOT use $(...) capture — background tunnel processes started
# inside a command substitution can be killed when the subshell exits.
open_tunnel() { # $1 port $2 name — sets ${2}_URL on success, returns 0/1
  local port="$1" name="$2" log="/tmp/cf-$2.log" url=""
  echo "[tunnel:$name] starting cloudflared for :$port ..."
  rm -f "$log"
  cloudflared tunnel --url "http://localhost:$port" --no-autoupdate >"$log" 2>&1 &
  echo "$!" >"/tmp/cf-$name.pid"
  for i in $(seq 1 45); do
    url=$(grep -Eo 'https://[A-Za-z0-9-]+\.trycloudflare\.com' "$log" 2>/dev/null | head -1)
    [ -n "$url" ] && break
    if ! kill -0 "$(cat "/tmp/cf-$name.pid")" 2>/dev/null; then
      echo "[tunnel:$name] cloudflared died early. log:"; tail -30 "$log" || true
      break
    fi
    sleep 2
  done
  if [ -z "$url" ]; then
    echo "[tunnel:$name] FAILED (cloudflared log tail):"; tail -30 "$log" || true
    if grep -qi "429\|rate.limit\|too many" "$log" 2>/dev/null; then
      echo "[tunnel:$name] Cloudflare rate-limited this runner IP (common on shared Actions IPs)."
    fi
    return 1
  fi
  eval "${name}_URL=\"$url\""
  echo "[tunnel:$name] $url -> :$port"
  return 0
}
fallback_tunnel() { # $1 port $2 name — localtunnel.me via npx
  local port="$1" name="$2" log="/tmp/lt-$2.log" url=""
  command -v npx >/dev/null 2>&1 || { echo "[tunnel:$name] no npx for fallback"; return 1; }
  echo "[tunnel:$name] trying localtunnel fallback for :$port ..."
  rm -f "$log"
  npx -y localtunnel --port "$port" >"$log" 2>&1 &
  echo "$!" >"/tmp/lt-$name.pid"
  for i in $(seq 1 30); do
    url=$(grep -Eo 'https://[A-Za-z0-9.-]+\.loca\.lt' "$log" 2>/dev/null | head -1)
    [ -n "$url" ] && break
    sleep 2
  done
  [ -n "$url" ] || { echo "[tunnel:$name] localtunnel failed:"; tail -20 "$log" || true; return 1; }
  eval "${name}_URL=\"$url\""
  echo "[tunnel:$name] $url -> :$port (localtunnel)"
  return 0
}
echo "[start] opening tunnels..."
LAUNCHER_URL=""; VNC_URL=""
open_tunnel 8080 launcher || fallback_tunnel 8080 launcher || true
open_tunnel 6080 vnc || fallback_tunnel 6080 vnc || true
export LAUNCHER_URL VNC_URL
if [ -z "$LAUNCHER_URL" ]; then
  summarize "⚠️ Launcher tunnel FAILED — game may still run; use 'gh run view --log' and check /tmp/cf-launcher.log. Local check: curl http://localhost:8080/api/status"
fi
{
echo "================================================================"
echo " Launcher UI (install mods, hit Launch): ${LAUNCHER_URL:-FAILED — see /tmp/cf-launcher.log}"
echo " Game desktop in browser (noVNC):        ${VNC_URL:-FAILED — see /tmp/cf-vnc.log}/vnc.html"
echo " NOTE: noVNC needs the '/vnc.html' suffix — the bare domain shows an error."
echo " If a tunnel shows a Cloudflare 1033/error page, the tunnel is up but the"
echo " local port died — check the keepalive warnings below."
echo "================================================================"
} | tee /tmp/urls.txt
summarize "$(cat /tmp/urls.txt)"

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
  echo "[keepalive] ${i}m elapsed. Launcher: ${LAUNCHER_URL:-dead} | Game: ${VNC_URL:-dead}/vnc.html | $(date -u)"
  pgrep -f "Xvfb :99" >/dev/null || echo "[warn] Xvfb died"
  curl -sf -o /dev/null http://localhost:8080/api/status || echo "[warn] launcher :8080 down — tunnel will 1033. See $MC_DIR/logs/launcher.log"
  curl -sf -o /dev/null http://localhost:6080/vnc.html || echo "[warn] noVNC :6080 down — tunnel will 1033. See /tmp/novnc.log"
  for t in launcher vnc; do
    if [ -f "/tmp/cf-$t.pid" ] && ! kill -0 "$(cat "/tmp/cf-$t.pid")" 2>/dev/null; then
      echo "[warn] cloudflared ($t) died — tail:"; tail -10 "/tmp/cf-$t.log" || true
    fi
  done
done
