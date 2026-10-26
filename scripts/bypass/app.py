"""FastAPI 应用：路由、中间件、响应构造。

业务决策都在 forward.py，这里只负责 HTTP 层——解析请求、拼响应头、报错。
"""

from __future__ import annotations

import sys
from urllib.parse import urljoin, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from .config import (
    DROP_REQUEST,
    DROP_RESPONSE,
    MAX_BODY,
    METHODS,
    PORT,
    PROXY,
    mask_proxy,
)
from .docs import doc_html, doc_json
from .forward import forward
from .inbound import target_url
from .upstream import UpstreamError, pairs

app = FastAPI(title="playdriver bypass", docs_url=None, redoc_url=None, openapi_url=None)


class _NormalizeTargetMiddleware:
    """HTTP 代理会把请求行改成 absolute-form（无前导斜杠），路由前先补上。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            raw = scope.get("raw_path") or b""
            if raw and not raw.startswith(b"/"):
                scope["raw_path"] = b"/" + raw
                scope["path"] = "/" + (scope.get("path") or "")
        await self.app(scope, receive, send)


app.add_middleware(_NormalizeTargetMiddleware)


def _upstream_error(exc: UpstreamError) -> JSONResponse:
    print(f"[bypass] error: 无法连接 {exc.host}（curl {exc.code}: {exc.reason}）",
          file=sys.stderr, flush=True)
    return JSONResponse(
        {
            "error": "无法连接目标站点",
            "host": exc.host,
            "curl_code": exc.code,
            "reason": exc.reason,
            "proxy": mask_proxy(PROXY),
            "hint": "连接层就失败了，目标没有返回任何 HTTP 响应，所以不会走冷路径"
                    "——浏览器走同样的网络也一样连不上。最常见的原因是 DNS 被污染，"
                    "域名解析到了无关 IP：用 nslookup 对比 DoH 结果确认，"
                    "换 DNS 或用 PROXY 后重试。",
        },
        status_code=502,
    )


def _build_headers(request: Request, reply, mode: str, elapsed_ms: float):
    """把目标站点的响应头搬回来，去掉不该转发的，补上诊断头。"""
    raw: list[tuple[bytes, bytes]] = []
    for name, value in reply.headers:
        low = name.lower()
        if low in DROP_RESPONSE:
            continue
        if low == "location" and 300 <= reply.status < 400:
            # 重定向指回本服务，客户端才会继续把后续跳转也交给我们处理
            host = request.headers.get("host") or f"127.0.0.1:{PORT}"
            value = f"{request.url.scheme}://{host}/{urljoin(reply.url, value)}"
        raw.append((low.encode("latin-1", "replace"), value.encode("latin-1", "replace")))

    if request.method != "HEAD":
        raw.append((b"content-length", str(len(reply.body)).encode()))
    raw.append((b"x-bypass-url", reply.url.encode("latin-1", "replace")))
    raw.append((b"x-bypass-host", urlsplit(reply.url).netloc.encode("latin-1", "replace")))
    raw.append((b"x-bypass-mode", mode.encode()))
    raw.append((b"x-bypass-ms", str(int(elapsed_ms)).encode()))
    return raw


@app.api_route("/{target:path}", methods=METHODS)
async def proxy(request: Request):
    target = target_url(request)
    if not target:
        if "text/html" in request.headers.get("accept", ""):
            return HTMLResponse(doc_html())
        return JSONResponse(doc_json())
    if not target.startswith(("http://", "https://")):
        return JSONResponse(
            {"error": "目标 URL 必须以 http:// 或 https:// 开头", "got": target[:200]},
            status_code=400,
        )

    body = await request.body()
    headers = {k: v for k, v in pairs(request.headers) if k.lower() not in DROP_REQUEST}

    try:
        reply, elapsed_ms, mode = await run_in_threadpool(
            forward, request.method, target, headers, body
        )
    except UpstreamError as exc:
        return _upstream_error(exc)
    except Exception as exc:
        return JSONResponse(
            {"error": "转发失败", "detail": f"{type(exc).__name__}: {exc}"[:500]},
            status_code=502,
        )

    if len(reply.body) > MAX_BODY:
        return JSONResponse(
            {"error": "响应体超过上限", "limit_bytes": MAX_BODY, "actual_bytes": len(reply.body)},
            status_code=502,
        )

    response = Response(content=b"" if request.method == "HEAD" else reply.body,
                        status_code=reply.status)
    response.raw_headers = _build_headers(request, reply, mode, elapsed_ms)
    return response
