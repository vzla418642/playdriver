# Playdriver

Playdriver 是一个带**可视化桌面浏览器**的 Docker 运行环境：容器内跑 Xvfb + x11vnc + noVNC，提供一个可用浏览器远程查看的桌面（`1280x800`，显示器 `:99`），并预装 Google Chrome 与 Python 自动化栈。

默认同时启动三样东西：端口 `8000` 上的 **Cloudflare 绕过服务**（把目标 URL 拼在服务地址后面，普通 HTTP 客户端就能拿到过墙后的响应，不用改客户端代码，也不用手动处理 `cf_clearance`）、noVNC 桌面里的一个**常驻浏览器**（你自己点、验证自动过，`/cookies` 直接读它的 cookie）、以及同一个端口 `/adbs` 上的**手机画面监控台**（把 adb 里的模拟器/真机画面推成 MJPEG，能点、能滑、能直接打字）。桌面画面本身也在同一个端口的 `/vnc` 上，所以**看浏览器、看手机都只需要 `8000` 一个端口**。只要转发后端时加 `--no-browser --no-ui`，只摘常驻浏览器用 `--no-browser`，只摘画面监控用 `--no-ui`，只摘桌面画面用 `--no-vnc`。传入命令即可替换这一默认行为，改为运行你自己的脚本。

镜像内包含 Python 3.12、Google Chrome、Xvfb、x11vnc、noVNC，以及 `requests`、`patchright`、`nodriver`、`uiautomator2`、`curl_cffi`、`fastapi`、`uvicorn`、`PyPasser[vision]`。Chrome 路径固定为 `/usr/bin/google-chrome`。

## 快速开始

```sh
docker run --rm -p 8000:8000 \
  -v playdriver-profile:/data/profile \
  ghcr.io/vic2ray/playdriver:latest
```

- `http://localhost:8000/vnc` —— noVNC 桌面，里面已经有一个开好的 Chrome，地址栏随便输（打开即连，画面跟着窗口缩放）
- `http://localhost:8000` —— 绕过服务：自述页面、转发、`/cookies` 都在这个端口上
- `http://localhost:8000/adbs` —— 手机画面监控台：adb 里连着的设备画面 + 点滑按键 / 打字
- `http://localhost:6080` —— 同一个桌面的老入口（`websockify --web`），可以不用，但**进程不能删**（`/vnc` 的 WS 就是反代到它）
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
docker run --rm -p 8000:8000 \
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

绕过服务**原样挂在同一个端口的 `/` 上**，所以 `http://localhost:8000/https://example.com` 照旧可用，`/vnc`、`/cookies`、`/adbs` 只是额外多出来的三条。代码上就是 `scripts/routes.py` 往 app 上装自家接口，`resident/service.py` 最后再把整个绕过服务挂到 `/`。

**顺序是这个项目唯一的硬约束**：绕过服务的路由是 `/{target:path}` 全捕获，任何在它之后注册的路径都会被当成目标地址去转发（`/cookies` 会被理解成目标 `cookies`，`/adbs/api/devices` 会被理解成目标 `/adbs/api/devices`）。所以所有自家接口都集中在 `routes.py` 一个文件里、先于 `mount("/")` 装配，散落到各个包里最容易漏。

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

### 摘掉常驻浏览器

```sh
docker run --rm -p 8000:8000 ghcr.io/vic2ray/playdriver:latest \
  python -u scripts/run.py --no-browser
```

不起浏览器、不占那 200–400MB，`/cookies` 返回 `503` 并说明原因；转发和 `/adbs` 照常。这不是另一种模式，只是把浏览器那个组件摘掉，端口和 app 结构都不变。

### 环境变量

| 变量 | 说明 | 默认 |
| --- | --- | --- |
| `RESIDENT_PROFILE` | profile 目录，挂 volume 到宿主机才能跨容器保留登录态 | `/data/profile` |
| `RESIDENT_DETECT` | 挑战检测间隔（秒） | `2` |
| `RESIDENT_RETRY_COOLDOWN` | 同一地址过验证失败后多久内不再重试（秒） | `30` |

端口、代理、Chrome 路径、过挑战超时都复用绕过服务的配置：`BYPASS_PORT`、`PROXY`、`CHROME_PATH`、`BYPASS_SOLVE_TIMEOUT`。

## 桌面画面（/vnc）

`http://localhost:8000/vnc` 就是 noVNC 桌面，不用再换端口去看 6080。打开即连（落地页带 `autoconnect=1&resize=scale`），画面跟着窗口缩放。

### 怎么实现的：静态文件自己发，WebSocket 甩给 6080

noVNC 是**两样东西**：

1. `/usr/share/novnc` 里的一堆静态文件（`vnc.html` + `app/` + `core/` + `vendor/`）；
2. 一条 **WebSocket 通道** —— 前端把 RFB 字节塞进 WS 帧发出来，服务端再转成裸 TCP 喂给 x11vnc。

第 1 件用 `StaticFiles` 直接发。第 2 件**不自己实现**：`start.sh` 里的 `websockify --web /usr/share/novnc 6080 localhost:5900` 本来就在干这件事，而且它经过验证是好的。所以 `scripts/vnc.py` 只做一件事——把浏览器的 `/websockify` 反向代理到 `ws://127.0.0.1:6080/websockify`，字节原样搬运。

自己写 WS ↔ TCP 桥那条路试过，代价是要自己承担 RFB 的全部语义和排障：上游一沉默就变成「WS 连上了、日志一片安静、画面全黑」，根本无从定位。**6080 好好的就别重造**，转发一层即可。

三条坑，都写进 `vnc.py` 的注释了：

- **noVNC 不知道自己在子路径下**（这条最容易漏，线上表现是 `WebSocket /websockify 403`）。它拼地址的代码是 `url += '/' + path`，`path` 默认 `'websockify'` —— **永远钉在站点根上，不跟页面目录走**。页面在 `/vnc/vnc.html`，它照样去连 `/websockify`。所以根上必须有一条 `/websockify` 反代路由（`/vnc/websockify` 是同一路的别名，方便手工用 `?path=` 指定时也能连）。
- **WS 路由必须早于 `Mount`**。`Mount("/vnc", StaticFiles)` 对 `/vnc/websockify` 也会给出 FULL 匹配，而 `StaticFiles.__call__` 第一行是 `assert scope["type"] == "http"`，WS 请求进去直接 AssertionError。Starlette 遇到 FULL 立刻收工，谁先注册谁生效。
- **`/vnc` 自己接不住**。`Mount` 的正则是 `^/vnc/(?P<path>.*)$`，`/vnc` 不匹配，会掉进转发全捕获被当成目标地址 `vnc` 返回 400。所以补一条 307 重定向到落地页。

### 画面出不来时看哪里

转发是黑盒，所以它会自己说话（正常会话完全静默，只在出问题时打一行 `[vnc] WARNING`）：

| 日志 | 含义 |
| --- | --- |
| `上游 ws://127.0.0.1:6080/websockify 连不上（ConnectionRefusedError…）` | 6080 上的 websockify 没起来（连带看 x11vnc） |
| `会话中断：连上了但 5 秒没收到任何数据（上行 N 字节 / 下行 0 字节）` | WS 通了但不说话，上游不正常 |
| `会话中断：上游断开（ConnectionClosed…）（上行 N / 下行 M）` | 会话建立过，中途断了 |

上游不通时浏览器拿到的是**握手失败**（HTTP 403），不是"假装连上再莫名断开"——后者在 noVNC 里只会显示 `Connecting...` 然后静默重连，最难查。上游先断时用 **1011** 关闭，同样是为了让 noVNC 别再静默重连。

先确认 6080 本身是好的（它和 `/vnc` 是同一路）：

```sh
curl -sI http://localhost:6080/vnc.html | head -1   # 期望 200
```

### 环境变量

| 变量 | 说明 | 默认 |
| --- | --- | --- |
| `NOVNC_DIR` | noVNC 静态文件目录 | `/usr/share/novnc` |
| `VNC_TARGET` | WS 反代目标（websockify 的 host:port，不是 x11vnc 的 5900） | `127.0.0.1:6080` |
| `NOVNC_LANDING` | 落地页（相对 `/vnc`） | `vnc.html?autoconnect=1&resize=scale` |

宿主机上跑（没有 `/usr/share/novnc`）时 `/vnc` 自动降级成 `503` 并说明原因，不影响转发和 `/adbs`。想换成极简界面（没有侧边栏，只剩画面）就设 `NOVNC_LANDING=vnc_lite.html?scale=true` —— 注意 `vnc_lite.html` 的参数是 `scale=true`（不是 `resize=scale`），而且它加载即连，没有 `autoconnect` 开关。

日志里那条 `GET /vnc/package.json 404` 是浏览器扩展在探测版本，noVNC 本身不依赖它，可以不管。

### 6080 现在是 `/vnc` 的依赖

`/vnc` 把 WS 反代到 6080，所以 **`start.sh` 里那行 websockify 不能删**。两个入口是同一份内容：静态文件同一份，WS 同一路。不映射 `-p 6080:6080` 也行（只在容器内部用），但进程得留着。

## 手机画面监控（/adbs）

挂在同一个端口的 `/adbs` 上，把 adb server 里的设备画面推成 MJPEG，并且能远程操作。**它不去建立连接**——设备表是从 adb server 只读来的（`host:devices` / `host:track-devices`），u2、kumuapp、vkapp 谁把设备连上，这里就出现谁，`connect` / `disconnect` / `forward` 一概不碰。

打开 `http://localhost:8000/adbs` 就能用：

| 操作 | 怎么做 |
| --- | --- |
| 看某台设备 | 点网格里的卡片，进入单机模式，画面撑满高度 |
| 点击 / 长按 | 在画面上点 / 按住 0.5 秒以上 |
| 滑动 | 按住拖 |
| 返回 / Home / 最近 / 电源 | 屏幕右侧竖排的按钮 |
| 打字 | **直接敲键盘**，不用点任何输入框（中文也行） |
| 退出 | `Esc` 或右上角「✕ 退出」 |

打字是停顿 250ms 自动发一批，回车先发文字再敲回车键。触屏设备上不抢焦点（会弹软键盘挡住画面），改用底部「键盘」按钮手动叫出。

### 两条输入通道

不是二选一，是按实测延迟分工：

| 操作 | 走哪条 | 实测 |
| --- | --- | --- |
| 点击 / 滑动 / 按键 | `adb shell input` | 43 / 353 / 39 ms |
| 文字 | 设备上 ATX agent 的剪贴板 `setClipboard` + `pasteClipboard` | ~40–145 ms |

agent 的 `click` / `swipe` / `pressKey` 慢 3~25 倍，因为 uiautomator 的 `InteractionController` 每次注入前后都 `waitForIdle()`。反过来，`input text` **只认 ASCII**，中文和 emoji 根本打不出来，必须走剪贴板粘贴。

### 设备上的 agent

抓帧和打字都依赖设备上的 ATX agent（uiautomator2 server，端口 `9008`）。它是 `app_process` 起的子进程，**谁起的谁负责活着**——连接一断它就退出。所以：

- 宿主机上 u2 脚本（kumuapp / vkapp）跑着的时候，直接用它那条；
- 没有的话 uni2ui 自己拉一个（命令和 u2 内部完全一致，不依赖 `uiautomator2` 这个包）；
- 前提是设备上已经有 `/data/local/tmp/u2.jar`（装过一次 u2 就有），没有就退回 `screencap`，画面掉到约 1fps。

一个进程只持有一条 agent 连接，抓帧和打字共用。

### 资源占用

实测（单台设备）：

| 状态 | 主机 CPU | 内存 |
| --- | --- | --- |
| 没有设备，纯挂着 | 0.00% | 60 MB |
| 设备在线，没人看 | 0.00% | 60 MB |
| 正在推流（15fps） | 1.9–2.8% 单核 | 61 MB |

关掉页面 30 秒后自动停止抓帧、回落到 0%。真正的成本是带宽：单台约 1.2–2.2 MB/s，而且**网格模式会把所有在线设备同时推流**。

### 只要转发服务

```sh
docker run --rm -p 8000:8000 ghcr.io/vic2ray/playdriver:latest \
  python -u scripts/run.py --no-browser --no-ui
```

四个组件的开关，默认全开（`/vnc` 是桌面画面，其余三个各对应一块能力）：

| 命令 | 转发 | 桌面画面 `/vnc` | 常驻浏览器 | 手机画面 `/adbs` |
| --- | --- | --- | --- | --- |
| `run.py` | ✓ | ✓ | ✓ | ✓ |
| `run.py --no-vnc` | ✓ | | ✓ | ✓ |
| `run.py --no-browser` | ✓ | ✓ | | ✓ |
| `run.py --no-ui` | ✓ | ✓ | ✓ | |
| `run.py --no-browser --no-ui` | ✓ | ✓ | | |
| `run.py --no-browser --no-ui --no-vnc` | ✓ | | | |

少哪个组件就少哪块能力，端口和 app 结构完全不变：`/vnc` 返回 `503`（`--no-vnc`），`/cookies` 返回 `503`（`--no-browser`），`/adbs` 也返回 `503` 并说明是 `--no-ui` 关掉的。

### 环境变量

| 变量 | 说明 | 默认 |
| --- | --- | --- |
| `UNI2UI_ADB_HOST` | adb server 地址 | `127.0.0.1` |
| `UNI2UI_ADB_PORT` | adb server 端口 | `5037` |
| `UNI2UI_FPS` | 推流帧率 | `15` |
| `UNI2UI_JPEG_QUALITY` | JPEG 质量 | `60` |
| `UNI2UI_TOKEN` | 访问口令，设了之后所有接口要带 `?token=` | 空（不校验） |
| `UNI2UI_PREWARM` | 设备上线就提前拉起 agent | `1` |
| `UNI2UI_IDLE_TIMEOUT` | 没人看多久后停抓帧（秒） | `30` |

容器里要真能看到设备，还需要：能连到一个 adb server（见下一节，`--network host` 或 `UNI2UI_ADB_HOST=host.docker.internal`）、设备上已有 `u2.jar`。这些不做也不影响启动——页面会空着，CPU 0%，只是没有设备。

### 设备从哪来：只认 adb server，不认镜像和 u2

**设备表是 adb server 进程的内存状态**，不落盘、不共享。`/adbs` 只读它的 `host:devices` / `host:track-devices`，所以"谁把设备连上的"完全不重要 —— u2、`adb connect`、kumuapp、你自己的脚本，只要连的是**同一个 adb server**，设备表里就会出现。

反过来说：**两个容器 = 两个网络命名空间 = 两个 `127.0.0.1:5037` = 两张互不相干的设备表**。你在另一个容器里 `adb connect 192.168.1.5:5555`，这边看不到，除非让它们指向同一个 server：

| 做法 | 怎么做 | 代价 |
| --- | --- | --- |
| 同一个容器（最省事） | 脚本和 `run.py` 一起跑，或干脆在容器里 `adb connect` | 无 |
| 都走宿主机网络 | 两个容器都加 `--network host` | Linux 才行；端口不再隔离 |
| 第二个容器指过来 | `-p 5037:5037`，对方设 `ANDROID_ADB_SERVER_HOST=<本容器>`（u2/adbutils）或 `ADB_SERVER_SOCKET=tcp:<本容器>:5037`（adb CLI） | server 必须以 `adb -a` 启动（默认只听 `127.0.0.1`） |
| 本容器指向宿主机 | `UNI2UI_ADB_HOST=host.docker.internal`（或宿主机 IP） | 宿主机 server 也要 `-a` |

三个环境变量别混：**uni2ui 只认 `UNI2UI_ADB_HOST` / `UNI2UI_ADB_PORT`**（它显式传 `AdbClient(host, port)`，不走环境变量默认值）；u2 / adbutils 认 `ANDROID_ADB_SERVER_HOST` / `ANDROID_ADB_SERVER_PORT`；`adb` 命令行认 `ADB_SERVER_SOCKET`。

**还有个前提：镜像里没有任何东西会启动 adb server。** Dockerfile 没装 `adb` 包，`adbutils` 只连不起（没有 start-server 接口，只有 `server_kill`），u2 同理。所以容器里得先有人起一次，可以直接用 adbutils 自带的二进制：

```sh
$(python -c "import adbutils; print(adbutils.adb_path())") start-server
# 或者对外暴露，让别的容器连进来：
$(python -c "import adbutils; print(adbutils.adb_path())") -a nodaemon server
```

没起 server 时 `/adbs` 页面是空的，日志里会周期性出现 `track_devices 中断（ConnectionRefusedError...），Ns 后重连` —— 这是退避重连，不是崩了。

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

注意这个挂载会**整个覆盖**镜像自带的 `scripts/`（里面有入口 `run.py`、`vnc.py` 和 `bypass/`、`resident/`、`uni2ui/` 三个包），所以这种方式下只能跑你自己的脚本。想保留默认命令，把宿主机目录挂到别的路径，例如 `-v "$PWD/scripts:/app/mine"` 再跑 `python -u mine/my_script.py`。

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
  -p 8000:8000 \
  -v "$PWD/scripts:/app/scripts" \
  ghcr.io/vic2ray/playdriver:latest bash
```

在 shell 中可以手动运行 `python -u scripts/run.py`（四个组件全开）、`--no-browser`（不要浏览器）、`--no-ui`（不要画面监控）或 `--no-vnc`（不要桌面画面），也可以跑 `python -u scripts/my_script.py`。退出 shell 后容器停止并删除。

## 端口

| 容器端口 | 用途 | 需要映射吗 |
| --- | --- | --- |
| `8000` | 绕过服务 + `/vnc` + `/cookies` + `/adbs`（默认命令） | **需要，只映射这一个就够** |
| `6080` | noVNC 老入口（`websockify --web`），也是 `/vnc` 的 WS 上游 | 不需要映射，但容器内必须留着 |
| `5900` | 原始 VNC 连接（无密码） | 只在用宿主机 VNC 客户端时映射 |

`/vnc` 里的 WebSocket 桥是服务进程自己去连 `127.0.0.1:5900`，所以宿主机完全不用碰 `5900`，容器也不需要 `--network host`。

如果只在本机访问，建议限制宿主机绑定地址，避免把无密码 VNC 和免鉴权的绕过服务暴露到外部网络：

```sh
docker run --rm -p 127.0.0.1:8000:8000 \
  ghcr.io/vic2ray/playdriver:latest
```

## 启动行为

容器启动时只等待 Xvfb（`:99`）就绪，随后立即执行命令；x11vnc 与 noVNC 在后台并行拉起，不阻塞命令。Xvfb 失败会立即退出，其余服务若未在 10 秒内就绪，只补一行错误、不影响命令执行。

所有错误都以 `[start.sh] error:` 开头输出到 stderr；启动成功时不输出任何日志。绕过服务同理，成功请求不打印日志，只有出错才输出。常驻浏览器启动时会打一行 `[resident] 浏览器已启动`，手机画面监控会打一行 `[uni2ui] 手机画面监控已启动，路径 /adbs`（`/vnc` 不额外打印，找不到 noVNC 静态文件时才 warning），之后都只在出错时说话（uni2ui 的日志被压在 `WARNING` 以上，info 级别全静音）。

## 输入脚本到标准输入

也可以通过标准输入提供 Python 程序：

```sh
docker run --rm -i -p 6080:6080 \
  ghcr.io/vic2ray/playdriver:latest \
  python -u - < my_script.py
```

## 代码结构

进程入口只有一个：`scripts/run.py`。默认把四个组件一起拉起，`--no-browser` / `--no-ui` / `--no-vnc` 各摘掉一个。

```
scripts/
├── run.py        ← 唯一入口：python -u scripts/run.py [--no-browser] [--no-ui] [--no-vnc]
├── routes.py     ← 自家接口的装配点（必须在 mount("/") 之前，见下）
├── vnc.py        noVNC 桌面：静态文件 + WS ↔ TCP 桥
├── bypass/       纯转发服务，可独立运行
├── resident/     常驻浏览器，依赖 bypass
└── uni2ui/       手机画面监控，独立于前两者
```

`routes.py` 单独拎在顶层，不放进 `resident/`：它装的是**整个 app 的**自家接口（`/vnc`、`/cookies`、`/adbs`），跟"常驻浏览器"这个功能没关系。绕过服务的全捕获路由要求这些必须在 `mount("/")` 之前注册，集中在一个文件里才不容易漏。依赖方向是 `routes.py → resident / uni2ui / vnc`，三个包互不认识，也不认识 `routes.py`。

`vnc.py` 也放顶层：它只依赖 FastAPI/Starlette 和一条 TCP 连接，不认识浏览器也不认识转发服务，谁都能单独拿去用。

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
├── service.py    服务编排：起浏览器、起 watcher、起 uni2ui、组 app、拉起 uvicorn
├── watcher.py    挑战检测与过验证
├── browser.py    常驻浏览器的启动、关窗重开、cookie 读取
└── config.py     本模式独有的配置（profile 目录、检测间隔、重开策略）
```

常驻浏览器排查：窗口关掉没重开看 `browser.py` 的 `_supervise`，点了没自动过验证看 `watcher.py`，`/cookies` 返回不对看 `routes.py` 与 `browser.py` 的域过滤。

`uni2ui` 谁也不依赖，可以单独跑（`python -m uni2ui.app`），内部依赖单向：

```
scripts/uni2ui/
├── app.py        HTTP 路由 / 鉴权；create_app() 给 playdriver 用，main() 给独立运行用
├── registry.py   设备表 + 抓帧器池 + agent 通道池
├── control.py    点滑按键走 input，文字走 agent 剪贴板
├── capture.py    抓帧，MJPEG 生成器
├── agent.py      ATX agent 的探测 / 自拉起 / JSON-RPC（抓帧和打字共用一条连接）
├── discovery.py  只读 adb 设备表
└── config.py     环境变量（UNI2UI_*）
```

画面排查：卡在 1fps 看 `agent.py`（agent 没起来，退回 screencap），两台设备其实是同一台看 `registry.py` 的 `_merge`，点不准看前端 `imgPoint` 的坐标换算（`object-fit: contain` 的黑边），中文打不出去看 `control.py` 的剪贴板通道。

## 构建镜像

在包含 `Dockerfile` 的 `playdriver` 目录执行：

```sh
docker build -t playdriver:latest .
```

本仓库的 GitHub Actions workflow 在默认分支有任何改动（纯 Markdown 除外）时构建并推送 GHCR 镜像，也支持从 Actions 页面手动触发。推送的镜像名取决于 GitHub 仓库地址，格式为 `ghcr.io/<owner>/<repository>`；请根据仓库实际地址替换上文示例中的镜像名。
