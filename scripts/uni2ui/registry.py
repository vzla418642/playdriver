"""设备表 + 抓帧器池 + agent 通道池。

职责：把 adb 的设备表和"这台设备的画面/操作"对应起来。

agent 通道单独拎出来由这里持有，而不是塞进抓帧器里 —— 因为抓帧和输入
共用同一条（设备上只有一个 agent）。抓帧器只是个消费者，它被回收了
不该把 agent 一起带走。

设备掉线就回收抓帧器和通道；设备上线也不急着建 —— 等有人看再建（懒启动），
不然开着 5 台模拟器、页面都没开，CPU 白烧。
"""

import logging
import threading
import time
from typing import Optional

from .agent import AgentChannel
from .capture import FrameGrabber
from .config import DEFAULT_FPS, PREWARM
from .discovery import DeviceInfo, Discovery

log = logging.getLogger(__name__)


class Registry:
    def __init__(
        self,
        discovery: Discovery,
        default_fps: float = DEFAULT_FPS,
        prewarm: bool = PREWARM,
    ) -> None:
        self._discovery = discovery
        self._default_fps = default_fps
        self._prewarm_enabled = prewarm
        # 可重入：grabber() 持锁期间会调 channel()，两者都要写池子
        self._lock = threading.RLock()
        self._devices: list[DeviceInfo] = []
        self._channels: dict[str, AgentChannel] = {}
        self._grabbers: dict[str, FrameGrabber] = {}
        self._raw_serials: set[str] = set()
        # 已经预热过 agent 的串口。只在设备"新出现"时预热一次，
        # 否则设备表每次刷新都会重新拉一次 agent
        self._prewarmed: set[str] = set()
        self._adb_ok = False
        self._adb_error = ""
        self._updated_at = 0.0

    # ---- 生命周期 ------------------------------------------------------
    @property
    def discovery(self) -> Discovery:
        """底层的 adb 设备表访问器（启动横幅要用它的地址和首次列表）。"""
        return self._discovery

    def start(self) -> None:
        threading.Thread(target=self._watch, name="adb-watch", daemon=True).start()

    def stop(self) -> None:
        """回收所有抓帧器和 agent 通道。

        `adb-watch` 线程是 daemon，进程退出就没了，这里**故意不去停它**：
        `track_devices` 阻塞在 recv 上，想中断只能关 socket，为了一个马上
        就要退出的进程引入那套复杂度不划算。
        """
        with self._lock:
            grabbers = list(self._grabbers.values())
            channels = list(self._channels.values())
            self._grabbers.clear()
            self._channels.clear()
        for grabber in grabbers:
            grabber.stop()
        for channel in channels:
            channel.close()

    def _watch(self) -> None:
        # 先同步拉一次，别等第一个变更事件
        self._refresh()
        self._discovery.watch(lambda devices: self._apply(devices))

    def _refresh(self) -> None:
        try:
            self._apply(self._discovery.list())
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._adb_ok = False
                self._adb_error = f"{type(exc).__name__}: {exc}"
            log.warning("读不到 adb 设备表：%s", self._adb_error)

    def _apply(self, devices: list[DeviceInfo]) -> None:
        raw = {d.serial for d in devices}
        self._discovery.forget(self._raw_serials - raw)
        self._raw_serials = raw

        merged = self._merge(devices)
        online = {d.serial for d in merged if d.state == "device"}
        with self._lock:
            self._devices = merged
            self._adb_ok = True
            self._adb_error = ""
            self._updated_at = time.time()
            gone = (set(self._grabbers) | set(self._channels)) - online
            dead_grabbers = [self._grabbers.pop(s) for s in gone if s in self._grabbers]
            dead_channels = [self._channels.pop(s) for s in gone if s in self._channels]

        for grabber in dead_grabbers:
            grabber.stop()
        for channel in dead_channels:
            channel.close()
        for serial in gone:
            log.info("%s 已断开，回收抓帧器和 agent 通道", serial)

        if self._prewarm_enabled:
            fresh = online - self._prewarmed
            self._prewarmed = set(online)
            for serial in fresh:
                channel = self.channel(serial)
                if channel is not None:
                    channel.prewarm()

    def _merge(self, devices: list[DeviceInfo]) -> list[DeviceInfo]:
        """同一台机器的多个 adb 通道只留一个画面。

        MuMu 这类模拟器会同时出现在 tcp:7555 和 emulator-5556 两条通道上，
        boot_id 一样，其实是同一台机器。不合并的话，一张脸开两个抓帧线程，
        每秒向同一个 agent 发两倍请求，画面反而更卡。
        """
        groups: dict[str, list[DeviceInfo]] = {}
        merged: list[DeviceInfo] = []

        for device in devices:
            # 离线的设备问不了 boot_id，直接当独立设备显示
            fingerprint = (
                self._discovery.fingerprint(device.serial)
                if device.state == "device"
                else ""
            )
            if not fingerprint:
                merged.append(device)
                continue
            groups.setdefault(fingerprint, []).append(device)

        for group in groups.values():
            # 优先 host:port（MuMu 的 127.0.0.1:7555、无线真机），
            # emulator-NNNN 是 adb 自动注册的控制台别名，排在后面
            group.sort(key=lambda d: (d.serial.startswith("emulator-"), d.serial))
            primary = group[0]
            merged.append(
                DeviceInfo(
                    serial=primary.serial,
                    state=primary.state,
                    model=primary.model,
                    aliases=tuple(d.serial for d in group[1:]),
                )
            )

        merged.sort(key=lambda d: d.serial)
        return merged

    # ---- 查询 ----------------------------------------------------------
    def snapshot(self) -> dict:
        with self._lock:
            devices = list(self._devices)
            grabbers = dict(self._grabbers)
            adb_ok, adb_error, updated = self._adb_ok, self._adb_error, self._updated_at
        return {
            "adb": {
                "host": self._discovery.host,
                "port": self._discovery.port,
                "ok": adb_ok,
                "error": adb_error,
            },
            "updated_at": round(updated, 3),
            "devices": [
                {
                    "serial": d.serial,
                    "state": d.state,
                    "model": d.model,
                    "aliases": list(d.aliases),
                    "stats": grabbers[d.serial].stats() if d.serial in grabbers else None,
                }
                for d in devices
            ],
        }

    def online_serials(self) -> list[str]:
        with self._lock:
            return [d.serial for d in self._devices if d.state == "device"]

    def device(self, serial: str):
        """拿设备句柄做操作（点按/滑动/按键）。

        只校验"这台设备在线"，不碰连接状态 —— 和抓帧一样是纯观察者视角，
        唯一的区别是这里会往设备发 input 事件。
        """
        with self._lock:
            info = next((d for d in self._devices if d.serial == serial), None)
            if info is None:
                raise KeyError(f"没有这台设备: {serial}")
            if info.state != "device":
                raise RuntimeError(f"设备状态是 {info.state}，暂时操作不了")
        return self._discovery.device(serial)

    def channel(self, serial: str) -> Optional[AgentChannel]:
        """取（或建）这台设备的 agent 通道。设备不在线返回 None。

        通道是抓帧和输入共用的：设备上只有一个 agent，一条连接就够；
        更重要的是 AgentProcess 只能有一个主人，两处各起一个会互相踩
        （后起的占不上端口，先起的那个还被自己的连接生命周期牵着）。
        """
        with self._lock:
            channel = self._channels.get(serial)
            if channel is not None:
                return channel
            info = next((d for d in self._devices if d.serial == serial), None)
            if info is None or info.state != "device":
                return None
            try:
                device = self._discovery.device(serial)
            except Exception as exc:  # noqa: BLE001
                log.info("%s: 拿不到设备句柄（%s: %s）", serial, type(exc).__name__, exc)
                return None
            channel = AgentChannel(device, serial)
            self._channels[serial] = channel
            return channel

    def grabber(self, serial: str) -> FrameGrabber:
        """取（或建）抓帧器。设备不在线就抛。

        已经存在的对象一律复用，即使它的抓帧线程停了 —— touch() 会把线程重新拉起来。
        """
        with self._lock:
            info = next((d for d in self._devices if d.serial == serial), None)
            if info is None:
                raise KeyError(f"没有这台设备: {serial}")
            if info.state != "device":
                raise RuntimeError(f"设备状态是 {info.state}，暂时看不了")

            grabber = self._grabbers.get(serial)
            if grabber is None:
                channel = self.channel(serial)
                if channel is None:
                    raise RuntimeError(f"{serial} 拿不到设备句柄")
                grabber = FrameGrabber(
                    serial,
                    self._discovery.device(serial),
                    channel,
                    default_fps=self._default_fps,
                )
                self._grabbers[serial] = grabber
            return grabber

    def stream(self, serial: str, fps: Optional[float] = None):
        """返回 (grabber, 帧生成器)。"""
        grabber = self.grabber(serial)
        grabber.touch(fps or self._default_fps)
        return grabber, grabber.frames()

    def snapshot_frame(self, serial: str, timeout: float = 5.0) -> tuple[bytes, str]:
        return self.grabber(serial).snapshot(timeout)
