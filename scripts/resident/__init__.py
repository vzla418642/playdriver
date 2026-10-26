"""Playdriver 常驻浏览器：可人工操作 + 自动过验证。

由 scripts/run.py 拉起（默认就开，`--no-browser` 可关），和转发服务同一个进程、
同一个端口，同时提供三件事：

  1. 一个常驻的有头 Chrome，窗口在 noVNC 桌面里，用户自己点；
  2. 自动过验证 —— 页面停在 Cloudflare 拦截页上时，watcher 几秒内解掉；
  3. /cookies —— 读这个浏览器里的 cookie，转发服务照常在同一端口上工作。

模块划分（依赖单向）：
  service.py    服务编排：起浏览器、起 watcher、组 app、拉起 uvicorn
  routes.py     /cookies 路由
  watcher.py    挑战检测与过验证
  browser.py    常驻浏览器的启动、关窗重开、cookie 读取
  config.py     本模式独有的配置（profile 目录、检测间隔、重开策略）

端口、代理、Chrome 路径、过挑战超时都复用 bypass 的配置：两边必须是同一个
出口，否则常驻浏览器里拿到的 cf_clearance 到转发路径上用不了。

浏览器由 Playwright 以 --remote-debugging-pipe 方式持有，**不开放 9222**，
外部脚本无法连进来；要远程驱动请自己按 README 的示例启动带端口的 Chrome。
"""

__all__ = ["browser", "config", "routes", "watcher"]
