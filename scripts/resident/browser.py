"""常驻浏览器：一个长期活着的有头 Chrome，窗口关掉就自动重开。

用 launch_persistent_context 而不是"先起 Chrome 再连 9222"：

  - Playwright 直接持有浏览器进程，不需要额外开调试端口；
  - 不需要自己管子进程回收、孤儿进程、端口冲突；
  - profile 落盘由 Chrome 自己负责，关窗重开还是同一份 user-data。

生命周期的约定：
  - 关掉最后一个窗口 → context 关闭 → 自动重开，profile 不变；
  - 只关一个标签页 → 什么都没发生；
  - 结束会话 → Ctrl+C / docker stop，走 stop() 优雅收尾。
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

from bypass.config import CHROME_PATH, PROXY
from bypass.creds import related

from .config import (
    HEALTH_INTERVAL,
    MAX_LAUNCH_FAILURES,
    PROFILE_DIR,
    RELAUNCH_DELAY,
    RELAUNCH_MAX_DELAY,
    WINDOW_HEIGHT,
    WINDOW_WIDTH,
)

# Chrome 用这几个文件标记"这个 profile 正被某个进程占用"。Linux 上它们是符号
# 链接，容器被 kill 时不会被清理，下次启动会直接报 profile in use。
_LOCK_FILES = ("SingletonLock", "SingletonSocket", "SingletonCookie")


class BrowserGone(RuntimeError):
    """浏览器当前不可用（正在重开，或已经放弃重开）。"""


def _log(message: str) -> None:
    print(f"[resident] {message}", flush=True)


def _log_error(message: str) -> None:
    print(f"[resident] error: {message}", file=sys.stderr, flush=True)


def _clear_profile_locks(profile_dir: str) -> None:
    for name in _LOCK_FILES:
        path = os.path.join(profile_dir, name)
        try:
            if os.path.islink(path) or os.path.exists(path):
                os.unlink(path)
        except OSError as exc:
            _log_error(f"清理 {path} 失败（{type(exc).__name__}: {exc}）")


class ResidentBrowser:
    """一个常驻的有头 Chrome，外加它的看守任务。"""

    def __init__(self) -> None:
        self._pw: Any = None
        self._context: Any = None
        self._stopping = False
        self._task: asyncio.Task | None = None

    @property
    def context(self) -> Any:
        """当前的 context；重开期间是 None，调用方必须处理。"""
        return self._context

    async def start(self) -> None:
        """起浏览器，然后开始盯着它。

        首次启动失败不抛：转发服务照常可用，看守任务会按退避继续重试，这期间
        /cookies 返回 503。只有 Playwright 驱动本身起不来才算彻底不可用。
        """
        os.makedirs(PROFILE_DIR, exist_ok=True)
        _clear_profile_locks(PROFILE_DIR)

        from patchright.async_api import async_playwright

        self._pw = await async_playwright().start()
        try:
            await self._launch()
        except Exception as exc:
            _log_error(f"浏览器首次启动失败（{type(exc).__name__}: {exc}），后台继续重试")

        # 放在首次启动之后，避免看守任务和它同时抢着起第二个浏览器
        self._task = asyncio.create_task(self._supervise(), name="resident-supervisor")

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

        if self._context is not None:
            try:
                await self._context.close()
            except Exception:
                pass
            self._context = None

        if self._pw is not None:
            try:
                await self._pw.stop()
            except Exception:
                pass
            self._pw = None

    async def cookies(self, domain: str = "") -> list[dict]:
        """当前浏览器里的全部 cookie；给了 domain 就按域过滤。

        用 context.cookies() 取全量再自己筛，而不是 context.cookies(urls=[...])：
        后者只返回"该 URL 会发出的" cookie，会把同注册域下别的子域下发的、以及
        cf_clearance 这种域级 cookie 一并丢掉，而调用方要的往往正是它们。

        过滤规则是 RFC 6265 的域匹配，与 bypass 缓存 cookie 时的判定同一套。
        """
        context = self._context
        if context is None:
            raise BrowserGone("浏览器未就绪")

        try:
            raw = await context.cookies()
        except Exception as exc:
            raise BrowserGone(f"读取 cookie 失败（{type(exc).__name__}: {exc}）") from exc

        if not domain:
            return raw

        target = domain.strip().lstrip(".").lower()
        return [c for c in raw if related(str(c.get("domain", "")).lstrip(".").lower(), target)]

    # ---- 内部 ----

    async def _launch(self) -> None:
        args = [
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            # 上次是被 kill 的，Chrome 会弹"恢复页面"气泡挡在窗口顶上
            "--hide-crash-restore-bubble",
            "--window-position=0,0",
            f"--window-size={WINDOW_WIDTH},{WINDOW_HEIGHT}",
        ]
        if PROXY:
            # 与 bypass 的冷路径共用出口：cf_clearance 绑定出口 IP，
            # 两边出口不一致的话，这边过完的 cookie 到那边也用不了
            args.append(f"--proxy-server={PROXY}")

        # no_viewport：不模拟视口尺寸，页面按真实窗口大小排版，人工浏览才正常
        self._context = await self._pw.chromium.launch_persistent_context(
            PROFILE_DIR,
            headless=False,
            executable_path=CHROME_PATH,
            args=args,
            no_viewport=True,
        )

    async def _alive(self) -> bool:
        if self._context is None:
            return False
        try:
            # 探活用一次最轻的 CDP 调用；顺带能发现浏览器进程僵死（超时即视为没了）
            await asyncio.wait_for(self._context.cookies(), timeout=5)
            return True
        except Exception:
            return False

    async def _supervise(self) -> None:
        """轮询探活，发现浏览器没了就重开。

        不用 context 的 close 事件：事件丢了（进程僵死、管道断在半路）就再也
        不会重开。轮询一秒一次 CDP 调用，代价可以忽略，而且不依赖任何回调时序。
        """
        failures = 0
        while not self._stopping:
            await asyncio.sleep(HEALTH_INTERVAL)
            if await self._alive():
                continue

            self._context = None
            if self._stopping:
                return

            delay = min(RELAUNCH_DELAY * (2 ** failures), RELAUNCH_MAX_DELAY)
            _log(f"浏览器不可用，{delay:.0f}s 后重新拉起")
            await asyncio.sleep(delay)
            if self._stopping:
                return

            try:
                await self._launch()
            except Exception as exc:
                failures += 1
                _log_error(f"拉起浏览器失败（{type(exc).__name__}: {exc}）")
                if failures >= MAX_LAUNCH_FAILURES:
                    _log_error(
                        f"连续 {failures} 次失败，停止重试。"
                        "容器与 HTTP 服务仍在运行，重启容器可恢复"
                    )
                    return
            else:
                failures = 0
                _log("浏览器已就绪")
