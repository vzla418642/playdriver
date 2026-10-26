"""cookie 缓存。

按 cookie 的真实 domain 存，遵循 RFC 6265 的域匹配规则，所以裸域与子域
能共享同一个 cf_clearance。过挑战时的 UA 也一并记着，二者必须成对使用。
"""

from __future__ import annotations

import threading
import time

from .config import COOKIE_TTL


def domain_chain(host: str) -> list[str]:
    """由宽到窄列出 host 的所有父域：a.b.com -> [b.com, a.b.com]。"""
    parts = host.split(".")
    return [".".join(parts[i:]) for i in range(len(parts) - 2, -1, -1)]


def related(domain: str, host: str) -> bool:
    """是否同一注册域。用来滤掉 .doubleclick.net 这类第三方 cookie。"""
    return host == domain or host.endswith("." + domain) or domain.endswith("." + host)


class CredStore:
    """按 cookie 的真实 domain 存，遵循 RFC 6265 的域匹配。

    Cloudflare 发的 cf_clearance 绝大多数是 domain=.example.com 的域级 cookie，
    对裸域和所有子域同时有效。早先按 host 字符串精确匹配，访问过 www.example.com
    之后再访问 example.com 会白跑一次冷路径——cookie 本来就能用。
    """

    def __init__(self):
        self._host: dict[str, dict[str, tuple[str, float]]] = {}    # host-only
        self._domain: dict[str, dict[str, tuple[str, float]]] = {}  # 域级，含子域
        self._agent: str | None = None
        self._lock = threading.Lock()

    def get(self, host: str) -> tuple[dict[str, str], str | None]:
        now = time.time()
        with self._lock:
            jar: dict[str, str] = {}
            for dom in domain_chain(host):  # 宽到窄，窄的覆盖宽的
                for name, (value, exp) in self._domain.get(dom, {}).items():
                    if exp > now:
                        jar[name] = value
            for name, (value, exp) in self._host.get(host, {}).items():
                if exp > now:
                    jar[name] = value
            return jar, self._agent

    def update(self, host: str, cookies=None) -> None:
        """快路径回包的 cookie：不知道 domain，只能按 host-only 存。"""
        if not cookies:
            return
        now = time.time()
        with self._lock:
            slot = self._host.setdefault(host, {})
            for name, value in cookies:
                slot[str(name)] = (str(value), now + COOKIE_TTL)

    def absorb(self, raw: list[dict], host: str) -> None:
        """冷路径：浏览器上下文的全量 cookie，按各自的 domain 存。

        浏览器会自己跟完重定向链，上下文里往往已经有同域其它主机的 cookie；
        pypasser 只按 page.url 过滤，会把它们丢掉，所以这里自己取全量。
        """
        now = time.time()
        with self._lock:
            for item in raw:
                raw_domain = str(item.get("domain") or "")
                name = item.get("name")
                dom = raw_domain.lstrip(".").lower()
                if not dom or not name or not related(dom, host):
                    continue
                bucket = self._domain if raw_domain.startswith(".") else self._host
                bucket.setdefault(dom, {})[str(name)] = (
                    str(item.get("value", "")), now + COOKIE_TTL
                )

    def set_agent(self, agent: str | None) -> None:
        # cf_clearance 与 UA 绑定；同一份 Chrome 二进制，全局一个值就够
        if agent:
            with self._lock:
                self._agent = agent

    def drop(self, host: str) -> None:
        with self._lock:
            self._host.pop(host, None)
            for dom in domain_chain(host):
                self._domain.pop(dom, None)


CREDS = CredStore()
