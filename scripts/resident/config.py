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
