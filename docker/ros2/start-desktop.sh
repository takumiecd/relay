#!/usr/bin/env bash
# Display infrastructure only; start ROS nodes explicitly with ros2 launch.
set -euo pipefail
pids=()
cleanup() {
  trap - EXIT INT TERM
  for pid in "${pids[@]}"; do kill "$pid" 2>/dev/null || true; done
  wait || true
}
trap cleanup EXIT INT TERM

Xvfb "$DISPLAY" -screen 0 1440x900x24 -nolisten tcp >/tmp/relay-xvfb.log 2>&1 &
pids+=("$!")
display_ready=false
for attempt in {1..50}; do
  if xdpyinfo -display "$DISPLAY" >/dev/null 2>&1; then display_ready=true; break; fi
  sleep .1
done
if [ "$display_ready" != true ]; then cat /tmp/relay-xvfb.log >&2; exit 1; fi

openbox >/tmp/relay-openbox.log 2>&1 &
pids+=("$!")
x11vnc -display "$DISPLAY" -localhost -rfbport 5900 -forever -shared -nopw \
  >/tmp/relay-x11vnc.log 2>&1 &
pids+=("$!")
# Host publishing must be 127.0.0.1 only. No remote/public VNC endpoint.
websockify --web=/usr/share/novnc 0.0.0.0:6080 127.0.0.1:5900 \
  >/tmp/relay-websockify.log 2>&1 &
pids+=("$!")
echo 'ROS 2 display ready; start reference_view.launch.py in another terminal.'
wait -n "${pids[@]}"
