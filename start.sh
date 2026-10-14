#!/bin/bash
set -e

# 清理锁
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99

# 1. 起 Xvfb 并等它就绪，不是 sleep 1
Xvfb :99 -screen 0 1280x800x24 -ac -nolisten tcp &
for i in {1..30}; do
  xdpyinfo -display :99 >/dev/null 2>&1 && break
  sleep 0.2
done

# 2. 起 VNC + noVNC 后台
x11vnc -display :99 -forever -shared -nopw -rfbport 5900 -quiet -bg -o /tmp/x11vnc.log
NOVNC_PATH=$(ls -d /usr/share/novnc /usr/share/webapps/novnc 2>/dev/null | head -n1)
websockify --web $NOVNC_PATH 6080 localhost:5900 -D --log-file /tmp/websockify.log

echo "=== 环境就绪 ==="
echo "noVNC: http://localhost:6080"
echo "执行: $@"
echo "================"

# 3. 执行用户传进来的命令
exec "$@"