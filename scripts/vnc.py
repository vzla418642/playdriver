"""把 noVNC 挂到主服务的 `/vnc` 下，看浏览器不用再单独开 6080。

## 分工：静态文件自己发，WebSocket 甩给已经在工作的 websockify

noVNC 是两样东西：

1. `/usr/share/novnc` 里的静态文件（`vnc.html` + `app/` + `core/` + `vendor/`）
2. 一条 **WebSocket 通道**：前端把 RFB 字节塞进 WS 帧发给服务端，服务端再
   转成裸 TCP 喂给 x11vnc

第 1 件 `StaticFiles` 就够。第 2 件**不自己实现**：`start.sh` 里的
`websockify --web /usr/share/novnc 6080 localhost:5900` 本来就是干这个的，
而且它是经过验证能用的。所以这里只做一件事 —— 把浏览器的
`/websockify` 反向代理到 `ws://127.0.0.1:6080/websockify`，字节原样搬运。

自己写 WS ↔ TCP 桥那条路走过，代价是得自己承担 RFB 的全部语义和排障：
上游一沉默就变成「WS 连上了、日志一片安静、画面全黑」，完全没法定位。
既然 6080 好好的，就别重造。

## 三条坑（都踩过）

**零、noVNC 不知道自己在子路径下。** 它拼地址的代码是 `url += '/' + path`
（`vnc.html` / `vnc_lite.html` 都一样，`path` 默认 `'websockify'`），**永远
钉在站点根上**。页面在 `/vnc/vnc.html`，它照样去连 `/websockify`。所以
**根上必须有 `/websockify`**（就是下面那条别名路由），否则线上表现就是
`WebSocket /websockify 403`。

**一、WS 路由必须早于 `Mount`。** `Mount("/vnc", StaticFiles)` 对
`/vnc/websockify` 也会给出 FULL 匹配（前缀匹配后把剩余路径交给子 app），
而 `StaticFiles.__call__` 第一行是 `assert scope["type"] == "http"` ——
WS 请求进去直接 AssertionError。Starlette 的 Router 遇到 FULL 立刻收工，
所以谁先注册谁生效。

**二、`/vnc`（不带尾斜杠）自己接不住。** `Mount` 的正则是
`^/vnc/(?P<path>.*)$`，`/vnc` 压根不匹配，会掉进 bypass 的
`/{target:path}` 全捕获、被当成目标地址 "vnc" 返回 400。所以补一条 307
重定向，直接落到会自动连接的 `vnc.html`。

## 和 6080 的关系

`/vnc` 和 6080 是**同一份内容的两条入口**：静态文件同一份，WS 同一路
（`/vnc` 反代到 6080）。`start.sh` 一行没动。真要砍掉 6080，得先让这里
自己实现 WS 桥，不建议。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
from pathlib import Path
from typing import Optional

import websockets
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket

log = logging.getLogger("playdriver.vnc")
# 带 `[vnc]` 前缀直出 stderr，和 `[resident]` / `[uni2ui]` 一致：
# 正常会话完全静默，出问题才说话。转发是黑盒，画面不出来时这里要是也没
# 声音，就只能靠猜（踩过）。
if not log.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("[vnc] %(levelname)s %(message)s"))
    log.addHandler(_handler)
log.setLevel(logging.WARNING)
log.propagate = False

PREFIX = "/vnc"
DEFAULT_STATIC = "/usr/share/novnc"
# 反代目标：websockify 自己那个端口，不是 x11vnc 的 5900
DEFAULT_TARGET = "127.0.0.1:6080"
# websockify 在它自己端口上的固定 WS 路径
WS_PATH = "websockify"
# 落地页：autoconnect 省掉点 Connect，resize=scale 让画面跟着窗口缩放。
# 不用带 `path=` —— noVNC 默认就去连根上的 /websockify，我们正好在那儿接着。
DEFAULT_LANDING = "vnc.html?autoconnect=1&resize=scale"

CONNECT_TIMEOUT = 3.0
# 连上 websockify 后等第一段数据的超时。RFB 是**服务端先说**
# （`RFB 003.008\n`），所以连上却迟迟没有帧，只有一种解释：上游不正常。
# 没有这个看门狗，表现就是「WS 连上了、日志安静、画面全黑」（踩过）。
FIRST_BYTE_TIMEOUT = 5.0

# websockets 13 起 connect() 会读 HTTP_PROXY/HTTPS_PROXY，本地回环绝不能被带走；
# 12 的旧客户端没有这个参数，按签名判断，避免写死版本。
_PROXY_KWARG = "proxy" in inspect.signature(websockets.connect).parameters


def _split_target(target: str) -> tuple[str, int]:
    """`"127.0.0.1:6080"` → `("127.0.0.1", 6080)`；只给端口时默认本机。"""
    host, _, port = target.rpartition(":")
    if not host:
        host, port = "127.0.0.1", target
    return host, int(port)


async def _connect_upstream(url: str, protocols: list[str]):
    kwargs = {
        "subprotocols": protocols,
        # 一帧就是一段 RFB，大画面更新上 MB 很正常，默认 1MiB 会把帧截断
        "max_size": None,
        # websockify 不支持 permessage-deflate，别白烧 CPU
        "compression": None,
        "open_timeout": CONNECT_TIMEOUT,
    }
    if _PROXY_KWARG:
        kwargs["proxy"] = None
    return await websockets.connect(url, **kwargs)


async def _pump_browser_to_upstream(ws: WebSocket, upstream, stats: dict) -> str:
    """浏览器 → websockify。返回结束原因。"""
    while True:
        message = await ws.receive()
        if message["type"] == "websocket.disconnect":
            return "浏览器断开"
        data = message.get("bytes")
        if data is not None:
            stats["tx"] += len(data)
            await upstream.send(data)
            continue
        text = message.get("text")
        if not text:
            continue
        # 文本帧原样转文本帧：老 noVNC 的 base64 模式就靠这个
        stats["tx"] += len(text)
        await upstream.send(text)


async def _pump_upstream_to_browser(upstream, ws: WebSocket, stats: dict) -> str:
    """websockify → 浏览器。返回结束原因。"""
    while True:
        if stats["rx"] == 0:
            try:
                data = await asyncio.wait_for(upstream.recv(), FIRST_BYTE_TIMEOUT)
            except asyncio.TimeoutError:
                return f"连上了但 {FIRST_BYTE_TIMEOUT:.0f} 秒没收到任何数据"
        else:
            try:
                data = await upstream.recv()
            except Exception as exc:
                return f"上游断开（{type(exc).__name__}）"
        if isinstance(data, str):
            stats["rx"] += len(data)
            await ws.send_text(data)
        else:
            stats["rx"] += len(data)
            await ws.send_bytes(data)


def _make_handler(host: str, port: int):
    async def handler(ws: WebSocket) -> None:
        url = f"ws://{host}:{port}/{WS_PATH}"
        offered = ws.scope.get("subprotocols") or []

        # 先连上游、再 accept：上游不通时浏览器拿到的是握手失败（403），
        # 而不是「假装连上再莫名断开」——后者在 noVNC 里只会显示成
        # "Connecting..." 然后静默重连，最难查
        try:
            upstream = await _connect_upstream(url, offered or ["binary"])
        except Exception as exc:
            log.warning("上游 %s 连不上（%s: %s），检查 6080 上的 websockify",
                        url, type(exc).__name__, exc)
            await _safe_close(ws)
            return

        # 只回客户端提过的子协议，否则浏览器按 RFC 6455 判握手失败
        chosen = upstream.subprotocol if upstream.subprotocol in offered else None
        await ws.accept(subprotocol=chosen)

        stats = {"rx": 0, "tx": 0}
        up = asyncio.create_task(_pump_browser_to_upstream(ws, upstream, stats))
        down = asyncio.create_task(_pump_upstream_to_browser(upstream, ws, stats))

        # 先结束的那一头决定是谁把会话弄没的
        ended_by_upstream = False
        reason = ""
        try:
            done, _ = await asyncio.wait({up, down}, return_when=asyncio.FIRST_COMPLETED)
            finished = next(iter(done))
            ended_by_upstream = finished is down
            if finished.cancelled():
                reason = "被取消"
            elif finished.exception() is not None:
                exc = finished.exception()
                reason = f"{type(exc).__name__}: {exc}"
            else:
                reason = finished.result()
        finally:
            for task in (up, down):
                task.cancel()
            await asyncio.gather(up, down, return_exceptions=True)
            try:
                await upstream.close()
            except Exception:
                pass
            # 上游先走 → 用 1011。用 1000 的话 noVNC 会当成「正常断开」，
            # 一旦打开过 reconnect 就变成静默重连刷屏
            await _safe_close(ws, 1011 if ended_by_upstream else 1000)

        if ended_by_upstream:
            log.warning("会话中断：%s（上行 %d 字节 / 下行 %d 字节）",
                        reason, stats["tx"], stats["rx"])

    return handler


async def _safe_close(ws: WebSocket, code: int = 1000) -> None:
    """对端可能已经断了；accept 之前调用会变成 403，正是我们要的。"""
    try:
        await ws.close(code=code)
    except Exception:
        pass


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
    handler = _make_handler(host, port)

    # ① WS 反代 —— 必须早于下面的 Mount，否则被 StaticFiles 吃掉（见模块 docstring）
    #
    # 根上的 `/websockify` 是**主入口**：noVNC 默认就找它（它把 path 拼在站点
    # 根上，不跟页面目录走）。`/vnc/websockify` 是同一路的别名，方便手工
    # 用 ?path=vnc/websockify 指定时也能连。
    app.add_api_websocket_route("/" + WS_PATH, handler, name="vnc-ws")
    app.add_api_websocket_route(f"{prefix}/{WS_PATH}", handler, name="vnc-ws-prefixed")

    # ② 落地页重定向 —— /vnc 和 /vnc/ 都不匹配 Mount 的正则，得自己接住
    landing_url = f"{prefix}/{landing}"

    async def _landing(request: Request) -> RedirectResponse:
        extra = f"&{request.url.query}" if request.url.query else ""
        return RedirectResponse(f"{landing_url}{extra}", status_code=307)

    for path in (prefix, prefix + "/"):
        app.add_api_route(path, _landing, methods=["GET"], include_in_schema=False)

    # ③ 静态文件 —— html=True 让目录请求能落 index.html（本包不一定有，不影响）
    app.mount(prefix, StaticFiles(directory=static_dir, html=True), name="vnc")

    log.info("noVNC 已挂载：%s → %s（WS 反代到 ws://%s:%s/%s）",
             prefix, static_dir, host, port, WS_PATH)
    return True
