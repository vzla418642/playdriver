"""设备发现：以 adb server 的设备表为准。

这里是整个服务"不耦合 u2"的关键：不去问 u2"你连了谁"，
而是看 adb server 自己维护的设备表。u2 也好、kumuapp 也好、
命令行 adb 也好，谁把设备连上，设备表里就会出现它。

**自动发现全程只读**：只用 host:devices / host:track-devices，forward 一概不碰。
`connect` / `disconnect` 只在**用户显式要求**时调用（页面上的"连接"框、
或 URL 里的 `?address=`），服务自己绝不替别人做决定。
"""

# Discovery 自己有个 list 方法，类体里写 list[DeviceInfo] 会被当成方法名，
# 用延迟求值的注解绕开
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable, Optional

from adbutils import AdbClient
from adbutils.errors import AdbError

log = logging.getLogger(__name__)

# `adb connect 192.168.1.5` 不带端口时 adb 自己会用 5555，这里显式补上，
# 免得用户少打一个端口就换来一句莫名其妙的报错
DEFAULT_TCP_PORT = 5555


def normalize_address(address: str) -> str:
    """`192.168.1.5` → `192.168.1.5:5555`；带了端口就原样返回。

    只接受 host[:port]。这东西会被拼进 adb server 的协议行（`host:connect:`），
    注入不了，但脏输入只会换来一句难懂的 AdbError，不如在这里先挡掉。
    """
    addr = (address or "").strip()
    if not addr:
        raise ValueError("地址不能为空")
    if any(ch.isspace() for ch in addr):
        raise ValueError("地址不能带空格")
    if addr.count(":") > 1 and not addr.startswith("["):
        raise ValueError("IPv6 请写成 [::1]:5555 这种形式")
    if ":" not in addr:
        addr = f"{addr}:{DEFAULT_TCP_PORT}"
    host, _, port = addr.rpartition(":")
    if not host or not port.isdigit() or not 0 < int(port) < 65536:
        raise ValueError(f"端口不合法：{address}")
    return addr


@dataclass(frozen=True)
class DeviceInfo:
    serial: str
    state: str  # device / offline / unauthorized / ...
    model: str = ""
    # 同一台机器的其它 adb 通道（MuMu 会同时注册 tcp 和 emulator-NNNN 两条）
    aliases: tuple[str, ...] = ()


class Discovery:
    def __init__(self, host: str, port: int) -> None:
        self._client = AdbClient(host=host, port=port)
        self.host = host
        self.port = port
        self._fingerprints: dict[str, str] = {}

    def list(self) -> list[DeviceInfo]:
        infos = self._client.list(extended=True)
        return [
            DeviceInfo(
                serial=info.serial,
                state=info.state,
                model=(info.tags or {}).get("model", ""),
            )
            for info in infos
        ]

    def device(self, serial: str):
        """拿设备句柄。只是句柄，不会触发连接。"""
        return self._client.device(serial)

    def connect(self, address: str) -> str:
        """主动连一台设备（`adb connect`），返回 adb 的原话。

        **唯一的写操作，只能由用户显式触发。** 自动发现仍然是纯只读。
        连不上会抛 AdbError（不是 ValueError）。
        """
        return self._client.connect(normalize_address(address))

    def disconnect(self, address: str) -> str:
        """断掉一台设备。用途主要是收拾打错的地址 —— adb 会把它们留成
        offline 条目，一直挂在设备表里。"""
        return self._client.disconnect(normalize_address(address))

    def fingerprint(self, serial: str) -> str:
        """设备指纹，用来认"同一台机器走了两个 adb 通道"。

        boot_id 每次开机重新生成，同一台设备无论从 tcp:7555 还是
        emulator-5556 连进去都一样。MuMu 这类模拟器会同时注册两个通道，
        adb devices 里就出现两条，指纹相同就能合并成一个画面。

        拿不到就返回空串 —— 调用方会把空指纹当作"不合并"，宁可多显示一个，
        也不要错误地把两台设备合成一个。
        """
        cached = self._fingerprints.get(serial)
        if cached is not None:
            return cached
        try:
            out = self.device(serial).shell(
                ["cat", "/proc/sys/kernel/random/boot_id"], timeout=5
            )
            fp = (out or "").strip()
        except Exception as exc:
            log.debug("%s: 取指纹失败（%s）", serial, exc)
            fp = ""
        self._fingerprints[serial] = fp
        return fp

    def forget(self, serials: set[str]) -> None:
        """设备消失后清掉指纹缓存，重新插上时重新取（可能已经重启过）。"""
        for serial in serials:
            self._fingerprints.pop(serial, None)

    def server_version(self) -> Optional[int]:
        try:
            return self._client.server_version()
        except AdbError:
            return None

    def watch(self, on_change: Callable[[list[DeviceInfo]], None]) -> None:
        """阻塞：设备表一变就回调一次。

        track_devices 是 adb server 的推送通道（host:track-devices），
        比轮询快，也不会给 adb 添负担。server 挂了就退避重连。
        """
        delay = 1.0
        while True:
            try:
                for _event in self._client.track_devices():
                    delay = 1.0
                    on_change(self.list())
            except Exception as exc:
                # adb server 被杀 / 重启中，退避后重连，不退出线程
                log.warning(
                    "track_devices 中断（%s: %s），%.0fs 后重连",
                    type(exc).__name__,
                    exc,
                    delay,
                )
                time.sleep(delay)
                delay = min(delay * 2, 15.0)
