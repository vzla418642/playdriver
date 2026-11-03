"""单台设备的抓帧器。

两个后端，按可用性自动选：

  agent     —— 直接连设备上 ATX agent 的 HTTP 端口拿现成 JPEG，约 43ms/帧。
               agent 的生命周期归 AgentChannel 管（见 agent.py），这里只管用。
  screencap —— `adb shell screencap -p`，一次约 1s，agent 不在时的兜底。

为什么不用 u2 抓帧：u2.screenshot() 本质就是调 agent 的 takeScreenshot，拿到 JPEG
之后还要 PIL 解码 + 转 RGB + 再编码一遍。直接拿 agent 返回的 JPEG 能省掉这两步。

agent 连接是**和输入共用的**（同一个 AgentChannel），不是这里独占 ——
一台设备上只有一个 agent，开两条连接纯属浪费，而且会互相抢进程归属。
"""

import collections
import io
import logging
import threading
import time
from typing import Optional

from .agent import AgentChannel, AgentError
from .config import (
    DEFAULT_FPS,
    FALLBACK_FPS,
    IDLE_TIMEOUT,
    JPEG_QUALITY,
    MAX_FPS,
)

log = logging.getLogger(__name__)

BOUNDARY = b"frame"

# 帧率统计窗口（秒）。面板上显示的是实测值，不是配置值——
# 之前把"目标帧率"当实际帧率显示，看着 8fps 其实只有 1fps，很难发现
FPS_WINDOW = 3.0


# --------------------------------------------------------------------------
# screencap 兜底
# --------------------------------------------------------------------------
def _png_to_jpeg(png: bytes) -> Optional[bytes]:
    """有 Pillow 就转 JPEG（体积小 8 倍），没有就原样发 PNG。"""
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        image = Image.open(io.BytesIO(png)).convert("RGB")
        buf = io.BytesIO()
        image.save(buf, "JPEG", quality=JPEG_QUALITY)
        return buf.getvalue()
    except Exception:
        return None


def screencap_frame(device) -> tuple[bytes, str]:
    png = device.shell(["screencap", "-p"], encoding=None)
    if not png:
        raise RuntimeError("screencap 没返回数据")
    jpeg = _png_to_jpeg(png)
    if jpeg is not None:
        return jpeg, "image/jpeg"
    return png, "image/png"


# --------------------------------------------------------------------------
# 抓帧器
# --------------------------------------------------------------------------
class FrameGrabber:
    """一台设备一个实例，一个抓帧线程。

    没人看的时候线程会自己退（IDLE_TIMEOUT），所以多开模拟器也不会有
    一堆空转的 screencap。
    """

    def __init__(
        self,
        serial: str,
        device,
        channel: AgentChannel,
        default_fps: float = DEFAULT_FPS,
    ) -> None:
        self.serial = serial
        self._device = device
        self._channel = channel

        self._lock = threading.Lock()
        # 帧生成器靠它被唤醒：比每 20ms 轮询一次少最多 20ms 延迟
        self._cond = threading.Condition(self._lock)
        self._data = b""
        self._ctype = "image/jpeg"
        self._ts = 0.0
        self._error = ""
        self._stamps: collections.deque[float] = collections.deque(maxlen=120)

        self._backend = "screencap"

        self._fps = default_fps
        self._last_viewer_at = time.time()
        self._closed = False
        self._thread: Optional[threading.Thread] = None

    # ---- 生命周期 ------------------------------------------------------
    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        with self._lock:
            if self.alive:
                return
            self._closed = False
            self._last_viewer_at = time.time()
            self._thread = threading.Thread(
                target=self._run, name=f"grab-{self.serial}", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        """只停抓帧线程。

        **不碰 agent**：它是和输入共用的，而且空闲只占 7% 单核，
        关掉的话下次有人看要等 0.5s 重新拉起，还可能连累 kumuapp / vkapp。
        设备真的掉线了由 Registry 去 close 整条通道。
        """
        with self._lock:
            self._closed = True

    def touch(self, fps: float = 0.0) -> None:
        """有人在拉帧。

        不用观众计数：客户端断开时 Starlette 不保证立刻关掉同步生成器，
        计数会只增不减，抓帧线程就再也停不下来了。改成"最近有没有人拉"，
        自然过期，断连漏掉也不会一直空转。
        """
        with self._lock:
            self._last_viewer_at = time.time()
            if fps:
                self._fps = min(MAX_FPS, max(self._fps, fps))
        self.start()

    # ---- 状态 ----------------------------------------------------------
    def _actual_fps(self) -> float:
        if len(self._stamps) < 2:
            return 0.0
        cutoff = time.time() - FPS_WINDOW
        recent = [t for t in self._stamps if t >= cutoff]
        if len(recent) < 2:
            return 0.0
        span = recent[-1] - recent[0]
        return round((len(recent) - 1) / span, 1) if span > 0 else 0.0

    def stats(self) -> dict:
        with self._lock:
            return {
                "serial": self.serial,
                "backend": self._backend,
                "agent_port": self._channel.port,
                "fps": round(self._fps, 1),
                # 实测帧率。面板上要看这个，目标值骗人
                "actual_fps": self._actual_fps(),
                "streaming": self.alive,
                "frame_age": round(time.time() - self._ts, 2) if self._ts else None,
                "error": self._error,
            }

    # ---- 抓帧 ----------------------------------------------------------
    def _from_agent(self) -> Optional[tuple[bytes, str]]:
        """走 agent 拿一帧。没有 agent 返回 None。"""
        if not self._channel.available and not self._channel.ensure():
            return None
        frame = self._channel.screenshot(JPEG_QUALITY)
        with self._lock:
            self._backend = "agent"
        return frame, "image/jpeg"

    def _capture(self) -> tuple[bytes, str]:
        try:
            shot = self._from_agent()
        except AgentError as exc:
            # 通道自己放弃（连续失败把端口清了）之前，宁可抛给 _run 立刻重试，
            # 也别掉进 1s 一次的 screencap —— 那才是用户嘴里的"卡"
            if self._channel.available:
                raise
            log.info("%s: agent 不可用（%s），退回 screencap", self.serial, exc)
            shot = None

        if shot is not None:
            return shot

        with self._lock:
            self._backend = "screencap"
        return screencap_frame(self._device)

    def _run(self) -> None:
        while True:
            with self._lock:
                if self._closed:
                    break
                idle = time.time() - self._last_viewer_at > IDLE_TIMEOUT
                fps = self._fps
            if idle:
                log.info("%s: 无人观看，停止抓帧", self.serial)
                # 故意不关 agent：它空闲只占 7% 单核，关掉的话下次有人看要等 0.5s
                # 重新拉起。更重要的是，kumuapp / vkapp 可能已经接在这个 agent 上，
                # 我们去关会连累它们
                break

            started = time.time()
            try:
                data, ctype = self._capture()
            except Exception as exc:  # noqa: BLE001
                with self._lock:
                    self._error = f"{type(exc).__name__}: {exc}"
                # screencap 那次失败可能只是设备忙，等一下再来
                time.sleep(0.2 if not self._channel.available else 0.05)
            else:
                now = time.time()
                with self._cond:
                    self._data, self._ctype = data, ctype
                    self._ts = now
                    self._error = ""
                    self._stamps.append(now)
                    self._cond.notify_all()

            # screencap 天生慢，别按用户要的帧率硬拉
            target = fps if self._channel.available else FALLBACK_FPS
            sleep = (1.0 / target) - (time.time() - started)
            if sleep > 0:
                time.sleep(sleep)

    # ---- 给 HTTP 层用 --------------------------------------------------
    def frames(self):
        """MJPEG 帧生成器。

        每出一帧就 touch 一次（限流到 0.5s）；浏览器关掉后没人拉，抓帧线程
        自然过期退出。touch 兼作自愈：抓帧线程因为设备掉线退掉了，
        下一次 touch 会把它拉起来。
        """
        last = 0.0
        last_touch = 0.0
        while True:
            now = time.time()
            if now - last_touch >= 0.5:
                last_touch = now
                self.touch()

            with self._cond:
                if self._ts == last:
                    # 有新帧会被 notify_all 叫醒，不用 20ms 轮询一次
                    self._cond.wait(0.3)
                data, ctype, ts = self._data, self._ctype, self._ts

            if ts and ts != last:
                last = ts
                yield (
                    b"--" + BOUNDARY + b"\r\n"
                    b"Content-Type: " + ctype.encode() + b"\r\n"
                    b"Content-Length: " + str(len(data)).encode() + b"\r\n\r\n"
                    + data + b"\r\n"
                )

    def snapshot(self, timeout: float = 5.0) -> tuple[bytes, str]:
        """要一张静态图：拉起来抓一帧就走。"""
        self.touch(fps=1)
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                if self._ts and time.time() - self._ts < 2.0:
                    return self._data, self._ctype
            time.sleep(0.02)
        raise TimeoutError("等不到画面")
