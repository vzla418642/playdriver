"""把浏览器上的操作变成设备上的动作。

两条路，按实测延迟分，不是按"底层/高层"分：

  点 / 滑 / 三大键  →  adb `input`
      实测 tap 43ms、swipe(300ms) 353ms、keyevent 39ms。
      走 InputManager 注入，不读 UI 树。
      一条命令 40ms 左右；网上说的 400ms 是每次起 adb.exe 进程的开销，
      这里 adbutils 复用 adb server 连接，没有那个成本。

  文字  →  agent 剪贴板粘贴
      setClipboard 6ms + pasteClipboard 7ms。
      不只是快：`input text` 只认 ASCII，中文、emoji 根本打不出来，粘贴才行。

反过来用就慢得离谱：agent 的 click / swipe / pressKey 走 uiautomator 的
InteractionController，**每次注入前后都 waitForIdle()**，实测 157ms / 1016ms /
1028ms。所以不是"agent 不能碰"，是这两类操作各有更合适的通道。

agent 不可用时文字退回 `input text`（仅 ASCII），点滑按键不受影响。

坐标一律由前端按画面实际显示尺寸换算成设备像素再传过来，这里只做范围校验。
"""

import logging
import threading
import time
from typing import Callable, Optional

from .agent import AgentChannel, AgentError

log = logging.getLogger(__name__)

# Android keycode。只放常用的几个，不把任意 keycode 暴露给前端。
# 音量键故意不要：机房里用不上，少两个按钮省地方
KEYS = {
    "back": 4,      # KEYCODE_BACK
    "home": 3,      # KEYCODE_HOME
    "recent": 187,  # KEYCODE_APP_SWITCH
    "power": 26,    # KEYCODE_POWER
    "wakeup": 224,  # KEYCODE_WAKEUP
    "enter": 66,    # KEYCODE_ENTER
    "del": 67,      # KEYCODE_DEL（退格）
}

MIN_SWIPE_MS = 30
MAX_SWIPE_MS = 3000
MAX_TEXT = 1000

# 设备报不出分辨率时的兜底上限，纯粹防呆
FALLBACK_MAX = 10000


class InputError(ValueError):
    """参数不合法。路由层映射成 400。"""


def _num(value, name: str) -> int:
    if isinstance(value, bool) or value is None:
        raise InputError(f"{name} 必须是数字")
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        raise InputError(f"{name} 必须是数字") from None


def _coord(value, name: str, limit: int) -> int:
    n = _num(value, name)
    if n < 0:
        raise InputError(f"{name} 不能是负数")
    # 超出分辨率就夹住，别因为差一个像素整条指令被拒
    return min(n, limit - 1) if limit > 0 else min(n, FALLBACK_MAX)


def _text_of(data: dict) -> str:
    text = data.get("text")
    if not isinstance(text, str) or not text:
        raise InputError("text 不能为空")
    if len(text) > MAX_TEXT:
        raise InputError(f"文字太长（最多 {MAX_TEXT} 个字符）")
    return text


class Controller:
    """一台设备一个串行锁，避免连点时命令互相插队。"""

    def __init__(
        self,
        device_of: Callable[[str], object],
        channel_of: Optional[Callable[[str], Optional[AgentChannel]]] = None,
    ) -> None:
        self._device_of = device_of
        self._channel_of = channel_of or (lambda _serial: None)
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()
        self._sizes: dict[str, tuple[int, int]] = {}

    # ---- 内部 ----------------------------------------------------------
    def _lock(self, serial: str) -> threading.Lock:
        with self._guard:
            lock = self._locks.get(serial)
            if lock is None:
                lock = self._locks[serial] = threading.Lock()
            return lock

    def size(self, serial: str) -> tuple[int, int]:
        """设备分辨率。查一次就缓存，`wm size` 不值得每次都问。"""
        cached = self._sizes.get(serial)
        if cached is not None:
            return cached
        try:
            w, h = self._device_of(serial).window_size()
        except Exception:  # noqa: BLE001
            return (0, 0)
        if w and h:
            self._sizes[serial] = (w, h)
            return (w, h)
        return (0, 0)

    def _shell(self, device, cmd: list[str]) -> int:
        """跑一条 adb 命令，返回耗时（ms）。"""
        started = time.time()
        try:
            out = device.shell(cmd, timeout=10)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"执行失败（{type(exc).__name__}: {exc}）") from exc
        elapsed = round((time.time() - started) * 1000)

        # `input` 成功时没有输出；失败一般会把异常打到 stdout/stderr
        if out and ("Error" in out or "Exception" in out or "Usage" in out):
            raise RuntimeError(f"设备拒绝了这条指令：{out.strip()[:200]}")
        return elapsed

    def _build(self, action: str, data: dict, serial: str) -> list[str]:
        w, h = self.size(serial)

        if action == "tap":
            return [
                "input", "tap",
                str(_coord(data.get("x"), "x", w)),
                str(_coord(data.get("y"), "y", h)),
            ]

        if action == "swipe":
            ms = _num(data.get("duration", 200), "duration")
            ms = max(MIN_SWIPE_MS, min(ms, MAX_SWIPE_MS))
            return [
                "input", "swipe",
                str(_coord(data.get("x1"), "x1", w)),
                str(_coord(data.get("y1"), "y1", h)),
                str(_coord(data.get("x2"), "x2", w)),
                str(_coord(data.get("y2"), "y2", h)),
                str(ms),
            ]

        if action == "key":
            name = str(data.get("name") or "").strip().lower()
            if name not in KEYS:
                raise InputError(f"不支持的按键：{name}（可用：{', '.join(KEYS)}）")
            return ["input", "keyevent", str(KEYS[name])]

        raise InputError(f"不支持的操作：{action}")

    def _text(self, serial: str, data: dict, device) -> dict:
        """文字：优先剪贴板粘贴（支持中文），不行再退回 `input text`。"""
        text = _text_of(data)
        channel = self._channel_of(serial)

        # 主动要一次 agent，而不是只看 available：设备刚上线、还没人看画面时
        # 通道可能还没探过，这时候直接退到 `input text` 就打不出中文了。
        # ensure() 探失败会退避，不会每敲一下都去敲设备
        ready = False
        if channel is not None:
            try:
                ready = channel.ensure()
            except Exception as exc:  # noqa: BLE001
                log.info("%s: 拿不到 agent（%s: %s）", serial, type(exc).__name__, exc)

        if ready:
            started = time.time()
            with self._lock(serial):
                try:
                    channel.set_clipboard(text)
                    # 校验真的写进去了。不校验的话，写失败时 paste 会把**上一段
                    # 旧文字**粘进输入框，比什么都没发生糟糕得多。
                    # 有的系统不允许后台进程读剪贴板，那种情况跳过校验
                    try:
                        got = channel.get_clipboard()
                    except AgentError:
                        got = None
                    if got is not None and got != text:
                        raise AgentError(f"剪贴板没写进去（读回 {got!r}）")
                    channel.paste()
                except AgentError as exc:
                    log.info("%s: 剪贴板粘贴失败（%s），改用 input text", serial, exc)
                else:
                    return {
                        "ok": True,
                        "action": "text",
                        "via": "clipboard",
                        "ms": round((time.time() - started) * 1000),
                    }

        if not text.isascii():
            raise InputError(
                "设备上没有可用的 agent，中文打不出去"
                "（`input text` 只认 ASCII）。等 u2 的 agent 起来再试"
            )
        if any(c in text for c in "\r\n"):
            raise InputError("没有 agent 时文字不能带换行，回车请单独发 key=enter")
        # adbutils 的 list2cmdline 会正确转义空格、引号、& $ ` 这些，
        # 所以直接当单个参数传即可，不用自己拼 %s
        with self._lock(serial):
            ms = self._shell(device, ["input", "text", text])
        return {"ok": True, "action": "text", "via": "input", "ms": ms}

    # ---- 对外 ----------------------------------------------------------
    def send(self, serial: str, action: str, data: Optional[dict] = None) -> dict:
        data = data or {}
        device = self._device_of(serial)  # 顺带校验"这台设备在线"

        if action == "text":
            return self._text(serial, data, device)

        cmd = self._build(action, data, serial)
        with self._lock(serial):
            ms = self._shell(device, cmd)

        # 不把命令原文回显给前端（里面可能有用户输入的文字），只给耗时
        return {"ok": True, "action": action, "ms": ms}
