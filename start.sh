#!/bin/bash
set -e
unset PORT
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99

# 关键，全部重定向到 /dev/null
Xvfb :99 -screen 0 1280x800x24 >/dev/null 2>&1 &
x11vnc -display :99 -forever -shared -nopw -rfbport 5900 -quiet >/dev/null 2>&1 &
websockify --web /usr/share/novnc 6080 localhost:5900 -D --log-file /tmp/websockify.log >/dev/null 2>&1

exec "$@"