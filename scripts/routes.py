"""挂在 bypass 全捕获**之前**的全局路由。

`bypass` 整个服务挂在 `/` 上，路由是 `/{target:path}` 全捕获 —— 任何在它
**之后**注册的路径都会被当成目标地址去转发（`/cookies` 会被理解成目标
"cookies"，`/adbs/api/devices` 会被当成目标 "/adbs/api/devices"）。
所以凡是"我们自己的接口"必须集中在这一个文件里、先于 `mount("/")` 装配，
散落到各个包里最容易漏掉。

依赖方向：routes.py → resident / uni2ui / vnc。三个包互不认识，也不认识 routes.py。

为什么是 `register(app, ...)` 而不是返回一个 APIRouter：
FastAPI 的 `include_router` 只处理 `APIRoute` / `Route` / `WebSocketRoute`，
**不处理 `Mount`**，所以 `router.mount()` 再 `include_router()` 会被静默丢掉。
这里直接往 app 上装，语义最直白。

现有三条：
  /vnc      noVNC 桌面（浏览器画面），省掉 6080
  /cookies  常驻浏览器里的 cookie
  /adbs     手机画面监控台（uni2ui）
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse

from resident.browser import BrowserGone, ResidentBrowser
from vnc import mount as mount_vnc


def _cookies_router(browser: Optional[ResidentBrowser]) -> APIRouter:
    router = APIRouter()

    # 两条路径注册同一个函数：带斜杠的那条不注册的话，会被挂在 "/" 上的转发
    # 全捕获路由吃掉，当成目标地址 "cookies" 返回 400。
    @router.get("/cookies", response_model=None)
    @router.get("/cookies/", response_model=None)
    async def cookies(domain: str = ""):
        """返回 cookie 数组；给了 domain 就只返回该域（含父域下发的）。

        只返回 cookie 本身，不夹带 user_agent 之类的附加字段——调用方要的是
        "这个站的 cookie 长什么样"，多一个字段就多一层解释成本。

        注意别和"一次性浏览器"混淆：那是转发服务撞上 403 时临时拉起来过挑战、
        用完就关的；这里读的是**常驻浏览器**（用户手动点、窗口一直开着）。
        转发服务也不依赖这个接口，冷路径撞上失效的 cf_clearance 会自己重解。
        """
        if browser is None:
            return JSONResponse({"error": "未启用常驻浏览器（--no-browser）"}, status_code=503)

        try:
            return await browser.cookies(domain.strip())
        except BrowserGone as exc:
            return JSONResponse({"error": str(exc)}, status_code=503)

    return router


def register(
    app: FastAPI,
    browser: Optional[ResidentBrowser] = None,
    adbs_app=None,
    vnc: bool = True,
) -> None:
    """把自家接口装到 app 上。**必须在 `app.mount("/", bypass_app)` 之前调用。**

    Args:
        app: 外层 app
        browser: 常驻浏览器；None 表示 `--no-browser`，`/cookies` 返回 503
        adbs_app: uni2ui 的 ASGI app；None 表示 `--no-ui`，不挂 `/adbs`
        vnc: 是否挂 noVNC 桌面；False 表示 `--no-vnc`，`/vnc` 返回 503
    """
    app.include_router(_cookies_router(browser))

    # noVNC：静态文件 + 自己写的 WS ↔ TCP 桥。容器里才有 /usr/share/novnc，
    # 宿主机上跑会因为找不到静态文件自动降级成 503，不影响其余接口。
    mount_vnc(app, enabled=vnc)

    # 挂载也是"注册"，同样要抢在全捕获前面。
    # 挂上前缀后 uni2ui 内部路由不用改：/adbs/ → 它自己的 /，
    # /adbs/api/devices → 它自己的 /api/devices（前端用相对路径拼）。
    if adbs_app is not None:
        app.mount("/adbs", adbs_app, name="adbs")

        # 少了这一条，`/adbs`（不带尾斜杠）会掉进 "/" 的全捕获，被当成目标
        # 地址 "adbs" 返回 400：Mount 对 "/adbs" 只给 PARTIAL，而 catch-all
        # 给 FULL，Starlette 优先 FULL，轮不到 redirect_slashes 出手。
        async def _adbs_redirect(request: Request) -> RedirectResponse:
            query = f"?{request.url.query}" if request.url.query else ""
            return RedirectResponse(f"/adbs/{query}", status_code=307)

        app.add_api_route("/adbs", _adbs_redirect, methods=["GET"], include_in_schema=False)
    else:
        # 和 /cookies 一致：组件没开就给明确的 503，而不是让它掉进转发全捕获
        # 被当成目标地址 "adbs" 返回一句莫名其妙的 400
        @app.api_route("/adbs", methods=["GET"], include_in_schema=False)
        @app.api_route("/adbs/{rest:path}", methods=["GET"], include_in_schema=False)
        async def _adbs_disabled(rest: str = ""):
            return JSONResponse({"error": "未启用手机画面监控（--no-ui）"}, status_code=503)
