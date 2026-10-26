"""环境变量与常量。

只有这里读 os.getenv，其它模块一律从这里导入，改配置时只看一个文件。
"""

from __future__ import annotations

import os

# ---------------------------------------------------------------------------
# 配置：常用的放前面
# ---------------------------------------------------------------------------

# 出站代理，浏览器与 curl_cffi 共用。支持 http:// https:// socks5://
# 留空即直连。例：http://192.168.51.55:7890
PROXY = (os.getenv("PROXY") or os.getenv("BYPASS_PROXY") or "").strip()

# 服务监听端口
PORT = int(os.getenv("BYPASS_PORT", "8000"))

# 是否启用冷路径。设 0 则只做透传，遇到 Cloudflare 直接返回原始响应
COLD_ENABLED = os.getenv("BYPASS_COLD", "1") == "1"

# 单次转发超时（秒）
TIMEOUT = float(os.getenv("BYPASS_TIMEOUT", "60"))

# 过挑战超时（秒）。太短会在慢站点上误判失败，太长会拖住队列
SOLVE_TIMEOUT = float(os.getenv("BYPASS_SOLVE_TIMEOUT", "60"))

# ---- 以下基本不用动 ----

# 响应体上限（MB）。超限返回 502，注意是缓冲完才判断
MAX_BODY = int(float(os.getenv("BYPASS_MAX_BODY_MB", "20")) * 1024 * 1024)

# curl_cffi 的指纹模板，如 chrome / chrome131 / safari17_0
IMPERSONATE = os.getenv("BYPASS_IMPERSONATE", "chrome")

# 浏览器可执行文件。镜像里是 /usr/bin/google-chrome；Windows 本地调试按常见
# 安装位置找一遍，免得每次都要传环境变量。显式传 CHROME_PATH 时以它为准。
_WIN_CHROME = (
    r"D:\Chrome\Application\chrome.exe",
    r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe",
    r"%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe",
    r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
)


def _default_chrome() -> str:
    if os.name != "nt":
        return "/usr/bin/google-chrome"
    for raw in _WIN_CHROME:
        path = os.path.expandvars(raw)
        if "%" not in path and os.path.exists(path):
            return path
    return "/usr/bin/google-chrome"


CHROME_PATH = os.getenv("CHROME_PATH") or _default_chrome()

# cookie 缓存秒数，仅在响应不带 expires 时兜底
COOKIE_TTL = float(os.getenv("BYPASS_COOKIE_TTL", "1500"))

# 设 1 则透传客户端的 User-Agent。默认丢弃，改用与 TLS 指纹匹配的 Chrome UA
PASS_UA = os.getenv("BYPASS_PASS_UA", "0") == "1"

# uvicorn 日志级别。默认 info 会打印逐条请求，排查时够用；设 warning 可完全静默
LOG_LEVEL = os.getenv("BYPASS_LOG_LEVEL", "info")

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]

HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "trailers", "transfer-encoding", "upgrade",
})

# 请求侧：Host/Content-Length 交给 curl 重算，Accept-Encoding 交给 curl 协商，
# 这样拿到的 body 已经是解压后的明文。
DROP_REQUEST = HOP_BY_HOP | {"host", "content-length", "accept-encoding"}
if not PASS_UA:
    DROP_REQUEST |= {"user-agent"}

# 响应侧：body 已解压、长度已变，必须由我们重写
DROP_RESPONSE = HOP_BY_HOP | {"content-encoding", "content-length"}

BLOCK_STATUSES = frozenset({403, 503})
BLOCK_MARKERS = (
    b"cf-mitigated", b"just a moment", b"__cf_chl", b"cf_challenge", b"attention required",
)

# 这些 cookie 由服务自己过挑战产生，与出口 IP / UA 强绑定。客户端从自己浏览器
# 复制过来的那一个必然是别的环境发的，拿它覆盖刚过完挑战的值只会继续被拦。
CF_OWNED = frozenset({"cf_clearance", "__cf_bm"})


def mask_proxy(proxy: str) -> str:
    """展示用：把代理串里的账号密码打掉。"""
    if not proxy:
        return "（未设置）"
    if "@" in proxy:
        head, _, tail = proxy.rpartition("@")
        scheme = head.split("://")[0] + "://" if "://" in head else ""
        return f"{scheme}***@{tail}"
    return proxy
