"""冷路径：判断是否被 Cloudflare 拦截，以及用有头浏览器过挑战。

浏览器单实例 200-400MB，并发解挑战没有意义，所以全局串行。

能力边界：**只处理整页拦截（interstitial）**。判据是"挑战的产物是不是一个
可以重放的 cookie"。整页墙发 cf_clearance，能搬；嵌入式 Turnstile widget 发
cf-turnstile-response token，由宿主页面自己 POST 给站点后端，代理层拿不到也
没法替客户端提交，所以这里不做——那类场景走常驻浏览器人工登录 + /cookies 导出。
"""

from __future__ import annotations

import sys
import threading
import time

from .config import (
    BLOCK_MARKERS,
    BLOCK_STATUSES,
    CHROME_PATH,
    CLEARANCE_COOKIE,
    PROXY,
    RETRY_COOLDOWN,
    SOLVE_TIMEOUT,
    UNSOLVABLE_MARKERS,
    WALL_MARKERS,
)
from .creds import CREDS
from .upstream import Reply

_solve_lock = threading.Lock()
_UNSET = object()
_pypasser = _UNSET

# 按 host 的"正在解挑战"登记表，用于把并发请求合并成一次
_solving: dict[str, threading.Event] = {}
_solving_lock = threading.Lock()

# 按 host 的失败负缓存：直到这个时间点之前不再尝试。
# 没有它的话，每个撞上拦截的请求都会重新起一次浏览器，一个页面几十个子资源
# 就是几十次 60 秒 + 400MB。
_failed: dict[str, float] = {}
# 硬封页的提示去重：同一 host 只打一行日志
_noted: set[str] = set()
_failed_lock = threading.Lock()


def cooling_down(host: str) -> bool:
    """这个 host 是否在失败冷却期内。"""
    with _failed_lock:
        until = _failed.get(host, 0.0)
        if until <= time.monotonic():
            _failed.pop(host, None)
            return False
        return True


def _mark_failed(host: str) -> None:
    with _failed_lock:
        _failed[host] = time.monotonic() + RETRY_COOLDOWN


def _clear_failed(host: str) -> None:
    with _failed_lock:
        _failed.pop(host, None)


def note_unsolvable(host: str) -> bool:
    """硬封页每个 host 只提示一次。返回 True 表示这次该打印。"""
    with _failed_lock:
        if host in _noted:
            return False
        if len(_noted) >= 500:
            _noted.clear()
        _noted.add(host)
        return True


def is_unsolvable(reply: Reply) -> bool:
    """确认是 CF 的硬封页（WAF 规则拦截），没有任何挑战可过。

    和挑战页分开是必须的：这类页面起浏览器过去也只是一个静态拒绝页，
    白烧一次 400MB 的浏览器。判据要求"有硬封特征且没有挑战特征"，避免
    某个挑战页恰好也带这些词时被误降级。
    """
    head = (reply.body or b"")[:8192].lower()
    if not any(marker in head for marker in UNSOLVABLE_MARKERS):
        return False
    return not any(marker in head for marker in WALL_MARKERS)


def looks_blocked(reply: Reply) -> bool:
    """这个响应是不是 Cloudflare 的拦截页。

    只看状态码 + 特征，不做重试；判断错了会影响是否值得启动浏览器。
    """
    if reply.status not in BLOCK_STATUSES:
        return False
    if any(k.lower() == "cf-mitigated" for k, _ in reply.headers):
        return True
    if not reply.body:
        # 过完挑战后紧跟的突发请求常被软封：403 + 空 body，且不带 cf-mitigated，
        # 只能靠 server: cloudflare 判断，否则会漏判成"业务 403"而不重试。
        server = next((v.lower() for k, v in reply.headers if k.lower() == "server"), "")
        return "cloudflare" in server
    head = reply.body[:8192].lower()
    return any(marker in head for marker in BLOCK_MARKERS)


def _load_pypasser():
    """首次用到时才导入 patchright / pypasser，缺依赖时不影响快路径。"""
    global _pypasser
    if _pypasser is _UNSET:
        try:
            from patchright.sync_api import sync_playwright
            from pypasser.antibot import cloudflare
            _pypasser = (sync_playwright, cloudflare)
        except Exception as exc:
            print(f"[bypass] error: 冷路径不可用（{type(exc).__name__}: {exc}）",
                  file=sys.stderr, flush=True)
            _pypasser = None
    return _pypasser


def solve_clearance(url: str, host: str) -> bool:
    """用有头 patchright 过 Cloudflare，把 cf_clearance 与真实 UA 写入缓存。"""
    loaded = _load_pypasser()
    if loaded is None:
        return False
    sync_playwright, cloudflare = loaded

    with _solve_lock:  # 有头浏览器单实例 200-400MB，并发解挑战没有意义
        args = ["--no-sandbox", "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled"]
        if PROXY:
            args.append(f"--proxy-server={PROXY}")

        result = None
        harvested: list[dict] = []
        agent: str | None = None
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(
                    headless=False, executable_path=CHROME_PATH, args=args
                )
                try:
                    page = browser.new_page()
                    page.goto(url, wait_until="domcontentloaded", timeout=SOLVE_TIMEOUT * 1000)
                    result = cloudflare.solve(page, timeout=SOLVE_TIMEOUT)
                    if result is not None and result.is_solved:
                        # 浏览器自己跟完了重定向链，上下文里可能已有同域其它主机的
                        # cookie；pypasser 只按 page.url 过滤，会丢掉它们，这里取全量。
                        try:
                            harvested = page.context.cookies()
                        except Exception:
                            harvested = []
                        agent = result.user_agent
                finally:
                    try:
                        browser.close()
                    except Exception:
                        pass
        except Exception as exc:
            print(f"[bypass] error: 过挑战失败（{type(exc).__name__}: {exc}）",
                  file=sys.stderr, flush=True)
            return False

    if result is None or not result.is_solved:
        # 这条以前是静默的：挑战没过去（超时 / 软封）就什么都不打，
        # 从日志完全看不出冷路径到底跑没跑过。
        print(
            f"[bypass] error: {host} 过挑战未成功"
            f"（{'没有返回结果' if result is None else f'{SOLVE_TIMEOUT:.0f}s 内挑战没有通过'}）",
            file=sys.stderr, flush=True,
        )
        return False

    if not harvested:
        # context.cookies() 极少失败，兜底用 pypasser 按 page.url 过滤的那份
        harvested = [
            {"name": name, "value": value, "domain": host}
            for name, value in (result.cookies or {}).items()
        ]

    if not any(c.get("name") == CLEARANCE_COOKIE for c in harvested):
        # 过了挑战却没有 cf_clearance，只有两种可能：
        #   1. IP 软封（数据中心 IP 常见）—— 再撞也是一样；
        #   2. 目标根本不是拦截页 —— pypasser 的 `not _wall_present` 早退分支
        #      会返回 is_solved=True，比如 goto 了一个返回 JSON 的接口。
        # 两种情况写进缓存都只会污染后续请求，必须挡住。
        print(
            f"[bypass] error: {host} 过挑战未拿到 {CLEARANCE_COOKIE}"
            "（IP 软封，或目标不是拦截页），不写入缓存",
            file=sys.stderr, flush=True,
        )
        return False

    CREDS.absorb(harvested, host)
    CREDS.set_agent(agent or result.user_agent)
    return True


def ensure_clearance(url: str, host: str) -> str | None:
    """同一 host 只解一次挑战。返回 solved（我解的）/ waited（等别人）/ None（失败）。

    并发请求会在几秒内全部撞上 403。如果各自排队解一遍，N 个请求就是
    N × 20s；让第一个负责解、其余等它并复用结果，N 个请求仍然是 20s。

    失败会写进负缓存（RETRY_COOLDOWN 秒），调用方应先查 cooling_down()。
    """
    with _solving_lock:
        ev = _solving.get(host)
        leader = ev is None
        if leader:
            ev = threading.Event()
            _solving[host] = ev

    if not leader:
        # 被等的那个可能还在排 _solve_lock（别的 host 在解），给两倍预算
        ev.wait(timeout=SOLVE_TIMEOUT * 2 + 10)
        return "waited" if CREDS.has_clearance(host) else None

    try:
        # 只有负责解的这个才清缓存，避免把别人刚拿到的 cookie 删掉
        CREDS.drop(host)
        solved = solve_clearance(url, host)
        if solved:
            _clear_failed(host)
        else:
            _mark_failed(host)
        return "solved" if solved else None
    finally:
        with _solving_lock:
            _solving.pop(host, None)
        ev.set()
