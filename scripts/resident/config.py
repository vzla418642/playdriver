"""常驻模式的配置。

只有这里读 os.getenv。端口、代理、Chrome 路径、过挑战超时都**复用 bypass 的
配置**——两边必须是同一个出口，否则在常驻浏览器里手动过完拿到的 cf_clearance，
拿到 bypass 的转发路径上去用会因为出口 IP 不同直接失效。
"""

from __future__ import annotations

import os

# profile 落盘目录。挂 volume 到宿主机就能跨容器保留登录态；不挂则是容器内
# 临时目录，容器删了 cookie 也就没了。
PROFILE_DIR = os.getenv("RESIDENT_PROFILE", "/data/profile")

# 挑战检测间隔（秒）。太小白烧 CPU，太大会让用户在拦截页上多停一会儿
DETECT_INTERVAL = float(os.getenv("RESIDENT_DETECT", "2"))

# 单次检测（page.evaluate）超时（秒）。页面正在导航、或弹了 JS 对话框时会卡住
DETECT_TIMEOUT = 5.0

# 同一个地址过验证失败后，多久内不再重试（秒）。防软封时反复撞同一个页
RETRY_COOLDOWN = float(os.getenv("RESIDENT_RETRY_COOLDOWN", "30"))

# 浏览器探活间隔（秒）。用户关掉窗口后，最多这么久就会重开
HEALTH_INTERVAL = 1.0

# 重开浏览器的基础等待（秒），连续失败按 2 的幂退避，封顶 RELAUNCH_MAX_DELAY
RELAUNCH_DELAY = 1.0
RELAUNCH_MAX_DELAY = 15.0

# 连续重开失败多少次后放弃。放弃后容器还在、HTTP 服务还在，但浏览器不会再起来
MAX_LAUNCH_FAILURES = 5

# 窗口尺寸，与 start.sh 里 Xvfb 的 1280x800 对齐
WINDOW_WIDTH = 1280
WINDOW_HEIGHT = 800

# CDP 调试端口。**默认开启**，设 0 关闭。
# 开着的目的是让外部脚本接管这个浏览器做自动化点击流程 —— 脚本点出来的挑战页，
# Watcher 会照常接手过掉，两者是配合关系。
#
# 注意 CDP 没有任何鉴权：能连上端口就等于拿到了这个浏览器的一切（所有站点的
# cookie / localStorage / 任意 JS 执行）。只在受信网络里开。
#
# **监听地址是 127.0.0.1，不可改**：Chrome 136+ 起会忽略 --remote-debugging-address，
# 一律只绑回环（实测 Chrome 144，绑 0.0.0.0 和绑具体网卡 IP 都还是 127.0.0.1）。
# 所以容器里想从宿主机/局域网连，只能 `--network host`，或者让脚本进容器跑；
# 光 `-p 9222:9222` 是不通的（Docker 的端口映射走容器 IP，不是回环）。
CDP_PORT = int(os.getenv("RESIDENT_CDP_PORT", "9222"))
