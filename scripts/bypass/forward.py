"""两级路径编排：先直连，被拦了才启动浏览器过挑战，然后重试一次。

这是整个服务唯一的"决策点"，排查行为异常先看这里。
"""

from __future__ import annotations

import sys
import time
from urllib.parse import urlsplit

from .clearance import ensure_clearance, looks_blocked
from .config import COLD_ENABLED
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
    mode = "cache" if CREDS.get(host)[0] else "direct"

    if COLD_ENABLED and looks_blocked(reply):
        outcome = ensure_clearance(url, host)
        if outcome:
            reply = do_request(method, url, prepare_headers(host, base, client_cookies), body)
            mode = outcome
        if looks_blocked(reply):
            # 过完挑战还被拦，通常是 IP 信誉或软封，重试也没用。但状态必须如实
            # 报成 blocked，不能让调用方以为已经通过。
            mode = "blocked"
            print(f"[bypass] error: {host} 过挑战后仍被拦截（{reply.status}）",
                  file=sys.stderr, flush=True)

    CREDS.update(host, reply.cookies)
    return reply, (time.perf_counter() - started) * 1000, mode
