"""服务编排：把转发服务、常驻浏览器、/cookies 拼成一个进程。

入口在 scripts/run.py。默认全开；`--no-browser` 时只少一个浏览器组件，
app 结构与端口完全不变。
"""

from __future__ import annotations

import sys

import uvicorn
from fastapi import FastAPI

from bypass.app import app as bypass_app
from bypass.config import LOG_LEVEL, PORT

from .browser import ResidentBrowser
from .config import PROFILE_DIR
from .routes import build_router
from .watcher import Watcher


def build_app(browser: ResidentBrowser | None) -> FastAPI:
    """绕过服务挂在 / 上，所以 /cookies 必须先注册。

    bypass 的路由是 /{target:path} 这种全捕获，挂在 / 之后注册的 /cookies 会被
    它吃掉，当成目标地址 "cookies" 去转发。
    """
    app = FastAPI(
        title="playdriver",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.include_router(build_router(browser))
    app.mount("/", bypass_app)
    return app


async def serve(with_browser: bool = True) -> None:
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

    # uvicorn 自己接管 SIGINT / SIGTERM，所以 Ctrl+C 与 docker stop 都能让它
    # 正常返回，收尾统一放在 finally 里。
    config = uvicorn.Config(build_app(browser), host="0.0.0.0", port=PORT, log_level=LOG_LEVEL)
    server = uvicorn.Server(config)
    try:
        await server.serve()
    finally:
        if watcher is not None:
            await watcher.stop()
        if browser is not None:
            await browser.stop()
        print("[resident] 已退出", flush=True)
