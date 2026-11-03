"""把 noVNC 挂到主服务上（默认 `/vnc`），看浏览器不用再单独开 6080。

## noVNC 其实是两样东西，缺一不可

1. `/usr/share/novnc` 里的一堆静态文件（`vnc.html` + `app/` + `core/` + `vendor/`）
2. 一条 **WebSocket 通道**：前端把 RFB 字节塞进 WS 二进制帧发给服务端，
   服务端再转成裸 TCP 喂给 x11vnc

`websockify` 干的就是第 2 件事（它顺手用 `--web` 兼职第 1 件）。
**只挂静态文件是不够的**：页面能打开，点 Connect 之后前端会去连
`<站点根>/websockify`（noVNC 把 `path` 拼在根上，不跟页面目录走，见下面
第零条），没这条 WS 路由就永远停在 "Connecting"。所以这里除了 StaticFiles，
还自己写了个 WS ↔ TCP 桥，直接对 `127.0.0.1:5900`。协议本身没有魔法：一帧
二进制 = 一段裸 RFB 字节，双向对拷即可。现代 noVNC 用
`binaryType = 'arraybuffer'` 发原始字节，只有老客户端才走 base64 文本帧，
这里顺手兼容了。

## 三条坑（都踩过）

**零、noVNC 不知道自己在子路径下。** 它拼地址的方式是 `url += '/' + path`
（`vnc.html` / `vnc_lite.html` 都一样，`path` 默认 `'websockify'`），**永远
钉在站点根上**。页面在 `/vnc/vnc.html`，它照样去连 `/websockify`，然后落进
bypass 的全捕获被拒 —— 线上表现就是 `WebSocket /websockify 403`。所以落地页
必须带 `?path=vnc/websockify`（不能带前导斜杠，会拼成 `//vnc/websockify`），
并且给根上的 `/websockify` 留一条别名兜底。

**一、WS 路由必须早于 `Mount`。** `Mount("/vnc", StaticFiles)` 对
`/vnc/websockify` 也会给出 FULL 匹配（前缀匹配后把剩余路径交给子 app），
而 `StaticFiles.__call__` 第一行是 `assert scope["type"] == "http"` ——
WS 请求进去直接 AssertionError。Starlette 的 Router 遇到 FULL 立刻收工，
所以谁先注册谁生效，顺序在这里是硬约束。

**二、`/vnc`（不带尾斜杠）自己接不住。** `Mount` 的正则是
`^/vnc/(?P<path>.*)$`，`/vnc` 压根不匹配，会掉进 bypass 的
`/{target:path}` 全捕获、被当成目标地址 "vnc" 返回 400。所以补一条 307
重定向，直接落到会自动连接的 `vnc.html`。

## 和 6080 的关系

两者可以并存：`websockify --web` 那条老路完全没动，`start.sh` 里照旧。
这里只是多给一个入口，省掉"看画面要换端口"这件事。真要砍掉 6080，
删 `start.sh` 里的 websockify 那行、`Dockerfile` 里去掉 `EXPOSE 6080` 即可。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import os
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

log = logging.getLogger("playdriver.vnc")

PREFIX = "/vnc"
DEFAULT_STATIC = "/usr/share/novnc"
DEFAULT_TARGET = "127.0.0.1:5900"
# 落地页：autoconnect 省掉点 Connect，resize=scale 让画面跟着窗口缩放。
# `path=` 由 mount() 自动补（noVNC 不知道自己在子路径下，见下面的说明）。
DEFAULT_LANDING = "vnc.html?autoconnect=1&resize=scale"


def _split_target(target: str) -> tuple[str, int]:
    """`"127.0.0.1:5900"` → `("127.0.0.1", 5900)`；只给端口时默认本机。"""
    host, _, port = target.rpartition(":")
    if not host:
        host, port = "127.0.0.1", target
    return host, int(port)


async def _pump_ws_to_tcp(ws: WebSocket, writer: asyncio.StreamWriter) -> None:
    """浏览器 → x11vnc。"""
    while True:
        message = await ws.receive()
        if message["type"] == "websocket.disconnect":
            return
        data = message.get("bytes")
        if data is None:
            text = message.get("text")
            if not text:
                continue
            # 老 noVNC 走 base64 文本帧；1.x 不会进这个分支
            try:
                data = base64.b64decode(text, validate=False)
            except (binascii.Error, ValueError):
                log.warning("VNC: 收到无法 base64 解码的文本帧，丢弃 %d 字符", len(text))
                continue
        writer.write(data)
        await writer.drain()


async def _pump_tcp_to_ws(reader: asyncio.StreamReader, ws: WebSocket) -> None:
    """x11vnc → 浏览器。"""
    while True:
        data = await reader.read(65536)
        if not data:
            return
        await ws.send_bytes(data)


def _make_handler(host: str, port: int):
    async def handler(ws: WebSocket) -> None:
        # noVNC 固定 `new WebSocket(url, ['binary'])`，回显子协议最规范；
        # 不回显浏览器也能连，但按协议该回。
        await ws.accept(subprotocol="binary")

        try:
            reader, writer = await asyncio.open_connection(host, port)
        except OSError as exc:
            log.warning("VNC 上游连不上 %s:%s（%s: %s）", host, port, type(exc).__name__, exc)
            await ws.close(code=1011)
            return

        up = asyncio.create_task(_pump_ws_to_tcp(ws, writer))
        down = asyncio.create_task(_pump_tcp_to_ws(reader, ws))
        try:
            await asyncio.wait({up, down}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            # 一头断了另一头也活不成，直接一起收；否则半开连接会一直挂着
            for task in (up, down):
                task.cancel()
            results = await asyncio.gather(up, down, return_exceptions=True)
            for result in results:
                if isinstance(result, Exception) and not isinstance(
                    result, (WebSocketDisconnect, asyncio.CancelledError)
                ):
                    log.debug("VNC 桥接结束：%s: %s", type(result).__name__, result)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            try:
                await ws.close()
            except Exception:
                pass

    return handler


def mount(
    app: FastAPI,
    enabled: bool = True,
    *,
    static_dir: Optional[str] = None,
    target: Optional[str] = None,
    prefix: str = PREFIX,
) -> bool:
    """把 noVNC 挂到 `prefix` 下。**必须在 `app.mount("/", bypass_app)` 之前调用。**

    Returns:
        是否真的挂上了。False 表示关了、或者找不到静态文件 —— 两种情况
        `prefix` 都返回 503 并说明原因，不会掉进转发全捕获。
    """
    static_dir = static_dir or os.environ.get("NOVNC_DIR", DEFAULT_STATIC)
    target = target or os.environ.get("VNC_TARGET", DEFAULT_TARGET)
    landing = os.environ.get("NOVNC_LANDING", DEFAULT_LANDING)

    if not enabled:
        reason = "未启用 VNC 画面（--no-vnc）"
    elif not (Path(static_dir) / "vnc.html").is_file():
        reason = f"未找到 noVNC 静态文件（{static_dir}/vnc.html）"
        log.warning("%s，%s 返回 503", reason, prefix)
    else:
        reason = ""

    if reason:
        # 和 /cookies、/adbs 一致：组件没开就给明确的 503，而不是让它掉进
        # 转发全捕获被当成目标地址 "vnc" 返回一句莫名其妙的 400
        @app.api_route(prefix, methods=["GET"], include_in_schema=False)
        @app.api_route(prefix + "/", methods=["GET"], include_in_schema=False)
        @app.api_route(prefix + "/{rest:path}", methods=["GET"], include_in_schema=False)
        async def _vnc_disabled(rest: str = ""):
            return JSONResponse({"error": reason}, status_code=503)

        return False

    host, port = _split_target(target)

    # noVNC 拼 WebSocket 地址的方式是 `url += '/' + path`（`vnc.html` 和
    # `vnc_lite.html` 一模一样，`path` 默认 `'websockify'`）——**永远钉在站点
    # 根上，不跟页面所在目录走**。页面在 `/vnc/vnc.html`，它照样去连
    # `/websockify`，落进 bypass 全捕获被拒 → 403。所以必须显式把路径告诉它。
    # 注意这里不能带前导斜杠：带了会拼成 `//vnc/websockify`。
    ws_path = f"{prefix.strip('/')}/websockify"
    handler = _make_handler(host, port)

    # ① WS 桥 —— 必须早于下面的 Mount，否则被 StaticFiles 吃掉（见模块 docstring）
    app.add_api_websocket_route(f"/{ws_path}", handler, name="vnc-ws")

    # 兜底别名：直接打开 `/vnc/vnc.html`（不带 ?path=）时，noVNC 仍然会去连根上
    # 的 `/websockify`。给它一条同名路由，省得用户手输地址就撞 403。
    if ws_path != "websockify":
        app.add_api_websocket_route("/websockify", handler, name="vnc-ws-alias")

    # ② 落地页重定向 —— /vnc 和 /vnc/ 都不匹配 Mount 的正则，得自己接住
    landing_url = f"{prefix}/{landing}"
    if "path=" not in landing:
        landing_url += ("&" if "?" in landing else "?") + f"path={ws_path}"

    async def _landing(request: Request) -> RedirectResponse:
        extra = f"&{request.url.query}" if request.url.query else ""
        return RedirectResponse(f"{landing_url}{extra}", status_code=307)

    for path in (prefix, prefix + "/"):
        app.add_api_route(path, _landing, methods=["GET"], include_in_schema=False)

    # ③ 静态文件 —— html=True 让目录请求能落 index.html（本包不一定有，不影响）
    app.mount(prefix, StaticFiles(directory=static_dir, html=True), name="vnc")

    log.info("noVNC 已挂载：%s → %s:%s", prefix, host, port)
    return True
