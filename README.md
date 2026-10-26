# Playdriver

Playdriver 是一个带**可视化桌面浏览器**的 Docker 运行环境：容器内跑 Xvfb + x11vnc + noVNC，提供一个可用浏览器远程查看的桌面（`1280x800`，显示器 `:99`），并预装 Google Chrome 与 Python 自动化栈。

默认同时启动两样东西：端口 `8000` 上的 **Cloudflare 绕过服务**（把目标 URL 拼在服务地址后面，普通 HTTP 客户端就能拿到过墙后的响应，不用改客户端代码，也不用手动处理 `cf_clearance`），以及 noVNC 桌面里的一个**常驻浏览器**——你自己点、验证自动过，`/cookies` 直接读它的 cookie。只想当纯转发后端时加 `--no-browser`。传入命令即可替换这一默认行为，改为运行你自己的脚本。

镜像内包含 Python 3.12、Google Chrome、Xvfb、x11vnc、noVNC，以及 `requests`、`patchright`、`nodriver`、`uiautomator2`、`curl_cffi`、`fastapi`、`uvicorn`、`PyPasser[vision]`。Chrome 路径固定为 `/usr/bin/google-chrome`。

## 快速开始

```sh
docker run --rm -p 6080:6080 -p 8000:8000 \
  -v playdriver-profile:/data/profile \
  ghcr.io/vic2ray/playdriver:latest
```

- `http://localhost:6080` —— noVNC 桌面，里面已经有一个开好的 Chrome，地址栏随便输
- `http://localhost:8000` —— 绕过服务：自述页面、转发、`/cookies` 都在这个端口上
- 页面停在 Cloudflare 拦截页上时，几秒内自动过掉，不用你点复选框

`-v playdriver-profile:/data/profile` 是浏览器 profile 的落盘位置，挂出来登录态才能跨容器保留；不挂也能跑，只是容器删了 cookie 就没了。

## 绕过服务

### 用法

把完整目标 URL 拼在服务地址后面即可，method、headers、body 全部透传：

```sh
curl "http://localhost:8000/https://example.com/path?q=1"
```

```python
import requests

r = requests.get("http://localhost:8000/https://www.nairaland.com/politics")
print(r.status_code, len(r.content), r.headers.get("x-bypass-mode"))
```

也可以用请求头指定目标，适合 URL 中带特殊字符、容易被客户端转义的场景：

```sh
curl -H "X-Target-URL: https://example.com/path" "http://localhost:8000/fetch"
```

任何 HTTP 客户端都能用，不限 `requests`；唯一差别是各客户端对重定向的默认策略不同。

### 响应

目标站点的 `status`、`headers`、`body` 原样回传。**重定向不跟随**，`Location` 会被改写成指向本服务，因此客户端的 `allow_redirects` 和 `r.history` 照常工作。

响应额外带四个诊断头：

| Header | 含义 |
| --- | --- |
| `X-Bypass-URL` | 本次转发的目标地址 |
| `X-Bypass-Host` | 目标站点 host |
| `X-Bypass-Mode` | `direct` 直连 / `cache` 命中缓存 / `solved` 本次过了挑战 / `waited` 等同 host 并发解完 / `blocked` 过完仍被拦 |
| `X-Bypass-Ms` | 本次转发耗时（毫秒） |

### 带登录态

客户端 `Cookie` 头原样透传，与缓存里的 `cf_clearance` 合并后一起发出，所以直接带上登录 cookie 就能访问需要登录的页面：

```python
r = requests.get(
    "http://localhost:8000/https://example.com/account",
    headers={"Cookie": "session=abc123; uid=42"},
)
```

合并规则：**同名 cookie 以客户端的为准**，唯一例外是 `cf_clearance`——它由服务自己过挑战产生，与出口 IP、UA 强绑定，客户端从自己浏览器复制来的那一个必然是别的环境下发的，用它覆盖只会继续被拦。

几点需要注意：

- 服务**不保存**客户端的 cookie，只缓存目标站点在响应里 `Set-Cookie` 的公共部分，所以多客户端之间不会串号。
- 请求的 `User-Agent` 会被替换成与 TLS 指纹匹配的 Chrome UA。若目标站点把登录态与 UA 绑定，可设 `BYPASS_PASS_UA=1` 透传，但这样 `cf_clearance` 可能失效。
- 站点在响应里轮换的 `Set-Cookie` 会随响应回传给客户端，但下一次请求仍以客户端自己带的值为准；如果登录态依赖服务端轮换，需要在客户端同步更新。

### 两级路径

| 路径 | 触发条件 | 典型耗时 |
| --- | --- | --- |
| 快路径 | 缓存里有该 host 可用的 cookie | 0.1–1s |
| 冷路径 | 目标返回 Cloudflare 挑战 | 10–30s |

快路径用 `curl_cffi` 以 Chrome 的 TLS 指纹直连，重放之前拿到的 `cf_clearance`。命中拦截时才走冷路径：用 PyPasser 驱动**有头** patchright 打开页面、过挑战，拿到 `cf_clearance` 与浏览器的真实 UA 后重试一次。冷路径的浏览器窗口在 noVNC 桌面里可见。

`cf_clearance` 与出口 IP、UA 强绑定，所以缓存里同时记着过挑战时的 UA，转发时优先使用它，避免指纹自相矛盾。

Cloudflare 发的 `cf_clearance` 通常是 `domain=.example.com` 的**域级 cookie**，缓存按 RFC 6265 的域规则匹配，所以 `example.com` 与 `www.example.com` 通常只过一次挑战。若站点下发的是 host-only cookie，则各子域仍需各自过。

**同一 host 的并发请求只会解一次挑战。** 并发场景下几个请求会在几秒内全部撞上 403，如果各自排队解一遍，N 个请求就是 N × 20s。现在第一个请求负责解，其余等待并复用结果——5 个并发从 90s+ 降到一次冷路径的耗时，等待的那些返回 `mode=waited`。

### 环境变量

| 变量 | 说明 | 默认 |
| --- | --- | --- |
| `PROXY` | 出站代理，浏览器与 `curl_cffi` 共用，支持 `http://` `https://` `socks5://` | 空 |
| `BYPASS_PORT` | 服务监听端口 | `8000` |
| `BYPASS_COLD` | 设为 `0` 关闭冷路径，只做透传 | `1` |
| `BYPASS_TIMEOUT` | 单次转发超时（秒） | `60` |
| `BYPASS_SOLVE_TIMEOUT` | 过挑战超时（秒） | `60` |
| `BYPASS_MAX_BODY_MB` | 响应体上限（MB） | `20` |
| `BYPASS_IMPERSONATE` | `curl_cffi` 指纹模板，如 `chrome`、`chrome131` | `chrome` |
| `CHROME_PATH` | 浏览器可执行文件，留空则自动探测（镜像内为 `/usr/bin/google-chrome`） | 自动 |
| `BYPASS_COOKIE_TTL` | cookie 缓存秒数，仅在响应不带 `expires` 时兜底 | `1500` |
| `BYPASS_PASS_UA` | 设为 `1` 则透传客户端 UA | `0` |
| `BYPASS_LOG_LEVEL` | uvicorn 日志级别，`warning` 时不打印逐条请求（`[resident]` 的事件日志不受影响） | `info` |

带代理启动：

```sh
docker run --rm -p 6080:6080 -p 8000:8000 \
  -e PROXY=http://192.168.1.10:7890 \
  ghcr.io/vic2ray/playdriver:latest
```

### 已知限制

- 响应体先完整缓冲再判断上限，超过 `BYPASS_MAX_BODY_MB` 返回 `502`，不是流式转发。
- 冷路径**全局串行**：一个有头 Chrome 占 200–400MB，同一时刻只解一个挑战。同一 host 的并发会合并成一次，不同 host 仍要排队。
- `cf_clearance` 绑定出口 IP 与 UA，换代理或换指纹都要重新过挑战。
- 跨注册域的重定向（例如跳到完全不同的域名）要各自过挑战，cookie 不跨域共享。
- 默认丢弃客户端的 `User-Agent`，改用与 TLS 指纹匹配的 Chrome UA；确需透传时设 `BYPASS_PASS_UA=1`。
- 服务无鉴权，任何能访问端口的人都能借它出网。只在本机使用时请绑定 `127.0.0.1`。

## 常驻浏览器

默认就开着。它和绕过服务在同一个进程、同一个端口上，一起提供三件事：

- noVNC 桌面里一个常驻的有头 Chrome，你自己点；
- 页面停在 Cloudflare 拦截页上时，几秒内自动过掉；
- `/cookies` 读这个浏览器里的 cookie。

### 和绕过服务的关系

绕过服务**原样挂在同一个端口的 `/` 上**，所以 `http://localhost:8000/https://example.com` 照旧可用，`/cookies` 只是额外多出来的一条路由。代码上就是 `resident/service.py` 组装了一个 app：先注册 `/cookies`，再把整个绕过服务挂到 `/`。

两边的浏览器是分开的：

| | 谁在用 | 生命周期 |
| --- | --- | --- |
| 常驻浏览器 | 你手动点，`/cookies` 读它 | 一直开着，窗口关掉自动重开 |
| 一次性浏览器 | 绕过服务撞上 403 时临时拉起 | 过完挑战立刻关掉 |

所以后台请求触发冷路径时，noVNC 桌面上会短暂出现第二个浏览器窗口，属于正常现象。

### /cookies

```sh
curl "http://localhost:8000/cookies?domain=example.com"
```

返回 cookie 数组，字段就是浏览器里的原样值：

```json
[{"name":"cf_clearance","value":"...","domain":".example.com","path":"/",
  "expires":1793002421,"httpOnly":true,"secure":true,"sameSite":"None"}]
```

`domain` 省略则返回全部；给了就按域匹配，父域下发的 cookie（`.example.com`）也会带上——`cf_clearance` 绝大多数就是这种域级 cookie。浏览器正在重开时返回 `503`。

### 生命周期

- 关掉最后一个窗口 → 1 秒内发现，1 秒后重开，profile 不变，登录态还在
- 只关一个标签页 → 什么都不会发生
- 结束会话 → `Ctrl+C` 或 `docker stop`
- 连续 5 次重开失败（比如 profile 被别的进程锁着）→ 停止重试，容器和 HTTP 服务仍在运行，重启容器可恢复
- 浏览器起不来（比如 Chrome 缺依赖）→ 不影响转发服务，`/cookies` 返回 `503` 并在日志里说明原因

### 只要转发服务

```sh
docker run --rm -p 8000:8000 ghcr.io/vic2ray/playdriver:latest \
  python -u scripts/run.py --no-browser
```

不起浏览器、不占那 200–400MB，其余完全一样（`/cookies` 返回 `503` 并说明原因）。这不是另一种模式，只是把浏览器那个组件摘掉，端口和 app 结构都不变。

### 环境变量

| 变量 | 说明 | 默认 |
| --- | --- | --- |
| `RESIDENT_PROFILE` | profile 目录，挂 volume 到宿主机才能跨容器保留登录态 | `/data/profile` |
| `RESIDENT_DETECT` | 挑战检测间隔（秒） | `2` |
| `RESIDENT_RETRY_COOLDOWN` | 同一地址过验证失败后多久内不再重试（秒） | `30` |

端口、代理、Chrome 路径、过挑战超时都复用绕过服务的配置：`BYPASS_PORT`、`PROXY`、`CHROME_PATH`、`BYPASS_SOLVE_TIMEOUT`。

**代理必须是同一个。** `cf_clearance` 绑定出口 IP，常驻浏览器和绕过服务走不同出口的话，这边过完的 cookie 到那边就用不了。

### 已知限制

- 常驻浏览器由 Playwright 以 `--remote-debugging-pipe` 持有，**不开 9222**，外部脚本连不进来。要远程驱动，自己启动一个带 `--remote-debugging-port` 的 Chrome（Chrome 136 起必须同时给 `--user-data-dir`，否则端口不会开）。
- 冷路径过挑战时会在同一块屏幕上再开一个窗口，可能抢走焦点。
- 常驻浏览器常占 200–400MB，同时触发冷路径时内存翻倍。
- 自动过验证只处理 Cloudflare 的整页拦截；登录页里独立的 Turnstile 组件不保证能过。

## 运行自己的脚本

将宿主机脚本目录挂载到 `/app/scripts`，再把脚本命令作为容器参数传入。传入命令会**替换**默认命令（转发服务 + 常驻浏览器）：

```sh
docker run --rm \
  -p 6080:6080 \
  -v "$PWD/scripts:/app/scripts" \
  ghcr.io/vic2ray/playdriver:latest \
  python -u scripts/my_script.py
```

Windows PowerShell 可这样挂载当前目录下的 `scripts` 文件夹：

```powershell
docker run --rm -p 6080:6080 `
  -v "${PWD}/scripts:/app/scripts" `
  ghcr.io/vic2ray/playdriver:latest `
  python -u scripts/my_script.py
```

容器默认工作目录是 `/app`。自有脚本应使用可视模式启动 Chrome（`headless=False`），并连接容器的 `DISPLAY=:99`。

注意这个挂载会**整个覆盖**镜像自带的 `scripts/`（里面有入口 `run.py` 和 `bypass/`、`resident/` 两个包），所以这种方式下只能跑你自己的脚本。想保留默认命令，把宿主机目录挂到别的路径，例如 `-v "$PWD/scripts:/app/mine"` 再跑 `python -u mine/my_script.py`。

镜像内的 Playwright 系驱动是 `patchright`（Playwright 的无检测分支，API 与 Playwright 一致，只有导入路径不同），镜像**不包含** `playwright` 包，`from playwright.sync_api import ...` 会直接 `ImportError`。示例：

```python
import time
from patchright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(
        headless=False,
        executable_path="/usr/bin/google-chrome",
        args=["--no-sandbox", "--disable-dev-shm-usage"],
    )
    page = browser.new_page()
    page.goto("https://example.com")
    time.sleep(30)  # 方便通过 noVNC 查看
    browser.close()
```

镜像也预装了 `nodriver`，可参考 `scripts/test/test_nodriver.py` 的启动方式。不要将脚本中的 `headless` 设为 `True`，否则 noVNC 桌面不会显示浏览器窗口。

### 容器内没有 CDP 端口

默认命令会预启动一个常驻浏览器（见[常驻浏览器](#常驻浏览器)），但它由 Playwright 以 `--remote-debugging-pipe` 持有，**不开放 CDP 调试端口**（`9222` 之类），外部脚本连不进来。你自己脚本启动的 Chrome 是另一个进程，随脚本退出而结束。

由此带来一个限制：noVNC 只映射容器内 `:99` 的画面。脚本在容器内启动的浏览器可以看到；如果脚本连接的是容器外的浏览器，那部分画面不经过本镜像，需要由那台机器自己提供查看方式。

## 交互调试

需要进入容器 shell 时，把要执行的命令设为 `bash`：

```sh
docker run --rm -it \
  -p 6080:6080 -p 8000:8000 \
  -v "$PWD/scripts:/app/scripts" \
  ghcr.io/vic2ray/playdriver:latest bash
```

在 shell 中可以手动运行 `python -u scripts/run.py`（转发 + 浏览器）或 `python -u scripts/run.py --no-browser`（只转发），也可以跑 `python -u scripts/my_script.py`。退出 shell 后容器停止并删除。

## 端口

| 容器端口 | 用途 |
| --- | --- |
| `6080` | noVNC 网页入口 |
| `5900` | 原始 VNC 连接（无密码）；仅在需要 VNC 客户端时映射 |
| `8000` | 绕过服务 + `/cookies`（默认命令） |

如果只在本机访问，建议限制宿主机绑定地址，避免把无密码 VNC 和免鉴权的绕过服务暴露到外部网络：

```sh
docker run --rm -p 127.0.0.1:6080:6080 -p 127.0.0.1:8000:8000 \
  ghcr.io/vic2ray/playdriver:latest
```

## 启动行为

容器启动时只等待 Xvfb（`:99`）就绪，随后立即执行命令；x11vnc 与 noVNC 在后台并行拉起，不阻塞命令。Xvfb 失败会立即退出，其余服务若未在 10 秒内就绪，只补一行错误、不影响命令执行。

所有错误都以 `[start.sh] error:` 开头输出到 stderr；启动成功时不输出任何日志。绕过服务同理，成功请求不打印日志，只有出错才输出。常驻浏览器启动时会打一行 `[resident] 浏览器已启动`，之后只在过验证、关窗重开这类事件上说话。

## 输入脚本到标准输入

也可以通过标准输入提供 Python 程序：

```sh
docker run --rm -i -p 6080:6080 \
  ghcr.io/vic2ray/playdriver:latest \
  python -u - < my_script.py
```

## 代码结构

进程入口只有一个：`scripts/run.py`，它和两个包平级。默认把绕过服务和常驻浏览器一起拉起，`--no-browser` 摘掉后者。

```
scripts/
├── run.py        ← 唯一入口：python -u scripts/run.py [--no-browser]
├── bypass/       纯转发服务，可独立运行
└── resident/     常驻浏览器，依赖 bypass
```

`bypass` 内部按职责分层，依赖单向（越往下越底层，不会反向引用）：

```
scripts/bypass/
├── app.py        FastAPI 应用、中间件、路由、响应头构造
├── forward.py    两级路径编排（唯一的决策点）
├── docs.py       首页说明页，HTML / JSON 两种形态
├── clearance.py  拦截判定与冷路径过挑战
├── inbound.py    入站请求解析：目标 URL、cookie 拆分、请求头合并
├── upstream.py   出站转发：会话、Reply、连接错误
├── creds.py      cookie 缓存与 RFC 6265 域匹配
└── config.py     环境变量与常量（只有这里读 os.getenv）
```

排查时按这个顺序看：行为异常看 `forward.py`，被拦没生效看 `clearance.py`，
cookie 不对看 `inbound.py` / `creds.py`，报错信息看 `upstream.py`。

`resident` 依赖单向指向 `bypass`（反过来没有引用，所以绕过服务仍可独立运行）：

```
scripts/resident/
├── service.py    服务编排：起浏览器、起 watcher、组 app、拉起 uvicorn
├── routes.py     /cookies 路由
├── watcher.py    挑战检测与过验证
├── browser.py    常驻浏览器的启动、关窗重开、cookie 读取
└── config.py     本模式独有的配置（profile 目录、检测间隔、重开策略）
```

常驻浏览器排查：窗口关掉没重开看 `browser.py` 的 `_supervise`，点了没自动过验证看 `watcher.py`，`/cookies` 返回不对看 `routes.py` 与 `browser.py` 的域过滤。

## 构建镜像

在包含 `Dockerfile` 的 `playdriver` 目录执行：

```sh
docker build -t playdriver:latest .
```

本仓库的 GitHub Actions workflow 在默认分支有任何改动（纯 Markdown 除外）时构建并推送 GHCR 镜像，也支持从 Actions 页面手动触发。推送的镜像名取决于 GitHub 仓库地址，格式为 `ghcr.io/<owner>/<repository>`；请根据仓库实际地址替换上文示例中的镜像名。
