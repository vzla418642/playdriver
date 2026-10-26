"""/cookies：把常驻浏览器里的 cookie 交出去。

常驻容器里会同时出现两个浏览器，别混淆：
  - **常驻浏览器**（本模块的对象）—— 用户手动点、窗口一直开着；
  - **一次性浏览器** —— 转发服务撞上 403 时临时拉起来过挑战，用完就关。

这个接口只读常驻浏览器的 context，跟冷路径那个没有任何关系；转发服务也不依赖
它，冷路径撞上失效的 cf_clearance 会自己重解一次。
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from .browser import BrowserGone, ResidentBrowser


def build_router(browser: ResidentBrowser | None) -> APIRouter:
    router = APIRouter()

    # 两条路径注册同一个函数：带斜杠的那条不注册的话，会被挂在 "/" 上的转发
    # 全捕获路由吃掉，当成目标地址 "cookies" 返回 400。
    @router.get("/cookies", response_model=None)
    @router.get("/cookies/", response_model=None)
    async def cookies(domain: str = ""):
        """返回 cookie 数组；给了 domain 就只返回该域（含父域下发的）。

        只返回 cookie 本身，不夹带 user_agent 之类的附加字段——调用方要的是
        "这个站的 cookie 长什么样"，多一个字段就多一层解释成本。
        """
        if browser is None:
            return JSONResponse({"error": "未启用常驻浏览器（--no-browser）"}, status_code=503)

        try:
            return await browser.cookies(domain.strip())
        except BrowserGone as exc:
            return JSONResponse({"error": str(exc)}, status_code=503)

    return router
