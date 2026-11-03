"""服务编排：把转发服务、常驻浏览器、手机画面监控拼成一个进程。

入口在 scripts/run.py。四个组件默认全开；`--no-browser` / `--no-ui` /
`--no-vnc` 各摘掉一个，app 结构与端口完全不变。

装配顺序是这个文件唯一的硬约束：自家的路由和挂载必须先于
`mount("/", bypass_app)`，否则会被转发服务的全捕获吃掉（见 routes.py）。
"""

from __future__ import annotations

import logging
import sys
from typing import Optional

import uvicorn
from fastapi import FastAPI

from bypass.app import app as bypass_app
from bypass.config import LOG_LEVEL, PORT
from routes import register

from .browser import ResidentBrowser
from .config import PROFILE_DIR
from .watcher import Watcher


def _start_uni2ui():
    """建 uni2ui 的 app（它自己会起设备表监听线程）。起不来就返回 None。

    失败不影响转发 —— 和常驻浏览器一样，缺一个组件就少一块能力，
    其余照常服务。
    """
    # uni2ui 的日志默认没有 handler（独立运行时由它自己的 main() 配 basicConfig）。
    # 嵌进来时给它挂一个只输出 warning 以上的 handler：成功路径保持安静，
    # agent 拉不起来、设备拒绝指令这类事能看见。
    logger = logging.getLogger("uni2ui")
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("[uni2ui] %(levelname)s %(message)s"))
        logger.addHandler(handler)
    logger.setLevel(logging.WARNING)

    try:
        from uni2ui.app import create_app

        app, registry = create_app()
    except Exception as exc:
        print(
            f"[uni2ui] error: 手机画面监控不可用（{type(exc).__name__}: {exc}），"
            "只提供转发",
            file=sys.stderr,
            flush=True,
        )
        return None, None

    print("[uni2ui] 手机画面监控已启动，路径 /adbs", flush=True)
    return app, registry


def build_app(
    browser: Optional[ResidentBrowser],
    adbs_app=None,
    vnc: bool = True,
) -> FastAPI:
    app = FastAPI(
        title="playdriver",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    # 先装自家的（/vnc、/cookies、/adbs），最后才把全捕获挂到 "/" 兜底
    register(app, browser, adbs_app, vnc)
    app.mount("/", bypass_app)
    return app


async def serve(
    with_browser: bool = True,
    with_ui: bool = True,
    with_vnc: bool = True,
) -> None:
    browser = None
    if with_browser:
        browser = ResidentBrowser()
        try:
            await browser.start()
        except Exception as exc:
            # 浏览器起不来不影响转发，/cookies 会返回 503 并说明原因
            print(f"[resident] error: 常驻浏览器不可用（{type(exc).__name__}: {exc}），"
                  "只提供转发服务", file=sys.stderr, flush=True)
            browser = None
        else:
            print(f"[resident] 浏览器已启动，profile: {PROFILE_DIR}", flush=True)

    watcher = Watcher(browser) if browser is not None else None
    if watcher is not None:
        watcher.start()

    adbs_app, uni2ui_registry = _start_uni2ui() if with_ui else (None, None)

    # uvicorn 自己接管 SIGINT / SIGTERM，所以 Ctrl+C 与 docker stop 都能让它
    # 正常返回，收尾统一放在 finally 里。
    config = uvicorn.Config(build_app(browser, adbs_app, with_vnc), host="0.0.0.0",
                            port=PORT, log_level=LOG_LEVEL)
    server = uvicorn.Server(config)
    try:
        await server.serve()
    finally:
        if watcher is not None:
            await watcher.stop()
        if browser is not None:
            await browser.stop()
        if uni2ui_registry is not None:
            uni2ui_registry.stop()
        print("[resident] 已退出", flush=True)
