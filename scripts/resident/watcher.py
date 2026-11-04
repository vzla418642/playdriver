"""挑战检测：用户点到 Cloudflare 拦截页时，自动把它过掉。

与 bypass 的分工：
  - bypass 是被动的，请求撞上 403 才启动一次性浏览器去解；
  - 这里是主动的，盯着常驻浏览器里的每一个标签页，谁停在拦截页上就解谁。

所以用户只需要"点"，不需要关心验证——点了跳转到挑战页，几秒后页面自己过去了。

同时处理整页拦截（interstitial）和嵌入式 Turnstile widget。两类东西
长得像、本质不同：

  - 整页墙：Cloudflare 拿走了整个页面，产物是 `cf_clearance` cookie；
  - 嵌入式 widget：站点自己的表单里放一个 `challenges.cloudflare.com` 的
    iframe，产物是 `cf-turnstile-response` token，由**宿主页面自己**提交给
    站点后端，不产生任何可重放的 cookie。

嵌入式 widget 使用 PyPasser Turnstile solver，只完成验证，不提交表单，
不提取或记录 token。已写入 response 的 widget 跳过；失败沿用地址冷却机制。
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
_turnstile_asolve: Any = _UNSET

# 判定与 PyPasser 自己的 _wall_present 保持一致：它拿 page_source 找这几个标记，
# 这里在页面里直接查，省掉把整份 HTML 序列化出来的开销。
# 判错了也不致命——asolve 内部会再判一次，没墙就立刻返回，不会白等。
#
# 返回三档而不是布尔：
#   'wall'   整页拦截，可以过（asolve 的适用场景）
#   'widget' 嵌入式 Turnstile，使用 turnstile.asolve 就地处理
#   ''       没有挑战
# 顺序很重要：先判墙。墙页里也有 widget 的 iframe，反过来判会把墙误降级成 widget。
_DETECT_JS = """
() => {
  const markers = ['just a moment', 'checking your browser',
                   'enable javascript and cookies to continue'];
  const title = (document.title || '').toLowerCase();
  if (markers.some((m) => title.includes(m))) return 'wall';

  // 整页墙自己的容器。这些 id/class 只在 Cloudflare 拿走整页时出现，
  // 正常页面里的嵌入式 widget 不会带。
  if (document.querySelector(
      '#challenge-form, #challenge-running, #challenge-stage,'
      + '#cf-challenge-running, .cf-browser-verification')) {
    return 'wall';
  }

  // 标题被站点改掉的拦截页（少见），退回看正文开头
  const text = (document.body && document.body.textContent || '').slice(0, 2000).toLowerCase();
  if (markers.some((m) => text.includes(m))) return 'wall';

  // 嵌入式 widget：宿主页面里的 turnstile iframe / 容器。
  // 注意 .cf-turnstile 是容器，iframe 是它注入的，两者命中其一即可。
  if (document.querySelector(
      "iframe[src*='challenges.cloudflare.com'],"
      + ".cf-turnstile,"
      + "iframe[title*='Cloudflare security challenge']")) {
    return 'widget';
  }

  return '';
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


def _load_turnstile_asolve():
    """与整页 solver 一样延迟导入，依赖故障不阻止常驻服务启动。"""
    global _turnstile_asolve
    if _turnstile_asolve is _UNSET:
        try:
            from pypasser.captcha.turnstile import asolve
            _turnstile_asolve = asolve
        except Exception as exc:
            _log_error(f"Turnstile 自动验证不可用（{type(exc).__name__}: {exc}），请手动处理")
            _turnstile_asolve = None
    return _turnstile_asolve


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
        if len(self._cooldown) >= 200:
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
            kind = await asyncio.wait_for(page.evaluate(_DETECT_JS), timeout=DETECT_TIMEOUT)
        except Exception:
            return                       # 正在导航、或弹了 JS 对话框，下一轮再说

        if kind == "wall":
            await self._solve(page, url)
        elif kind == "widget":
            await self._solve_turnstile(page, url)

    async def _solve_turnstile(self, page: Any, url: str) -> None:
        """就地完成 widget 验证；token 留给宿主页面，不提交表单。"""
        if page.is_closed():
            return
        try:
            # 验证完成后容器和 iframe 仍可能存在，不要每轮重新点击。
            ready = await asyncio.wait_for(page.evaluate("""() => {
                const responses = [...document.querySelectorAll(
                    'input[name="cf-turnstile-response"]')];
                return responses.length > 0 && responses.every((el) => el.value.trim());
            }"""), timeout=DETECT_TIMEOUT)
        except Exception:
            return  # 导航或页面关闭，下一轮重新检测
        if ready:
            return

        asolve = _load_turnstile_asolve()
        if asolve is None:
            return
        _log(f"检测到嵌入式 Turnstile，开始验证：{_short(url)}")
        started = time.monotonic()
        try:
            result = await asolve(page, timeout=SOLVE_TIMEOUT)
        except Exception as exc:
            _log_error(f"Turnstile 验证失败（{type(exc).__name__}: {exc}）：{_short(url)}")
            self._cooldown[url] = time.monotonic() + RETRY_COOLDOWN
            return

        if getattr(result, "is_solved", False):
            _log(f"Turnstile 验证成功（{time.monotonic() - started:.1f}s）：{_short(url)}")
            self._cooldown.pop(url, None)
            return
        self._cooldown[url] = time.monotonic() + RETRY_COOLDOWN
        _log_error(f"Turnstile 验证未成功，{RETRY_COOLDOWN:.0f}s 内不再重试同一地址：{_short(url)}")

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
