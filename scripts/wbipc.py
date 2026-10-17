"""WorkBuddy 本机 IPC 客户端（零第三方依赖，仅标准库）。

协议来源：WorkBuddy 客户端自身的 wbipc 实现（本机互操作所需的事实）。
实现为本项目原创。

协议概要
--------
1. 发现：``~/.workbuddy/wbipc/endpoint.json`` 给出 ``endpoint``（命名管道 / unix socket）
   与 ``ticket``（32 字节随机数的 base64url）。
2. 传输：NDJSON —— 一帧一行，每行一个 JSON 对象，UTF-8。
3. 握手：三轮挑战应答，用 ticket 作 HMAC 密钥互相证明身份。
       C -> session_hello     {protocol_min, protocol_max, client_nonce, ticket_id, client}
       S -> session_challenge {server_nonce, server_proof}
       C -> session_prove     {client_proof}
       S -> session_hello_ack {connection_epoch, pipes}
4. 调用：在已建立的通道上跑 JSON-RPC 2.0，方法名为 ``<pipe>/<method>``。
   HTTP 转发走 ``wb.request/http.fetch``，鉴权头由客户端自己附加，
   因此本模块全程接触不到、也不需要 access token。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import queue
import socket
import struct
import sys
import threading
import time

PROTOCOL = 1
PIPE_HTTP = "wb.request"
METHOD_HTTP = "http.fetch"
MAX_FRAME = 8 * 1024 * 1024


class WbipcError(RuntimeError):
    """IPC 层错误。code 与客户端自身的错误码对齐。"""

    def __init__(self, code: str, message: str, data=None):
        super().__init__(message)
        self.code = code
        self.data = data


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def ticket_id(ticket: str) -> str:
    """ticket 的短标识，握手时用于让服务端定位它手里那份 ticket。"""
    return _sha256_hex(ticket)[:16]


def _transcript(role: str, endpoint: str, client_nonce: str, server_nonce: str) -> bytes:
    """构造被 HMAC 覆盖的字节串。

    角色前缀 + 长度前缀（大端 uint32）拼接的五段：角色、协议版本、endpoint、
    客户端 nonce、服务端 nonce。任何一段字节变化都会让证明失效。
    """
    parts = [
        b"wbipc-s" if role == "server" else b"wbipc-c",
        str(PROTOCOL).encode("utf-8"),
        endpoint.encode("utf-8"),
        client_nonce.encode("utf-8"),
        server_nonce.encode("utf-8"),
    ]
    return b"".join(struct.pack(">I", len(p)) + p for p in parts)


def compute_proof(ticket: str, role: str, endpoint: str,
                  client_nonce: str, server_nonce: str) -> str:
    transcript = _transcript(role, endpoint, client_nonce, server_nonce)
    digest = hmac.new(ticket.encode("utf-8"), transcript, hashlib.sha256).digest()
    return _b64url(digest)


def config_dir() -> str:
    """WorkBuddy 配置目录。"""
    custom = os.environ.get("WORKBUDDY_CONFIG_DIR", "").strip()
    return custom or os.path.join(os.path.expanduser("~"), ".workbuddy")


def discovery_path() -> str:
    return os.path.join(config_dir(), "wbipc", "endpoint.json")


def load_endpoint() -> tuple[str, str]:
    """读出 (endpoint, ticket)。客户端没运行时这里会失败 —— 这正是通道不可用的判据。"""
    path = discovery_path()
    if not os.path.exists(path):
        raise WbipcError("E_NO_ENDPOINT", "未找到 %s，WorkBuddy 桌面端似乎没有在运行" % path)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            info = json.load(fh)
    except (OSError, ValueError) as exc:
        raise WbipcError("E_ENDPOINT_UNREADABLE", "读取 %s 失败：%s" % (path, exc))
    endpoint = info.get("endpoint") or ""
    ticket = info.get("ticket") or ""
    if not endpoint or not ticket:
        raise WbipcError("E_ENDPOINT_INVALID", "%s 缺少 endpoint / ticket 字段" % path)
    return endpoint, ticket


class _Transport:
    """把命名管道 / unix socket 统一成 send(dict) + recv(timeout)。"""

    def __init__(self, endpoint: str):
        self.endpoint = endpoint
        self._closed = False
        self._fd = None
        self._sock = None
        self._thread = None
        self._buf = b""
        self._broken = False

    def open(self) -> None:
        if os.name == "nt":
            try:
                # 裸 fd：Windows 命名管道不支持带缓冲的文件对象并发读写
                self._fd = os.open(self.endpoint, os.O_RDWR | os.O_BINARY)
            except OSError as exc:
                raise WbipcError("E_NOT_CONNECTED", "连接命名管道失败：%s" % exc)
        else:
            self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                self._sock.connect(self.endpoint)
            except OSError as exc:
                raise WbipcError("E_NOT_CONNECTED", "连接 unix socket 失败：%s" % exc)

    def _raw_read(self, timeout: float) -> bytes:
        """一次阻塞读，超时后放弃这个连接。

        不能常驻一个读线程：Windows 命名管道上「一个线程阻塞在读、另一个线程写」
        会直接报 EINVAL。这里改成每个往返临时起一个读线程并 join，保证任意时刻
        只有一种操作在进行。
        """
        box: queue.Queue = queue.Queue()

        def work() -> None:
            try:
                if self._sock is not None:
                    box.put(self._sock.recv(65536))
                else:
                    box.put(os.read(self._fd, 65536))
            except OSError as exc:
                box.put(exc)

        worker = threading.Thread(target=work, daemon=True)
        worker.start()
        try:
            item = box.get(timeout=timeout)
        except queue.Empty:
            # 读线程还堵着，这个连接已经没法安全复用了
            self._broken = True
            raise WbipcError("E_TIMEOUT", "等待响应超时（%.1fs）" % timeout)
        if isinstance(item, OSError):
            self._broken = True
            raise WbipcError("E_NOT_CONNECTED", "读取失败：%s" % item)
        if not item:
            self._broken = True
            raise WbipcError("E_NOT_CONNECTED", "连接已被对端关闭")
        return item

    def send(self, payload: dict) -> None:
        if self._broken:
            raise WbipcError("E_NOT_CONNECTED", "连接已失效，请重连")
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        try:
            if self._sock is not None:
                self._sock.sendall(raw)
            else:
                view = memoryview(raw)
                while view:
                    view = view[os.write(self._fd, view):]
        except OSError as exc:
            self._broken = True
            raise WbipcError("E_NOT_CONNECTED", "写入管道失败：%s" % exc)

    def recv(self, timeout: float = 20.0) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            while b"\n" in self._buf:
                line, self._buf = self._buf.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                if len(line) > MAX_FRAME:
                    raise WbipcError("E_PROTOCOL_MISMATCH", "单帧超过上限")
                try:
                    return json.loads(line.decode("utf-8"))
                except ValueError:
                    raise WbipcError("E_PROTOCOL_MISMATCH", "收到非法 JSON 帧")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._broken = True
                raise WbipcError("E_TIMEOUT", "等待响应超时（%.1fs）" % timeout)
            self._buf += self._raw_read(min(remaining, 5.0))

    def close(self) -> None:
        self._closed = True
        for handle in ("sock", "fd"):
            value = getattr(self, "_" + handle)
            if value is None:
                continue
            try:
                if handle == "sock":
                    value.close()
                else:
                    os.close(value)
            except OSError:
                pass


class WbipcClient:
    """一次会话：握手 → 若干次 JSON-RPC 调用。"""

    def __init__(self, endpoint: str | None = None, ticket: str | None = None,
                 timeout: float = 20.0):
        if endpoint is None or ticket is None:
            endpoint, ticket = load_endpoint()
        self.endpoint = endpoint
        self._ticket = ticket
        self.timeout = timeout
        self._tx = _Transport(endpoint)
        self._seq = 0
        self.epoch = ""
        self.pipes: list = []
        self._channels: dict = {}

    # ---- 生命周期 -------------------------------------------------------
    def __enter__(self) -> "WbipcClient":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def connect(self) -> None:
        self._tx.open()
        self._handshake()

    def close(self) -> None:
        self._tx.close()

    # ---- 握手 -----------------------------------------------------------
    def _handshake(self) -> None:
        client_nonce = _b64url(os.urandom(16))
        self._tx.send({
            "type": "session_hello",
            "protocol_min": PROTOCOL,
            "protocol_max": PROTOCOL,
            "client_nonce": client_nonce,
            "ticket_id": ticket_id(self._ticket),
            "client": {"kind": "skill", "id": "wb-buddy-checkin", "version": "1.0.0"},
        })

        msg = self._tx.recv(self.timeout)
        if msg.get("type") == "session_hello_error":
            raise WbipcError(
                "E_AUTH" if msg.get("code") == "auth_failed" else "E_PROTOCOL_MISMATCH",
                "握手被拒绝：%s" % msg.get("code"),
            )
        if msg.get("type") != "session_challenge":
            raise WbipcError("E_PROTOCOL_MISMATCH", "期望 session_challenge，收到 %s" % msg.get("type"))

        server_nonce = msg.get("server_nonce") or ""
        if not server_nonce:
            raise WbipcError("E_PROTOCOL_MISMATCH", "session_challenge 缺少 server_nonce")

        # 反向校验服务端身份：确认对端确实持有同一份 ticket
        expect = compute_proof(self._ticket, "server", self.endpoint, client_nonce, server_nonce)
        if not hmac.compare_digest(expect, msg.get("server_proof") or ""):
            raise WbipcError("E_AUTH", "服务端 proof 校验失败，对端 ticket 不匹配")

        self._tx.send({
            "type": "session_prove",
            "client_proof": compute_proof(self._ticket, "client", self.endpoint,
                                          client_nonce, server_nonce),
        })

        ack = self._tx.recv(self.timeout)
        if ack.get("type") != "session_hello_ack":
            raise WbipcError("E_AUTH", "握手未完成：%s" % ack.get("type"))
        self.epoch = ack.get("connection_epoch") or ""
        self.pipes = ack.get("pipes") or []

    # ---- 调用 -----------------------------------------------------------
    def rpc(self, method: str, params: dict | None = None, timeout: float | None = None,
            mode: str | None = None):
        self._seq += 1
        req_id = self._seq
        frame = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params or {},
        }
        if mode:
            frame["mode"] = mode
        self._tx.send(frame)
        deadline = timeout or self.timeout
        while True:
            msg = self._tx.recv(deadline)
            if msg.get("id") != req_id:
                continue  # 通知 / 迟到响应，丢弃
            if "error" in msg:
                err = msg["error"] or {}
                raise WbipcError(err.get("code") or "E_INTERNAL",
                                 err.get("message") or "RPC 失败", err.get("data"))
            return msg.get("result")

    def open_channel(self, pipe: str) -> tuple[str, list]:
        """向 broker 申请一条管道通道，拿到形如 ``c:<pipe>`` 的通道名。"""
        cached = self._channels.get(pipe)
        if cached:
            return cached
        result = self.rpc("broker/GetPipe", {"pipe": pipe}) or {}
        channel = result.get("channel") or ""
        if not channel:
            raise WbipcError("E_PIPE_UNKNOWN", "申请通道 %s 失败：%s" % (pipe, result))
        entry = (channel, result.get("methods") or [])
        self._channels[pipe] = entry
        return entry

    def http(self, method: str, path: str, body: dict | None = None,
             query: dict | None = None, headers: dict | None = None,
             timeout: float = 25.0, check_status: bool = True) -> dict:
        """通过客户端代发一次 HTTP 请求。

        鉴权头由客户端按它自己的登录态附加，本函数不接触任何凭据。
        返回解析后的 JSON；非 2xx 或非 JSON 一律抛 WbipcError。

        check_status=False 时不因 4xx 抛错，而是把响应体交回上层 ——
        WorkBuddy 的业务拒绝（例如「今天已签到」）正是用 HTTP 400 + 业务码表达的。
        """
        channel, _methods = self.open_channel(PIPE_HTTP)

        params: dict = {"method": method.upper(), "path": path}
        if query:
            params["query"] = query
        if headers:
            params["headers"] = headers
        if body is not None:
            raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
            params["body_b64"] = base64.b64encode(raw).decode("ascii")

        result = self.rpc("%s/%s" % (channel, METHOD_HTTP), params,
                          timeout=timeout, mode="call") or {}
        status = result.get("status")
        raw_b64 = result.get("body_b64") or ""
        text = ""
        if raw_b64:
            try:
                text = base64.b64decode(raw_b64).decode("utf-8", "replace")
            except (ValueError, TypeError):
                text = ""
        if isinstance(status, int) and not 200 <= status < 300 and check_status:
            raise WbipcError("E_UPSTREAM", "HTTP %s：%s" % (status, text[:200]))
        try:
            return json.loads(text) if text else {}
        except ValueError:
            raise WbipcError("E_UPSTREAM", "响应不是 JSON：%s" % text[:200])


def probe() -> tuple[bool, str]:
    """只做一次连接+握手尝试，用于自检。"""
    try:
        with WbipcClient():
            return True, ""
    except WbipcError as exc:
        return False, "%s: %s" % (exc.code, exc)


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    ok, why = probe()
    print("连接：%s %s" % ("可用" if ok else "不可用", why))
    if ok:
        with WbipcClient() as cli:
            print("epoch:", cli.epoch)
            print("可见管道:", json.dumps(cli.pipes, ensure_ascii=False)[:400])
