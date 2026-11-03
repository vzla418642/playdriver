"""uni2ui —— 手机模拟器画面监控 + 基本操作。

做三件事：
  1. 从 adb server 读设备表（只读，绝不 connect / disconnect）
  2. 把在线设备的画面转成 MJPEG 推给浏览器
  3. 把浏览器上的点按 / 滑动 / 按键 / 打字变成设备上的动作

**连接的建立**由 u2（kumuapp / vkapp 那些脚本）负责，这边不抢：设备表里有谁
就显示谁。但**操作**不客气 —— 点滑按键走 `input` 注入，打字走 agent 的剪贴板
粘贴（中文只能这么打）。agent 不在就自己拉一个，机房环境里 u2 反正装着。

运行：
    python -m uni2ui.app                       # 独立跑（在 scripts/ 目录下）
    python -m uni2ui.app --host 0.0.0.0 --port 8000 --fps 15
    UNI2UI_TOKEN=xxx python -m uni2ui.app --host 0.0.0.0   # 对外开时加口令

嵌进 playdriver 时不用 main()，走 `create_app()`：只建 app 不跑 uvicorn，
由 scripts/run.py 统一在 8000 端口上挂到 /adbs。
"""

import argparse
import logging
import os
import sys

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response, StreamingResponse

from . import config
from .control import Controller, InputError
from .discovery import Discovery
from .registry import Registry

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

log = logging.getLogger("uni2ui")


def _detail(exc: Exception) -> str:
    """KeyError 的 str() 会给消息套一层引号，剥掉再给前端。"""
    return str(exc.args[0]) if exc.args else str(exc)


def build_app(registry: Registry) -> FastAPI:
    app = FastAPI(title="uni2ui", docs_url=None, redoc_url=None, openapi_url=None)
    # 输入要 agent 的剪贴板（中文只能靠粘贴），所以把通道也给它
    controller = Controller(registry.device, registry.channel)

    def auth(token: str) -> None:
        """口令校验。没设 TOKEN 就什么都不做，设了就每条请求都要带。"""
        if config.TOKEN and token != config.TOKEN:
            raise HTTPException(401, "口令不对")

    @app.get("/")
    def index():
        # 页面本身只是静态文件，真正的数据都在下面那些接口里
        return FileResponse(os.path.join(STATIC_DIR, "index.html"))

    @app.get("/api/devices")
    def api_devices(token: str = Query("")):
        auth(token)
        return registry.snapshot()

    @app.get("/api/{serial}/info")
    def api_info(serial: str, token: str = Query("")):
        auth(token)
        try:
            registry.device(serial)  # 只为校验在线，句柄本身不用
        except KeyError as exc:
            raise HTTPException(404, _detail(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, _detail(exc)) from exc
        width, height = controller.size(serial)
        return {"serial": serial, "width": width, "height": height}

    @app.post("/api/{serial}/input")
    def api_input(serial: str, payload: dict = Body(...), token: str = Query("")):
        auth(token)
        action = str(payload.get("action") or "")
        try:
            return controller.send(serial, action, payload)
        except InputError as exc:
            raise HTTPException(400, _detail(exc)) from exc
        except KeyError as exc:
            raise HTTPException(404, _detail(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, _detail(exc)) from exc

    @app.get("/stream/{serial}")
    def stream(serial: str, fps: float = 0, token: str = Query("")):
        auth(token)
        try:
            _grabber, frames = registry.stream(serial, fps)
        except KeyError as exc:
            raise HTTPException(404, _detail(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, _detail(exc)) from exc
        return StreamingResponse(
            frames, media_type="multipart/x-mixed-replace; boundary=frame"
        )

    @app.get("/snapshot/{serial}")
    def snapshot(serial: str, token: str = Query("")):
        auth(token)
        try:
            data, ctype = registry.snapshot_frame(serial)
        except KeyError as exc:
            raise HTTPException(404, _detail(exc)) from exc
        except (RuntimeError, TimeoutError) as exc:
            raise HTTPException(409, _detail(exc)) from exc
        return Response(content=data, media_type=ctype)

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "devices": len(registry.online_serials())}

    return app


def create_app(
    adb_host: str = "",
    adb_port: int = 0,
    fps: float = 0,
    prewarm: bool = True,
) -> tuple[FastAPI, Registry]:
    """建 app 并启动设备表监听，**不跑 uvicorn**。

    独立运行和嵌进 playdriver 都走这里，区别只是谁来 serve：
      - `main()` 自己 `uvicorn.run(app)`
      - `scripts/run.py` 把 app 挂到 `/adbs` 上，和转发服务共用 8000 端口

    返回 (app, registry)，registry 要留着 —— 关服务时调它的 `stop()`。
    """
    discovery = Discovery(adb_host or config.ADB_HOST, adb_port or config.ADB_PORT)
    registry = Registry(
        discovery,
        default_fps=fps or config.DEFAULT_FPS,
        prewarm=config.PREWARM and prewarm,
    )
    registry.start()
    return build_app(registry), registry


def _print_banner(registry: Registry, host: str, port: int, fps: float) -> None:
    discovery = registry.discovery
    print(f"[uni2ui] adb server: {discovery.host}:{discovery.port}")
    try:
        devices = discovery.list()
    except Exception as exc:
        print(f"[uni2ui] 连不上 adb server（{type(exc).__name__}: {exc}）")
        print("[uni2ui] 页面能打开，但设备表是空的；adb server 起来后会自动恢复")
    else:
        if not devices:
            print("[uni2ui] 设备表是空的，等 u2 把设备连上来")
        for dev in devices:
            flag = "[on] " if dev.state == "device" else "[--] "
            print(f"[uni2ui]   {flag}{dev.serial}  {dev.state}  {dev.model}")
    print(f"[uni2ui] 打开 http://{host}:{port}   默认 {fps:g} fps")
    if config.TOKEN:
        print("[uni2ui] 已设访问口令，地址要带 ?token=xxx")
    elif host not in ("127.0.0.1", "localhost"):
        print("[uni2ui] 注意：监听在非本机地址且没设口令，局域网内谁都能操作设备")
        print("[uni2ui]       要口令就设环境变量 UNI2UI_TOKEN")


def main() -> None:
    parser = argparse.ArgumentParser(prog="app.py", description="手机画面监控 + 基本操作")
    parser.add_argument("--host", default=config.HOST, help="监听地址")
    parser.add_argument("--port", type=int, default=config.PORT, help="监听端口")
    parser.add_argument("--fps", type=float, default=config.DEFAULT_FPS, help="默认帧率")
    parser.add_argument("--adb-host", default=config.ADB_HOST, help="adb server 地址")
    parser.add_argument("--adb-port", type=int, default=config.ADB_PORT, help="adb server 端口")
    parser.add_argument(
        "--no-prewarm",
        action="store_true",
        help="设备上线时不提前拉起 agent（默认会拉，打开页面即可出画）",
    )
    parser.add_argument("--log-level", default=config.LOG_LEVEL, help="日志级别")
    args = parser.parse_args()

    # 被 webrun 之类的运行台重定向到文件时，不加这行会看不到启动横幅
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    app, registry = create_app(
        adb_host=args.adb_host,
        adb_port=args.adb_port,
        fps=args.fps,
        prewarm=not args.no_prewarm,
    )
    _print_banner(registry, args.host, args.port, args.fps)

    import uvicorn

    try:
        uvicorn.run(
            app,
            host=args.host,
            port=args.port,
            log_level=args.log_level,
            access_log=False,
        )
    finally:
        registry.stop()


if __name__ == "__main__":
    main()
