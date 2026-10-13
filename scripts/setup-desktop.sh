#!/usr/bin/env bash
# Headless desktop: Xvfb + Openbox + TigerVNC + noVNC + Cloudflared + dummy audio
set -eux

if command -v apt-get >/dev/null 2>&1; then
  sudo apt-get install -y --no-install-recommends \
    xvfb x11vnc tigervnc-standalone-server tigervnc-common \
    openbox xterm novnc websockify net-tools \
    pulseaudio alsa-utils dbus-x11 python3-numpy || true
  # novnc path fix (Ubuntu places it at /usr/share/novnc)
  if [ ! -d /opt/novnc ] && [ -d /usr/share/novnc ]; then sudo ln -sfn /usr/share/novnc /opt/novnc; fi
elif command -v dnf >/dev/null 2>&1; then
  sudo dnf install -y tigervnc-server openbox xterm novnc websockify pulseaudio curl wget || true
fi

# Cloudflared (tunnel to expose 8080 launcher UI + 6080 noVNC in browser)
if ! command -v cloudflared >/dev/null 2>&1; then
  echo "[desktop] installing cloudflared..."
  ARCH=$(uname -m); [ "$ARCH" = "x86_64" ] && CFARCH=amd64 || CFARCH=arm64
  curl -fsSL -o /tmp/cloudflared "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${CFARCH}"
  sudo install -m 755 /tmp/cloudflared /usr/local/bin/cloudflared
fi

# noVNC fallback download if package missing
if [ ! -d /opt/novnc ] && [ ! -d /usr/share/novnc ]; then
  echo "[desktop] fetching noVNC..."
  sudo mkdir -p /opt
  curl -fsSL -o /tmp/novnc.tar.gz "https://github.com/novnc/noVNC/archive/refs/tags/v1.5.0.tar.gz"
  sudo tar xzf /tmp/novnc.tar.gz -C /opt && sudo mv /opt/noVNC-1.5.0 /opt/novnc
  curl -fsSL -o /tmp/ws.tar.gz "https://github.com/novnc/websockify/archive/refs/tags/v0.12.0.tar.gz" || true
fi

echo "[desktop] done."
