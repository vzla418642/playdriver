#!/bin/bash
# 启动虚拟桌面与 VNC 服务，X 就绪后立即执行传入的命令。
# 约定：成功路径完全静默；仅在启动失败时向 stderr 输出 error。
set -euo pipefail

unset PORT

log_error() {
    printf '[start.sh] error: %s\n' "$*" >&2
}

port_open() {
    (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null
}

# 等待 X socket 出现；同时确认 Xvfb 进程没提前退出
wait_x_socket() {
    local sock=$1 pid=$2 timeout=${3:-10}
    local end=$((SECONDS + timeout))
    while [ "$SECONDS" -lt "$end" ]; do
        if [ -S "$sock" ]; then
            return 0
        fi
        if ! kill -0 "$pid" 2>/dev/null; then
            return 1
        fi
        sleep 0.1
    done
    return 1
}

# VNC 栈不阻塞业务命令，只在超时后补一行 error
check_port_async() {
    local port=$1 name=$2 timeout=$3 logfile=${4:-}
    local end=$((SECONDS + timeout))
    (
        while [ "$SECONDS" -lt "$end" ]; do
            if port_open "$port"; then
                exit 0
            fi
            sleep 0.2
        done
        printf '[start.sh] error: %s 未在 %s 秒内就绪（端口 %s）\n' \
            "$name" "$timeout" "$port" >&2
        if [ -n "$logfile" ] && [ -s "$logfile" ]; then
            tail -n 20 "$logfile" >&2
        fi
    ) &
}

rm -f /tmp/.X99-lock /tmp/.X11-unix/X99

# Xvfb 与 websockify 互不依赖，同时拉起
Xvfb :99 -screen 0 1280x800x24 >/dev/null 2>&1 &
xvfb_pid=$!

websockify --web /usr/share/novnc 6080 localhost:5900 >/tmp/websockify.log 2>&1 &

# 业务脚本只依赖 X，这是唯一需要阻塞的环节
if ! wait_x_socket /tmp/.X11-unix/X99 "$xvfb_pid" 10; then
    log_error "Xvfb 启动失败，:99 在 10 秒内未就绪"
    exit 1
fi

x11vnc -display :99 -forever -shared -nopw -rfbport 5900 -quiet >/dev/null 2>&1 &

check_port_async 5900 x11vnc 10
check_port_async 6080 websockify 10 /tmp/websockify.log

exec "$@"
