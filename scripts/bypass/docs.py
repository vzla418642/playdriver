"""首页说明页。浏览器访问返回 HTML，脚本访问返回 JSON。

内容全部由 config 里的运行时值生成，改了环境变量刷新页面就能看到。
"""

from __future__ import annotations

from .config import (
    CHROME_PATH,
    COLD_ENABLED,
    IMPERSONATE,
    MAX_BODY,
    PORT,
    PROXY,
    mask_proxy,
)

ENV_DOC = [
    ("PROXY", "出站代理，浏览器与 curl_cffi 共用", "（空）"),
    ("BYPASS_PORT", "服务监听端口", "8000"),
    ("BYPASS_COLD", "设为 0 关闭冷路径，只做透传", "1"),
    ("BYPASS_TIMEOUT", "单次转发超时（秒）", "60"),
    ("BYPASS_SOLVE_TIMEOUT", "过挑战超时（秒）", "60"),
    ("BYPASS_MAX_BODY_MB", "响应体上限（MB）", "20"),
    ("BYPASS_IMPERSONATE", "curl_cffi 指纹模板", "chrome"),
    ("CHROME_PATH", "浏览器可执行文件，留空则自动探测", "自动"),
    ("BYPASS_COOKIE_TTL", "cookie 缓存秒数", "1500"),
    ("BYPASS_PASS_UA", "设为 1 则透传客户端 UA", "0"),
    ("BYPASS_LOG_LEVEL", "uvicorn 日志级别，warning 时不打印逐条请求", "info"),
]

HEADERS_DOC = [
    ("X-Bypass-URL", "本次转发的目标地址"),
    ("X-Bypass-Host", "目标站点 host"),
    ("X-Bypass-Mode", "direct 直连 / cache 命中缓存 / solved 本次过挑战 / "
                      "waited 等同 host 并发解完 / blocked 过完仍被拦"),
    ("X-Bypass-Ms", "本次转发耗时（毫秒）"),
]

_MAX_MB = MAX_BODY // (1024 * 1024)


def doc_json() -> dict:
    return {
        "service": "playdriver bypass",
        "usage": {
            "path_prefix": f"http://<host>:{PORT}/<完整目标URL>",
            "example": f"http://127.0.0.1:{PORT}/https://example.com/path?q=1",
            "header_form": {
                "method": "GET",
                "url": f"http://<host>:{PORT}/fetch",
                "headers": {"X-Target-URL": "https://example.com/path"},
            },
            "note": "method / headers / body 全部透传，任意 HTTP 客户端均可使用",
        },
        "response": "原样回传目标站点的 status / headers / body；重定向不跟随，"
                    "Location 会改写回本服务，因此客户端侧 r.history 可用",
        "diagnostics": dict(HEADERS_DOC),
        "runtime": {
            "port": PORT,
            "proxy": mask_proxy(PROXY),
            "cold_path": COLD_ENABLED,
            "impersonate": IMPERSONATE,
            "chrome": CHROME_PATH,
            "max_body_mb": _MAX_MB,
        },
        "env": [{"name": n, "desc": d, "default": v} for n, d, v in ENV_DOC],
    }


_DOC_CSS = """
*{box-sizing:border-box}
body{margin:0;padding:40px 24px;font:14px/1.7 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;color:#2c2c2a;background:#f7f7f5}
main{max-width:760px;margin:0 auto}
h1{font-size:20px;font-weight:600;margin:0 0 4px}
.sub{color:#5f5e5a;margin:0 0 20px}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:28px}
.chip{padding:4px 10px;border-radius:999px;background:#fff;border:1px solid #d3d1c7;font-size:12px;color:#444441}
.chip b{font-weight:600;color:#185fa5}
h2{font-size:13px;font-weight:600;text-transform:uppercase;letter-spacing:.06em;color:#888780;margin:28px 0 10px}
pre{background:#fff;border:1px solid #d3d1c7;border-radius:8px;padding:14px 16px;overflow-x:auto;margin:0 0 10px;font:13px/1.6 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
code{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
p code,td code{background:#f1efe8;padding:1px 5px;border-radius:4px;font-size:12.5px}
table{width:100%;border-collapse:collapse;background:#fff;border:1px solid #d3d1c7;border-radius:8px;overflow:hidden}
th,td{text-align:left;padding:9px 14px;border-bottom:1px solid #ece9e1;font-size:13px}
th{background:#f1efe8;font-weight:600;color:#5f5e5a}
tr:last-child td{border-bottom:none}
ul{margin:0;padding-left:20px}
li{margin-bottom:6px}
.note{color:#5f5e5a;font-size:13px}
"""


def doc_html() -> str:
    env_rows = "".join(
        f"<tr><td><code>{n}</code></td><td>{d}</td><td><code>{v}</code></td></tr>"
        for n, d, v in ENV_DOC
    )
    head_rows = "".join(f"<tr><td><code>{n}</code></td><td>{d}</td></tr>" for n, d in HEADERS_DOC)
    cold = "开启" if COLD_ENABLED else "关闭"
    base = f"http://127.0.0.1:{PORT}"
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>playdriver bypass</title><style>" + _DOC_CSS + "</style></head><body><main>"
        "<h1>playdriver bypass</h1>"
        '<p class="sub">把目标 URL 拼在服务地址后面，就能用普通 HTTP 客户端拿到绕过后 Cloudflare 的响应。</p>'
        '<div class="chips">'
        f'<span class="chip">端口 <b>{PORT}</b></span>'
        f'<span class="chip">冷路径 <b>{cold}</b></span>'
        f'<span class="chip">代理 <b>{mask_proxy(PROXY)}</b></span>'
        f'<span class="chip">指纹 <b>{IMPERSONATE}</b></span>'
        f'<span class="chip">上限 <b>{_MAX_MB}MB</b></span>'
        "</div>"
        "<h2>用法一 · 拼接</h2>"
        f"<pre>GET {base}/https://example.com/path?q=1</pre>"
        "<h2>用法二 · 请求头</h2>"
        f"<pre>GET {base}/fetch\nX-Target-URL: https://example.com/path</pre>"
        "<h2>客户端示例</h2>"
        f"<pre>import requests\nr = requests.get(\"{base}/https://example.com/\")\nr.status_code, r.text, r.json(), r.cookies</pre>"
        '<p class="note">method、headers、body 全部透传，任何 HTTP 客户端都能用，不限 requests。'
        "重定向不跟随，Location 会改写回本服务，所以 <code>r.history</code> 仍然可用。</p>"
        "<h2>诊断响应头</h2>"
        f"<table><thead><tr><th>Header</th><th>含义</th></tr></thead><tbody>{head_rows}</tbody></table>"
        "<h2>环境变量</h2>"
        f"<table><thead><tr><th>变量</th><th>说明</th><th>默认</th></tr></thead><tbody>{env_rows}</tbody></table>"
        "<h2>已知限制</h2><ul>"
        f"<li>响应体先完整缓冲再判断上限（{_MAX_MB}MB），超限返回 502。</li>"
        "<li>冷路径全局串行：一个有头 Chrome 约 200-400MB，并发过挑战没有意义。</li>"
        "<li><code>cf_clearance</code> 与出口 IP、UA 强绑定，代理换了就得重新过挑战。</li>"
        "<li>本页只在请求头带 <code>Accept: text/html</code> 时返回，脚本调用拿到的是 JSON。</li>"
        "</ul></main></body></html>"
    )
