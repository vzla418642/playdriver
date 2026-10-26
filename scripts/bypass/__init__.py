"""Playdriver 绕过服务。

把目标 URL 拼在服务地址后面即可用普通 HTTP 客户端调用：

    requests.get("http://127.0.0.1:8000/https://example.com/path?q=1")
    requests.post("http://127.0.0.1:8000/https://example.com/api", json={...})

服务端用 curl_cffi 以 Chrome 指纹转发，原样回传 status / headers / body，
响应附带 X-Bypass-* 诊断头。重定向不跟随，Location 会改写回本服务，
所以客户端侧的 r.history 依然可用。

两级路径：
  1. 快路径 —— 带缓存的 cf_clearance 用 curl_cffi 直连，约 0.1s。
  2. 冷路径 —— 被 Cloudflare 拦截时，用 PyPasser 驱动有头 patchright 过挑战，
               拿到 cf_clearance 与浏览器真实 UA 后重试一次。

模块划分（依赖单向，越靠下越底层）：
  app.py        FastAPI 应用、中间件、路由
  forward.py    两级路径编排
  docs.py       首页说明页（HTML / JSON 两种形态）
  clearance.py  拦截判定与冷路径过挑战
  inbound.py    入站请求解析：目标 URL、cookie 拆分、请求头构造
  upstream.py   出站转发：会话、Reply、连接错误
  creds.py      cookie 缓存与域匹配
  config.py     环境变量与常量

本包不含入口。进程入口统一在 scripts/run.py：转发服务默认就挂在 `/` 上，
resident 包只是在同一个 app 上多挂一个常驻浏览器和 `/cookies`。
"""

__all__ = ["app", "config", "creds", "upstream", "inbound", "clearance", "forward", "docs"]
