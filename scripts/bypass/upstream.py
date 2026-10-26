"""出站转发：把请求打到目标站点，把结果包成 Reply。

这一层只管"发出去、收回来"，不做拦截判断、不重试、不碰缓存。
"""

from __future__ import annotations

import threading
from typing import NamedTuple
from urllib.parse import urlsplit

from curl_cffi import requests as cffi
from curl_cffi.requests.exceptions import RequestException

from .config import IMPERSONATE, PROXY, TIMEOUT

_local = threading.local()


class Reply(NamedTuple):
    status: int
    headers: list[tuple[str, str]]
    body: bytes
    url: str
    cookies: list[tuple[str, str]]


# curl 退出码 -> 人话。连不上目标时最常见的就是 6/7/28，光看 curl 原文根本
# 不知道是"被墙"还是"域名没解析"还是"忘了配代理"。
CURL_HINTS = {
    5: "代理地址无法解析，检查 PROXY 是否写错",
    6: "目标域名无法解析，DNS 可能被污染",
    7: "无法建立 TCP 连接：域名可能被污染解析到了错误 IP，或目标被墙",
    28: "连接或读取超时：域名可能被污染解析到了错误 IP，或目标被墙",
    35: "TLS 握手失败，出口可能被中间设备拦截",
    56: "连接被对端重置",
}


class UpstreamError(Exception):
    """转发阶段失败：连不上目标，一个 HTTP 响应都没拿到。

    与"目标返回了 403 挑战页"是两回事——后者能拿到响应，会走冷路径；
    这里连 TCP 都建不起来，浏览器走同样的网络也一样连不上，所以不重试。
    """

    def __init__(self, host: str, exc: Exception) -> None:
        self.host = host
        self.code = getattr(exc, "code", None)
        self.reason = CURL_HINTS.get(self.code) or str(exc)[:200]
        super().__init__(self.reason)


def pairs(headers) -> list[tuple[str, str]]:
    """curl_cffi 的 Headers 要取多值（多重 Set-Cookie 会丢），普通 dict 走 items。"""
    getter = getattr(headers, "multi_items", None)
    if callable(getter):
        try:
            return [(str(k), str(v)) for k, v in getter()]
        except Exception:
            pass
    try:
        return [(str(k), str(v)) for k, v in headers.items()]
    except Exception:
        return []


def _pooled_session() -> cffi.Session:
    session = getattr(_local, "session", None)
    if session is None:
        session = cffi.Session(impersonate=IMPERSONATE)
        _local.session = session
    return session


def do_request(method: str, url: str, headers: dict[str, str], body: bytes) -> Reply:
    kwargs = {"headers": headers, "allow_redirects": False, "timeout": TIMEOUT}
    if body:
        kwargs["data"] = body
    if PROXY:
        kwargs["proxies"] = {"http": PROXY, "https": PROXY}

    # 明文 http 目标在连接复用后会让 curl 尝试 h2c 升级，uvicorn 这类服务端
    # 会把后续请求判成非法路径。用一次性会话规避，https 目标不受影响。
    disposable = url.startswith("http://")
    session = cffi.Session(impersonate=IMPERSONATE) if disposable else _pooled_session()
    try:
        resp = session.request(method, url, **kwargs)
    except RequestException as exc:
        raise UpstreamError(urlsplit(url).netloc, exc) from exc
    else:
        return Reply(
            status=resp.status_code,
            headers=pairs(resp.headers),
            body=resp.content,
            url=str(resp.url),
            cookies=[(str(k), str(v)) for k, v in resp.cookies.items()],
        )
    finally:
        if disposable:
            try:
                session.close()
            except Exception:
                pass
