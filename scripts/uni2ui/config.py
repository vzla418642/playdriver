"""uni2ui 配置。全部走环境变量，前缀 UNI2UI_。

命令行参数会覆盖环境变量（见 app.py）。
"""

import os


def _env(name: str, default: str) -> str:
    return os.getenv(f"UNI2UI_{name}", default)


# 监听地址。要给别人看就设 0.0.0.0
HOST = _env("HOST", "127.0.0.1")
PORT = int(_env("PORT", "8000"))

# adb server。只连它读设备表，绝不 connect/disconnect 设备
ADB_HOST = _env("ADB_HOST", "127.0.0.1")
ADB_PORT = int(_env("ADB_PORT", "5037"))

# 设备上 u2 agent（ATX）的端口。
# uiautomator2 3.x 用 9008，2.x 用 7912，两个都试，谁通算谁的
AGENT_PORTS = tuple(
    int(p) for p in _env("AGENT_PORTS", "9008,7912").split(",") if p.strip()
)

# 抓帧。实测 agent 路径 43ms/帧，15fps（67ms 间隔）稳得住，
# 而且设备端 CPU 在 15fps 和空闲时几乎没差别（都是 ~65%），
# 所以默认给 15 —— 8fps 那种一顿一顿的观感就是"不跟随"
DEFAULT_FPS = float(_env("FPS", "15"))
MAX_FPS = float(_env("MAX_FPS", "20"))
# 没有 agent 时只能走 screencap，一次 ~1s，别硬拉帧率
FALLBACK_FPS = float(_env("FALLBACK_FPS", "1"))
# 60 是原来那版用的值。实测 q60→146KB / q80→213KB，抓帧耗时几乎一样，
# 但体积小 30% 意味着 adb 通道和浏览器解码都轻一些
JPEG_QUALITY = int(_env("JPEG_QUALITY", "60"))

# 设备表刷新
POLL_INTERVAL = float(_env("POLL_INTERVAL", "2"))
# 没人看了多久之后停掉抓帧线程（省 CPU）。
# 停掉只是不再截图，设备上的 agent 一般还在，下次有人看能立刻出帧。
IDLE_TIMEOUT = float(_env("IDLE_TIMEOUT", "30"))
# agent 探测失败后多久再试。别设太大：这期间只有 1fps 的 screencap，
# 用户看到的就是"卡"。5 秒够 agent 自己起来，也不至于把设备问烦
AGENT_RETRY = float(_env("AGENT_RETRY", "5"))

# 设备一上线就提前把 agent 准备好（拉起一次约 0.5s），
# 这样第一次打开页面就是立刻出画，而不是先卡半秒
PREWARM = _env("PREWARM", "1") not in ("0", "false", "no")

# agent 不在时，允不允许自己把它拉起来。
# 拉法就是 u2 内部用的那条命令：一条常驻 adb shell 跑 app_process，
# 不依赖 uiautomator2 这个包（谁起的谁负责活着，连接一断 agent 就退）。
# 前提是设备上已经有 u2.jar，没有就直接放弃，退回 screencap。
# 之所以要这一步：agent 是 app_process 的子进程，kumuapp/vkapp 退出会把它带走，
# 没人拉的话只能 1fps 抓图，画面会明显卡。
BOOTSTRAP_AGENT = _env("BOOTSTRAP_AGENT", "1") not in ("0", "false", "no")
# 设备上的 u2 jar 路径（装过一次 u2 就有）
AGENT_JAR = _env("AGENT_JAR", "/data/local/tmp/u2.jar")

LOG_LEVEL = _env("LOG_LEVEL", "info")

# 访问口令。留空 = 不校验（默认，本机用不折腾）。
# 一旦把 --host 设成 0.0.0.0 给别人/机房用，**建议设上**：
# 这个面板能点、能滑、能按电源键，裸奔在局域网里不合适。
# 设了之后所有 /api 和 /stream 都要求 ?token=xxx
TOKEN = _env("TOKEN", "").strip()
