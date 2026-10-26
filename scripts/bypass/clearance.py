"""冷路径：判断是否被 Cloudflare 拦截，以及用有头浏览器过挑战。

浏览器单实例 200-400MB，并发解挑战没有意义，所以全局串行。
"""

from __future__ import annotations

import sys
import threading

from .config import BLOCK_MARKERS, BLOCK_STATUSES, CHROME_PATH, PROXY, SOLVE_TIMEOUT
from .creds import CREDS
from .upstream import Reply

_solve_lock = threading.Lock()
_UNSET = object()
_pypasser = _UNSET

# 按 host 的"正在解挑战"登记表，用于把并发请求合并成一次
_solving: dict[str, threading.Event] = {}
_solving_lock = threading.Lock()


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

    if result is None or not result.is_solved or not result.cookies:
        return False
    CREDS.absorb(harvested, host)
    CREDS.set_agent(agent or result.user_agent)
    return True


def ensure_clearance(url: str, host: str) -> str | None:
    """同一 host 只解一次挑战。返回 solved（我解的）/ waited（等别人）/ None（失败）。

    并发请求会在几秒内全部撞上 403。如果各自排队解一遍，N 个请求就是
    N × 20s；让第一个负责解、其余等它并复用结果，N 个请求仍然是 20s。
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
        return "waited" if CREDS.get(host)[0] else None

    try:
        # 只有负责解的这个才清缓存，避免把别人刚拿到的 cookie 删掉
        CREDS.drop(host)
        return "solved" if solve_clearance(url, host) else None
    finally:
        with _solving_lock:
            _solving.pop(host, None)
        ev.set()
