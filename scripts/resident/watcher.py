"""挑战检测：用户点到 Cloudflare 拦截页时，自动把它过掉。

与 bypass 的分工：
  - bypass 是被动的，请求撞上 403 才启动一次性浏览器去解；
  - 这里是主动的，盯着常驻浏览器里的每一个标签页，谁停在拦截页上就解谁。

所以用户只需要"点"，不需要关心验证——点了跳转到挑战页，几秒后页面自己过去了。
"""

from __future__ import annotations

import asyncio
import sys
import time
from typing import Any

from bypass.config import SOLVE_TIMEOUT

from .browser import ResidentBrowser
from .config import DETECT_INTERVAL, DETECT_TIMEOUT, RETRY_COOLDOWN

_UNSET = object()
_asolve: Any = _UNSET

# 判定与 PyPasser 自己的 _wall_present 保持一致：它拿 page_source 找这几个标记，
# 这里在页面里直接查，省掉把整份 HTML 序列化出来的开销。
# 判错了也不致命——asolve 内部会再判一次，没墙就立刻返回，不会白等。
_DETECT_JS = """
() => {
  const markers = ['just a moment', 'checking your browser',
                   'enable javascript and cookies to continue'];
  const title = (document.title || '').toLowerCase();
  if (markers.some((m) => title.includes(m))) return true;

  if (document.querySelector(
      "iframe[src*='challenges.cloudflare.com'],"
      + ".cf-turnstile iframe,"
      + "iframe[title*='Cloudflare security challenge'],"
      + "#challenge-form, #challenge-running, #challenge-stage,"
      + "#cf-challenge-running, .cf-browser-verification")) {
    return true;
  }

  // 标题被站点改掉的拦截页（少见），退回看正文开头
  const text = (document.body && document.body.textContent || '').slice(0, 2000).toLowerCase();
  return markers.some((m) => text.includes(m));
}
"""


def _log(message: str) -> None:
    print(f"[resident] {message}", flush=True)


def _log_error(message: str) -> None:
    print(f"[resident] error: {message}", file=sys.stderr, flush=True)


def _short(url: str, limit: int = 120) -> str:
    """日志用。挑战页的 URL 后面挂着一长串 __cf_chl_rt_tk，打全了没法看。"""
    return url if len(url) <= limit else url[:limit] + "…"


def _load_asolve():
    """首次真要过验证时才导入 PyPasser。

    它会连带拖进 numpy / PIL 等视觉依赖，装坏了也只是"不能自动过验证"，
    手动点照样能用，不该让整个常驻服务起不来。
    """
    global _asolve
    if _asolve is _UNSET:
        try:
            from pypasser.antibot.cloudflare import asolve
            _asolve = asolve
        except Exception as exc:
            _log_error(f"自动过验证不可用（{type(exc).__name__}: {exc}），只能手动点")
            _asolve = None
    return _asolve


class Watcher:
    """轮询所有标签页，停在拦截页上的就地过掉。"""

    def __init__(self, browser: ResidentBrowser) -> None:
        self._browser = browser
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._cooldown: dict[str, float] = {}  # 地址 -> 允许再试的时间点

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="resident-watcher")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):
            pass
        self._task = None

    # ---- 内部 ----

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # 检测本身出问题不该让看守任务死掉，下一轮继续
                _log_error(f"挑战检测异常（{type(exc).__name__}: {exc}）")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=DETECT_INTERVAL)
            except asyncio.TimeoutError:
                continue

    async def _tick(self) -> None:
        context = self._browser.context
        if context is None:      # 浏览器正在重开
            return
        self._prune()
        for page in list(context.pages):
            if self._stop.is_set():
                return
            await self._check(page)

    def _prune(self) -> None:
        """冷却表按地址累积，攒到一定量才清一次过期项。"""
        if len(self._cooldown) < 200:
            return
        now = time.monotonic()
        self._cooldown = {url: until for url, until in self._cooldown.items() if until > now}

    async def _check(self, page: Any) -> None:
        if page.is_closed():
            return
        url = page.url or ""
        if not url.startswith(("http://", "https://")):
            return                       # about:blank、chrome-error:// 之类
        if time.monotonic() < self._cooldown.get(url, 0):
            return

        try:
            hit = await asyncio.wait_for(page.evaluate(_DETECT_JS), timeout=DETECT_TIMEOUT)
        except Exception:
            return                       # 正在导航、或弹了 JS 对话框，下一轮再说
        if hit:
            await self._solve(page, url)

    async def _solve(self, page: Any, url: str) -> None:
        """就地过验证。

        整个 watcher 是一个顺序循环，一次只解一个标签页，不需要额外的锁。
        """
        asolve = _load_asolve()
        if asolve is None or page.is_closed():
            return

        _log(f"检测到挑战，开始过验证：{_short(url)}")
        started = time.monotonic()
        try:
            result = await asolve(page, timeout=SOLVE_TIMEOUT)
        except Exception as exc:
            _log_error(f"过验证失败（{type(exc).__name__}: {exc}）：{_short(url)}")
            self._cooldown[url] = time.monotonic() + RETRY_COOLDOWN
            return

        if getattr(result, "is_solved", False):
            _log(f"过验证成功（{time.monotonic() - started:.1f}s）：{_short(url)}")
            self._cooldown.pop(url, None)
            return

        # 软封（数据中心 IP 常见）：页面过了但没下发 cf_clearance，再撞也是一样
        self._cooldown[url] = time.monotonic() + RETRY_COOLDOWN
        _log_error(f"过验证未成功，{RETRY_COOLDOWN:.0f}s 内不再重试同一地址：{_short(url)}")
