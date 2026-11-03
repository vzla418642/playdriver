"""设备上的 ATX agent（uiautomator2 server）：拉起、连上、调 JSON-RPC。

一台设备上只有一个 agent，抓帧和输入共用它，所以所有权集中在这里：

  capture.py  抓帧 —— takeScreenshot 拿现成 JPEG（43ms/帧）
  control.py  文字 —— setClipboard + pasteClipboard（13ms，支持中文）

AgentChannel 是唯一持有者：端口探测、自拉起、常驻连接、以及那条连接上的锁。
之所以不能两处各起一个：agent 是 `app_process` 起的子进程，**谁起的谁负责活着**
（连接一断它就退出），两边各持一条连接等于两边都能把它带走，互相踩。

反过来，接在别人（kumuapp / vkapp）已经跑着的 agent 上完全没问题 ——
它是 nanohttpd，本来就能同时服务多个客户端。
"""

import base64
import http.client
import json
import logging
import threading
import time
from typing import Optional

import adbutils

from .config import AGENT_JAR, AGENT_PORTS, AGENT_RETRY, BOOTSTRAP_AGENT

log = logging.getLogger(__name__)

# 连续失败这么多次才判定 agent 没了。太少会误判（设备忙一下就会超时），
# 太多则要卡好几帧才肯退回 screencap
MAX_FAILURES = 3


class AgentError(RuntimeError):
    """agent 不可用，或者这次调用失败了。"""


class AgentUnavailable(AgentError):
    """设备上没有可用的 agent（没 u2.jar / 拉不起来 / 端口不通）。"""


class AgentRPCError(AgentError):
    """连接是好的，是这次调用本身失败（比如当前没有聚焦的输入框）。

    这种情况不该把连接丢掉 —— 下一帧还要用。
    """


# --------------------------------------------------------------------------
# HTTP over adb
# --------------------------------------------------------------------------
class _AdbHTTPConnection(http.client.HTTPConnection):
    """把 http.client 架在 adb 的 TCP 通道上。

    adb 支持 `host:transport:<serial>` 之后接任意 socket 命令，
    adbutils 的 create_connection 把它封装成了 socket 的样子。
    u2 自己也是这么做的，所以不需要 adb forward，主机上也不会多监听端口。
    """

    def __init__(self, device, port: int, timeout: float) -> None:
        super().__init__("127.0.0.1", port, timeout=timeout)
        self._device = device
        self._port = port

    def connect(self) -> None:  # type: ignore[override]
        self.sock = self._device.create_connection(adbutils.Network.TCP, self._port)


class AgentSession:
    """一条常驻的 adb-HTTP 连接。

    每帧新建连接要多花约 40ms（adb transport 握手 + HTTP 建连），复用一条就够
    （实测 86ms → 43ms）。连接被服务端关掉（nanohttpd 会回收空闲连接）时
    自动重连一次。
    """

    HEADERS = {
        "Content-Type": "application/json",
        # nanohttpd 的 gzip 有资源泄漏，u2 也是显式关掉的
        "Accept-Encoding": "",
    }

    def __init__(self, device, port: int, timeout: float = 5.0) -> None:
        self._device = device
        self.port = port
        self._timeout = timeout
        self._conn: Optional[_AdbHTTPConnection] = None

    def _open(self) -> _AdbHTTPConnection:
        conn = _AdbHTTPConnection(self._device, self.port, self._timeout)
        conn.connect()  # 提前连，失败立刻抛，不用等到 send
        return conn

    def close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    def request(self, method: str, path: str, body: Optional[bytes] = None) -> bytes:
        last: Optional[Exception] = None
        # 最多两次：第一次复用旧连接，失败就丢掉重连
        for _ in range(2):
            try:
                if self._conn is None:
                    self._conn = self._open()
                self._conn.request(method, path, body, self.HEADERS)
                resp = self._conn.getresponse()
                payload = resp.read()
                if resp.status != 200:
                    raise RuntimeError(f"agent HTTP {resp.status}")
                return payload
            except Exception as exc:  # noqa: BLE001
                last = exc
                self.close()
        assert last is not None
        raise last

    def ping(self) -> bool:
        try:
            return bool(self.request("GET", "/ping"))
        except Exception:
            return False

    def call(self, method: str, params: list) -> object:
        """调一个 JSON-RPC 方法。"""
        payload = self.request(
            "POST",
            "/jsonrpc/0",
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode(),
        )
        data = json.loads(payload)
        if data.get("error"):
            raise AgentRPCError(f"agent 报错: {data['error']}")
        return data.get("result")

    def screenshot(self, quality: int, display_id: int = 0) -> bytes:
        # 参数是 [display_id, quality]，display 0 就是主屏
        result = self.call("takeScreenshot", [display_id, quality])
        if not result:
            raise AgentRPCError("agent 没返回画面")
        return base64.b64decode(result)


def ping_agent(device, port: int, timeout: float = 2.0) -> bool:
    """探一下端口上是不是活的 agent。用完立刻断开。"""
    session = AgentSession(device, port, timeout)
    try:
        return session.ping()
    finally:
        session.close()


def find_agent(device, ports: tuple[int, ...] = AGENT_PORTS) -> Optional[int]:
    """在候选端口里找活着的 agent，找不到返回 None。"""
    for port in ports:
        if ping_agent(device, port):
            return port
    return None


# --------------------------------------------------------------------------
# 拉起 agent
# --------------------------------------------------------------------------
class AgentProcess:
    """自己拉起来的 ATX agent。

    uiautomator2 内部就是这么起的（core.launch_uiautomator）：

        CLASSPATH=/data/local/tmp/u2.jar app_process / com.wetest.uia2.Main -p <port>

    走一条常驻的 adb shell 连接。连接一断 agent 就跟着退，所以这个对象必须留着；
    同时要把它的输出读空，否则日志把管道写满，agent 会卡在写 stdout 上，
    表现就是"突然不响应"。
    """

    # 只留最后一段输出用于报错，别让 buffer 无限涨
    KEEP = 4096

    def __init__(self, device, port: int, jar: str = AGENT_JAR) -> None:
        self._device = device
        self.port = port
        self._jar = jar
        self._conn = None
        self._output = bytearray()

    def start(self) -> None:
        cmd = f"CLASSPATH={self._jar} app_process / com.wetest.uia2.Main -p {self.port}"
        self._conn = self._device.shell(cmd, stream=True)
        threading.Thread(
            target=self._drain, name=f"agent-out-{self.port}", daemon=True
        ).start()

    def _drain(self) -> None:
        try:
            while True:
                chunk = self._conn.conn.recv(4096)
                if not chunk:
                    break
                self._output.extend(chunk)
                if len(self._output) > self.KEEP:
                    del self._output[: -self.KEEP]
        except Exception:
            pass

    @property
    def tail(self) -> str:
        return bytes(self._output[-400:]).decode("utf-8", "replace").strip()

    def close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def launch(
    device,
    port: Optional[int] = None,
    timeout: float = 12.0,
    jar: str = AGENT_JAR,
) -> Optional[AgentProcess]:
    """拉起 agent 并等端口就绪。起不来返回 None。

    前提是设备上已经有 u2.jar（装过一次 uiautomator2 就有）——
    没有就什么都不做，绝不在设备上装东西。
    """
    try:
        probe = device.shell(["ls", jar], timeout=5)
    except Exception:
        probe = ""
    if "u2.jar" not in (probe or ""):
        return None

    port = port or AGENT_PORTS[0]
    proc = AgentProcess(device, port, jar)
    proc.start()

    deadline = time.time() + timeout
    while time.time() < deadline:
        if ping_agent(device, port, timeout=1.0):
            return proc
        time.sleep(0.2)

    log.warning("agent 起了但 %.0fs 内端口没开，输出：%s", timeout, proc.tail or "(空)")
    proc.close()
    return None


# --------------------------------------------------------------------------
# 一台设备一条通道
# --------------------------------------------------------------------------
class AgentChannel:
    """抓帧和输入共用的那条 agent 通道。

    三层锁，各管一件事，别合并：

      _state  字段（端口 / 连接 / 进程 / 退避时间），只持有极短时间，
              所以 stats 接口读 port 永远不会被一次 12s 的拉起卡住
      _io     那条 HTTP 连接。一条连接不能并发用，抓帧线程和点击请求
              必须排队（实测抓帧占空比约 65%，点击最多等一帧，43ms）
      _probe  探测 + 拉起。只让一个线程去探，免得同时起两个 agent 抢端口

    加锁顺序恒为 _probe → _state → _io，不会反向，所以不会死锁。

    取连接的方法叫 `_connection()` 不叫 `_session()`：实例属性 `self._session`
    会把同名方法遮蔽掉，调用时直接 TypeError: 'NoneType' object is not callable。
    """

    def __init__(
        self,
        device,
        serial: str = "",
        ports: tuple[int, ...] = AGENT_PORTS,
        retry: float = AGENT_RETRY,
        bootstrap: bool = BOOTSTRAP_AGENT,
        jar: str = AGENT_JAR,
    ) -> None:
        self._device = device
        self.serial = serial
        self._ports = tuple(ports)
        self._retry = retry
        self._bootstrap = bootstrap
        self._jar = jar

        self._state = threading.Lock()
        self._io = threading.Lock()
        self._probe = threading.Lock()

        self._port: Optional[int] = None
        self._session: Optional[AgentSession] = None
        self._proc: Optional[AgentProcess] = None
        self._retry_at = 0.0
        self._failures = 0

    # ---- 状态 ----------------------------------------------------------
    @property
    def port(self) -> Optional[int]:
        with self._state:
            return self._port

    @property
    def available(self) -> bool:
        with self._state:
            return self._port is not None

    def prewarm(self) -> None:
        """设备一上线就把 agent 准备好，别等有人打开页面才现拉。

        拉一次约 0.5s，提前做掉的话第一次打开面板就是立刻出画。
        放线程里跑：别拖住 adb 设备表的监听循环。
        """

        def run() -> None:
            try:
                self.ensure()
            except Exception as exc:  # noqa: BLE001
                log.info("%s: 预热失败（%s: %s）", self.serial, type(exc).__name__, exc)

        threading.Thread(target=run, name=f"prewarm-{self.serial}", daemon=True).start()

    # ---- 探测 / 拉起 ---------------------------------------------------
    def ensure(self) -> bool:
        """确认 agent 能用；没有就自己拉一个。可被多个线程同时调用。"""
        if self.available:
            return True
        with self._probe:
            if self.available:
                return True
            with self._state:
                if time.time() < self._retry_at:
                    return False
            return self._discover()

    def _discover(self) -> bool:
        """调用方必须已经持有 _probe。"""
        port = find_agent(self._device, self._ports)

        if port is None and self._bootstrap:
            log.info("%s: 设备上没有 agent，自己拉起一个（端口 %d）", self.serial, self._ports[0])
            proc = launch(self._device, self._ports[0], jar=self._jar)
            if proc is not None:
                with self._state:
                    old, self._proc = self._proc, proc
                if old is not None:
                    old.close()  # 旧的已经探不到了，连接放掉
                port = self._ports[0]

        if port is None:
            with self._state:
                self._retry_at = time.time() + self._retry
            log.info("%s: 暂时没有 agent，%gs 后再试", self.serial, self._retry)
            return False

        with self._state:
            self._port = port
            self._retry_at = 0.0
            self._failures = 0
        log.info("%s: agent 可用（端口 %d）", self.serial, port)
        return True

    def _connection(self) -> AgentSession:
        if not self.ensure():
            raise AgentUnavailable(f"{self.serial or '设备'} 上没有可用的 agent")
        with self._state:
            if self._session is None or self._session.port != self._port:
                old, self._session = self._session, AgentSession(self._device, self._port)
                if old is not None:
                    old.close()
            return self._session

    def _fail(self, exc: Exception) -> None:
        """连接层面的失败：丢掉这条连接；连续失败就判定 agent 没了。"""
        with self._state:
            self._failures += 1
            session, self._session = self._session, None
            if self._failures >= MAX_FAILURES:
                log.warning(
                    "%s: agent 连续失败 %d 次（%s），重新探测",
                    self.serial,
                    self._failures,
                    exc,
                )
                self._port = None
                # 立刻允许重探：agent 被别的进程重启（kumuapp 退出会带走它）
                # 是常见情况，不该让人先看几秒 1fps 的 screencap
                self._retry_at = 0.0
        if session is not None:
            session.close()

    # ---- 调用 ----------------------------------------------------------
    def screenshot(self, quality: int, display_id: int = 0) -> bytes:
        session = self._connection()
        try:
            with self._io:
                frame = session.screenshot(quality, display_id)
        except Exception as exc:  # noqa: BLE001
            self._fail(exc)
            raise AgentError(f"抓帧失败（{type(exc).__name__}: {exc}）") from exc
        with self._state:
            self._failures = 0
        return frame

    def call(self, method: str, params: list) -> object:
        session = self._connection()
        try:
            with self._io:
                return session.call(method, params)
        except AgentRPCError:
            # 连接是好的，是这次调用本身失败 —— 别把连接丢掉
            raise
        except Exception as exc:  # noqa: BLE001
            self._fail(exc)
            raise AgentError(f"{method} 失败（{type(exc).__name__}: {exc}）") from exc

    # ---- 剪贴板 --------------------------------------------------------
    def set_clipboard(self, text: str, label: Optional[str] = None) -> object:
        # 参数顺序是 [label, text]，不是 [text, label]。
        # u2 内部就是 jsonrpc.setClipboard(label, text)，反了会写进去一串 None
        return self.call("setClipboard", [label, text])

    def get_clipboard(self) -> Optional[str]:
        value = self.call("getClipboard", [])
        return value if isinstance(value, str) else None

    def paste(self) -> object:
        return self.call("pasteClipboard", [])

    # ---- 收尾 ----------------------------------------------------------
    def close(self) -> None:
        with self._state:
            session, self._session = self._session, None
            proc, self._proc = self._proc, None
            self._port = None
        if session is not None:
            session.close()
        if proc is not None:
            proc.close()
