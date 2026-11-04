"""两级路径编排：先直连，被拦了才启动浏览器过挑战，然后重试一次。

这是整个服务唯一的"决策点"，排查行为异常先看这里。

`mode` 的语义（对外通过 x-bypass-mode 暴露）：
  direct      直连，手里没有任何可重放的凭据
  clearance   手里有 cf_clearance，这次是拿它换的
  solved      本次现解的挑战
  waited      同 host 有别人在解，等它解完复用
  blocked     被拦截，且没通过（解失败 / 冷却期内 / 解完仍被拦）
  unsolvable  Cloudflare 硬封页（WAF 规则），没有挑战可过，没起浏览器
"""

from __future__ import annotations

import sys
import time
from urllib.parse import urlsplit

from .clearance import (
    cooling_down,
    ensure_clearance,
    is_unsolvable,
    looks_blocked,
    note_unsolvable,
)
from .config import CLEARANCE_COOKIE, COLD_ENABLED
from .creds import CREDS
from .inbound import prepare_headers, split_cookie
from .upstream import do_request


def forward(method: str, url: str, headers: dict[str, str], body: bytes):
    """返回 (reply, 耗时毫秒, mode)。"""
    host = urlsplit(url).netloc
    started = time.perf_counter()

    # Cookie 只解析一次：冷路径重试时必须用客户端原始值，不能复用上一次合并后的结果
    base = dict(headers)
    client_cookies = split_cookie(base)

    reply = do_request(method, url, prepare_headers(host, base, client_cookies), body)
    # 判据必须是"有没有 cf_clearance"，不能是"jar 非空"：任何站点都会下发
    # session / 统计 cookie，那样 mode 会几乎永远报有缓存，诊断信号就废了。
    mode = "clearance" if CREDS.has_clearance(host) else "direct"

    if COLD_ENABLED and looks_blocked(reply):
        if is_unsolvable(reply):
            # WAF 硬封：起浏览器过去也只是一个静态拒绝页，白烧 400MB
            mode = "unsolvable"
            if note_unsolvable(host):
                print(f"[bypass] error: {host} 是 Cloudflare 硬封页（无挑战可过），"
                      "不启动冷路径", file=sys.stderr, flush=True)
        elif cooling_down(host):
            # 刚失败过，冷却期内如实报告，不再起浏览器
            mode = "blocked"
        else:
            outcome = ensure_clearance(url, host)
            if outcome:
                reply = do_request(method, url,
                                   prepare_headers(host, base, client_cookies), body)
                mode = outcome
                if looks_blocked(reply):
                    # 有 clearance 还被拦，通常是 IP 信誉 / 软封，重试也没用。
                    # 状态必须如实报成 blocked，不能让调用方以为已经通过。
                    mode = "blocked"
                    print(f"[bypass] error: {host} 已拿到 {CLEARANCE_COOKIE} 仍被拦截"
                          f"（{reply.status}），可能是 IP 软封",
                          file=sys.stderr, flush=True)
            else:
                # 具体原因 solve_clearance 已经打过一行，这里不重复刷
                mode = "blocked"

    CREDS.update(host, reply.cookies)
    return reply, (time.perf_counter() - started) * 1000, mode
