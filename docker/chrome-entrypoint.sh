#!/usr/bin/env bash
# Boots the virtual display + VNC stack, then hands off to the Chrome
# supervisor which launches one Chrome per profile.
set -e

GEOM="${SCREEN_GEOMETRY:-1600x900x24}"
VNC_PORT="${VNC_PORT:-5900}"
NOVNC_PORT="${NOVNC_PORT:-6080}"

echo "[entrypoint] starting Xvfb on :99 ($GEOM)"
Xvfb :99 -screen 0 "$GEOM" -ac +extension RANDR >/tmp/xvfb.log 2>&1 &
XVFB_PID=$!

# Wait for Xvfb to be fully ready before launching anything that needs DISPLAY.
echo "[entrypoint] waiting for Xvfb…"
for i in $(seq 1 20); do
  if xdpyinfo -display :99 >/dev/null 2>&1; then
    echo "[entrypoint] Xvfb ready after ${i}s"
    break
  fi
  sleep 1
done
export DISPLAY=:99

echo "[entrypoint] starting fluxbox (window manager)"
fluxbox >/tmp/fluxbox.log 2>&1 &
sleep 2

echo "[entrypoint] starting x11vnc on :$VNC_PORT"
x11vnc -display :99 -forever -shared -nopw -rfbport "$VNC_PORT" \
       -quiet >/tmp/x11vnc.log 2>&1 &
sleep 1

echo "[entrypoint] starting noVNC on :$NOVNC_PORT  (open http://host:$NOVNC_PORT)"
websockify --web=/usr/share/novnc "$NOVNC_PORT" "localhost:$VNC_PORT" \
       >/tmp/novnc.log 2>&1 &
sleep 1

echo "[entrypoint] launching Chrome supervisor"
exec python /app/chrome_supervisor.py
