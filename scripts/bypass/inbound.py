"""入站请求解析：从客户端请求里还原出目标 URL，并构造转发用的请求头。

这里处理的是"乱七八糟的入站形态"——路径拼接、X-Target-URL 覆盖、
HTTP 代理的 absolute-form、冒号被转义成 %3A 等等。
"""

from __future__ import annotations

import re
from urllib.parse import unquote, urlsplit

from fastapi import Request

from .config import CF_OWNED
from .creds import CREDS

_ENCODED_ORIGIN = re.compile(r"^(https?)%3a//([^/]*)", re.IGNORECASE)


def _repair_encoded_origin(text: str) -> str:
    """部分代理会把 absolute-form 里的冒号转义成 %3A，只还原 scheme 与 host 部分。"""
    match = _ENCODED_ORIGIN.match(text)
    if not match:
        return text
    return f"{match.group(1)}://{unquote(match.group(2))}{text[match.end():]}"


def target_url(request: Request) -> str:
    """从请求里取出要访问的目标 URL；首页请求返回空串。"""
    override = request.headers.get("x-target-url")
    if override and override.strip():
        return override.strip()

    # 用 raw_path 而不是 request.url.path，避免 Starlette 解码后破坏目标 URL 的转义
    raw = (request.scope.get("raw_path") or b"").decode("latin-1").lstrip("/")
    query = (request.scope.get("query_string") or b"").decode("latin-1")
    target = f"{raw}?{query}" if query else raw

    # 客户端走 HTTP 代理时请求行是 absolute-form，先还原转义再剥掉指向本服务的 origin
    target = _repair_encoded_origin(target)
    own = (request.headers.get("host") or "").lower()
    parts = urlsplit(target)
    if own and parts.netloc.lower() == own:
        rest = target[len(parts.scheme) + 3 + len(parts.netloc):]
        target = _repair_encoded_origin(rest[1:] if rest.startswith("/") else rest)

    # 少数客户端会把 :// 折叠成 :/
    for scheme in ("http", "https"):
        folded = f"{scheme}:/"
        if target.startswith(folded) and not target.startswith(f"{folded}/"):
            target = f"{scheme}://" + target[len(folded):]
            break
    return target.strip()


def split_cookie(headers: dict[str, str]) -> dict[str, str]:
    """摘出客户端自带的 Cookie。必须在 headers 副本上调用，避免冷路径重试时复用旧值。"""
    raw = None
    for key in [k for k in headers if k.lower() == "cookie"]:
        raw = headers.pop(key)
    parsed = {}
    if raw:
        for part in raw.split(";"):
            if "=" in part:
                name, value = part.split("=", 1)
                parsed[name.strip()] = value.strip()
    return parsed


def prepare_headers(host: str, headers: dict[str, str], client_cookies: dict[str, str]):
    """把缓存里的 cookie 与客户端 cookie 合并，并补上与 cf_clearance 匹配的 UA。"""
    jar, agent = CREDS.get(host)
    merged = dict(jar)
    merged.update(client_cookies)  # 客户端的登录态优先，别被缓存的匿名 cookie 顶掉
    # 但过挑战拿到的 cookie 不能被客户端的同名值覆盖：那一个多半是别的 IP/UA
    # 下发的，用了等于白过。客户端没带时也用缓存值，不影响正常登录态。
    for name in CF_OWNED & jar.keys():
        merged[name] = jar[name]
    out = dict(headers)
    if merged:
        out["cookie"] = "; ".join(f"{k}={v}" for k, v in merged.items())
    if agent:
        # cf_clearance 与过挑战时的 UA 绑定，优先级高于任何默认 UA
        out["user-agent"] = agent
    return out
