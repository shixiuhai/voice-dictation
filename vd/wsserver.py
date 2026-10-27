"""最小 WebSocket 服务端 + 静态 HTTP 服务：浏览器当麦克风，顺便当配置页。

为什么需要它：这台 WSL2 没有声卡，Windows 上又常常没装 Python / ffmpeg。
浏览器（Edge / Chrome 自带）能用 getUserMedia 采麦克风，再用 WebSocket 把
16 kHz s16le PCM 推过来，Windows 侧**零安装**。

协议只用到最小子集：HTTP/1.1 升级握手 + 二进制帧（客户端->服务端带掩码）。

服务端分成两半：

- **HTTP**：页面、状态、历史清单、记录下载。跑在后台线程里，端口一绑上就能访问，
  不用等模型加载完（实测要 15 秒），页面先显示「模型加载中」。
- **WebSocket**：音频流 + 控制指令（改名、接着写）。每个连接一个线程，
  互相不阻塞；刷新页面、多开标签页都不会把服务卡住。

浏览器 -> 服务端：二进制帧 = 16 kHz s16le PCM；文本帧 = JSON 控制指令。
服务端 -> 浏览器：文本帧 = JSON（hello / partial / segment / status / ...）。
"""

from __future__ import annotations

import base64
import hashlib
import json
import queue
import socket
import struct
import threading
from pathlib import Path
from typing import Callable, Iterator
from urllib.parse import unquote, urlsplit

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OPCODE_CONT = 0x0
OPCODE_TEXT = 0x1
OPCODE_BINARY = 0x2
OPCODE_CLOSE = 0x8
OPCODE_PING = 0x9
OPCODE_PONG = 0xA

_MAX_HEADER = 64 * 1024
_ALLOWED_DOWNLOAD_EXT = (".md", ".txt", ".srt", ".json")

# 放进音频队列表示「输入到此为止」，客户端断开时用
_EOF = object()

# 客户端断开时补这么多秒静音，让识别器把当前这句收尾（同 vd/audio.py 的 TCP 源）
_TAIL_SILENCE = 0.5


def _recv_exact(conn: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("连接已断开")
        buf += chunk
    return buf


def read_frame(conn: socket.socket) -> tuple[int, bytes]:
    """读一个 WebSocket 帧，返回 (opcode, payload)。"""
    b1, b2 = _recv_exact(conn, 2)
    opcode = b1 & 0x0F
    masked = bool(b2 & 0x80)
    length = b2 & 0x7F
    if length == 126:
        length = struct.unpack(">H", _recv_exact(conn, 2))[0]
    elif length == 127:
        length = struct.unpack(">Q", _recv_exact(conn, 8))[0]
    mask = _recv_exact(conn, 4) if masked else b""
    payload = _recv_exact(conn, length) if length else b""
    if masked:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return opcode, payload


def send_frame(conn: socket.socket, payload: bytes, opcode: int = OPCODE_BINARY) -> None:
    """发一个不带掩码的帧（服务端->客户端）。"""
    n = len(payload)
    header = bytes([0x80 | opcode])
    if n < 126:
        header += bytes([n])
    elif n < 65536:
        header += bytes([126]) + struct.pack(">H", n)
    else:
        header += bytes([127]) + struct.pack(">Q", n)
    conn.sendall(header + payload)


class _Client:
    """一个 WebSocket 连接。发送加锁，避免多线程写同一个 socket 串帧。"""

    def __init__(self, sock: socket.socket, addr: tuple[str, int]):
        self.sock = sock
        self.addr = addr
        self.alive = True
        self.told_takeover = False
        self._lock = threading.Lock()

    def send(self, payload: bytes, opcode: int = OPCODE_BINARY) -> None:
        with self._lock:
            if not self.alive:
                return
            try:
                send_frame(self.sock, payload, opcode)
            except OSError:
                self.alive = False

    def send_json(self, obj: dict) -> None:
        self.send(json.dumps(obj, ensure_ascii=False).encode("utf-8"), OPCODE_TEXT)

    def close(self) -> None:
        self.alive = False
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


class MicWebServer:
    """HTTP 页面 + WebSocket 音频流。

    - ``GET /``                返回麦克风采集页面
    - ``GET /api/status``      服务器状态（模型就绪没、当前记录、句数）
    - ``GET /api/sessions``    历史记录清单
    - ``GET /download/<文件>`` 下载当前记录（md / txt / srt / json）
    - ``GET /ws``              WebSocket 升级，之后客户端推 16 kHz s16le PCM 二进制帧

    HTTP 线程在构造时就起来了，所以端口一绑上页面就能打开，不用等模型加载。
    """

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8765,
        page: str = "",
        sample_rate: int = 16000,
        once: bool = False,
    ):
        self.sample_rate = sample_rate
        self.once = once
        self._page = page.encode("utf-8")
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((host, port))
        self._sock.listen(16)
        self._sock.settimeout(0.5)

        self._lock = threading.Lock()
        self._clients: list[_Client] = []
        self._sender: _Client | None = None
        self._session = 0  # 会话代次，每换一个麦克风客户端 +1
        self._audio: queue.Queue = queue.Queue()
        self._stopped = threading.Event()
        self._ready = False

        # 浏览器用文本帧下发控制指令（比如改记录文件名）
        self.on_text: Callable[[str], str] | None = None
        # 状态（/api/status 和推给浏览器的 status 消息共用）
        self._status_provider: Callable[[], dict] | None = None
        # 历史记录清单（单独一个接口，因为读盘比读内存贵）
        self._sessions_provider: Callable[[], list] | None = None

        self._thread = threading.Thread(target=self._accept_loop, name="vd-http", daemon=True)
        self._thread.start()

    @property
    def address(self) -> tuple[str, int]:
        return self._sock.getsockname()[:2]

    @property
    def session(self) -> int:
        """会话代次，每换一个麦克风客户端 +1。"""
        with self._lock:
            return self._session

    def close(self) -> None:
        self._stopped.set()
        with self._lock:
            clients = list(self._clients)
            self._clients.clear()
        for client in clients:
            client.close()
        self._audio.put(_EOF)
        try:
            self._sock.close()
        except OSError:
            pass

    # ---------- 状态 ----------

    def set_ready(self, ready: bool = True) -> None:
        """模型加载完了没。页面据此决定要不要开麦。"""
        self._ready = ready

    def set_status_provider(self, fn: Callable[[], dict] | None) -> None:
        """注册一个返回状态字典的函数（/api/status 与推给浏览器的 status 都用它）。"""
        self._status_provider = fn

    def set_sessions_provider(self, fn: Callable[[], list] | None) -> None:
        """注册一个返回历史记录清单的函数。"""
        self._sessions_provider = fn

    def status(self) -> dict:
        data: dict = {"ready": self._ready}
        if self._status_provider is not None:
            try:
                data.update(self._status_provider())
            except Exception as e:  # 状态取不到不该拖垮服务
                print(f"[WS] 读取状态失败: {e}", flush=True)
        return data

    def sessions(self) -> list:
        if self._sessions_provider is None:
            return []
        try:
            return self._sessions_provider()
        except Exception as e:
            print(f"[WS] 读取历史清单失败: {e}", flush=True)
            return []

    def broadcast(self, payload: dict) -> None:
        """把一条 JSON 消息推给所有连着的浏览器。"""
        with self._lock:
            clients = list(self._clients)
        for client in clients:
            client.send_json(payload)

    # ---------- HTTP ----------

    def _accept_loop(self) -> None:
        while not self._stopped.is_set():
            try:
                conn, addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(
                target=self._handle_conn, args=(conn, addr), name="vd-conn", daemon=True
            ).start()

    def _handle_conn(self, conn: socket.socket, addr: tuple[str, int]) -> None:
        try:
            conn.settimeout(30)
            headers = self._read_request(conn)
            path = urlsplit(headers.get("__path", "/")).path
            if "websocket" in headers.get("upgrade", "").lower():
                conn.settimeout(None)
                self._ws_session(conn, addr, headers, path)
            else:
                self._serve_http(conn, path)
        except (ConnectionError, OSError, ValueError) as e:
            print(f"[WS] 连接结束 {addr[0]}:{addr[1]}: {e}", flush=True)
        except Exception as e:
            # 单个连接出错不该拖垮整个服务，但要留下痕迹
            print(f"[WS] 处理连接时出错 {addr[0]}:{addr[1]}: {e!r}", flush=True)
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _read_request(self, conn: socket.socket) -> dict[str, str]:
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = conn.recv(4096)
            if not chunk:
                break
            data += chunk
            if len(data) > _MAX_HEADER:
                raise ValueError("请求头太大")
        lines = data.decode("latin-1", errors="ignore").split("\r\n")
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        headers["__path"] = lines[0].split(" ")[1] if lines and " " in lines[0] else "/"
        return headers

    def _serve_http(self, conn: socket.socket, path: str) -> None:
        if path in ("/", "/index.html"):
            self._reply(conn, 200, "text/html; charset=utf-8", self._page)
        elif path == "/api/status":
            body = json.dumps(self.status(), ensure_ascii=False).encode("utf-8")
            self._reply(conn, 200, "application/json; charset=utf-8", body)
        elif path == "/api/sessions":
            body = json.dumps({"items": self.sessions()}, ensure_ascii=False).encode("utf-8")
            self._reply(conn, 200, "application/json; charset=utf-8", body)
        elif path.startswith("/download/"):
            self._serve_download(conn, path[len("/download/") :])
        elif path == "/favicon.ico":
            self._reply(conn, 204, "image/x-icon", b"")
        else:
            self._reply(conn, 404, "text/plain; charset=utf-8", b"404")

    def _serve_download(self, conn: socket.socket, name: str) -> None:
        name = unquote(name).strip()
        if not name or "/" in name or "\\" in name or ".." in name:
            self._reply(conn, 400, "text/plain; charset=utf-8", b"bad name")
            return
        out_dir = self.status().get("out_dir", "")
        if not out_dir:
            self._reply(conn, 503, "text/plain; charset=utf-8", b"not ready")
            return
        if not name.endswith(_ALLOWED_DOWNLOAD_EXT):
            self._reply(conn, 403, "text/plain; charset=utf-8", b"bad ext")
            return
        base = Path(out_dir).resolve()
        target = (base / name).resolve()
        if target.parent != base or not target.is_file():
            self._reply(conn, 404, "text/plain; charset=utf-8", b"no such file")
            return
        ctype = {
            ".md": "text/markdown; charset=utf-8",
            ".txt": "text/plain; charset=utf-8",
            ".srt": "text/plain; charset=utf-8",
            ".json": "application/json; charset=utf-8",
        }[target.suffix]
        body = target.read_bytes()
        self._reply(
            conn,
            200,
            ctype,
            body,
            extra={"Content-Disposition": f'attachment; filename="{target.name}"'},
        )

    def _reply(
        self,
        conn: socket.socket,
        code: int,
        ctype: str,
        body: bytes | str,
        extra: dict | None = None,
    ) -> None:
        if isinstance(body, str):
            body = body.encode("utf-8")
        reason = {
            200: "OK",
            204: "No Content",
            400: "Bad Request",
            403: "Forbidden",
            404: "Not Found",
            503: "Service Unavailable",
        }.get(code, "OK")
        head = [f"HTTP/1.1 {code} {reason}"]
        head.append(f"Content-Type: {ctype}")
        head.append(f"Content-Length: {len(body)}")
        head.append("Cache-Control: no-store")
        head.append("Connection: close")
        for k, v in (extra or {}).items():
            head.append(f"{k}: {v}")
        raw = ("\r\n".join(head) + "\r\n\r\n").encode("utf-8") + body
        try:
            conn.sendall(raw)
        except OSError:
            pass

    # ---------- WebSocket ----------

    def _ws_session(
        self,
        conn: socket.socket,
        addr: tuple[str, int],
        headers: dict[str, str],
        path: str,
    ) -> None:
        if path != "/ws":
            self._reply(conn, 404, "text/plain; charset=utf-8", b"404")
            return
        if not self._ready:
            # 模型还没加载完：先让页面等一会儿，别把刚打开的麦克风白费了
            self._reply(
                conn,
                503,
                "application/json; charset=utf-8",
                json.dumps({"error": "模型还在加载，请稍候重试"}, ensure_ascii=False).encode(
                    "utf-8"
                ),
            )
            return
        if not self._handshake(conn, headers):
            return

        client = _Client(conn, addr)
        with self._lock:
            self._clients.append(client)
        print(f"[WS] 浏览器已连接 {addr[0]}:{addr[1]}", flush=True)
        try:
            self._send_hello(client)
            self._ws_loop(client)
        finally:
            with self._lock:
                if client in self._clients:
                    self._clients.remove(client)
                was_sender = self._sender is client
                if was_sender:
                    self._sender = None
            client.close()
            if was_sender:
                # 补一段静音让当前这句自然收尾：跟 TCP 源一个道理，
                # 断开时识别器还欠着音频没吐字，不补的话最后一句永远出不来。
                # （final 模式靠静音触发断句，VAD 模式靠静音结段。）
                self._audio.put(
                    b"\x00" * (int(self.sample_rate * _TAIL_SILENCE) * 2)
                )
            print(
                "[WS] 浏览器断开，记录已实时保存（再打开页面可继续，Ctrl+C 结束）",
                flush=True,
            )
            if self.once:
                self._audio.put(_EOF)

    def _handshake(self, conn: socket.socket, headers: dict[str, str]) -> bool:
        key = headers.get("sec-websocket-key")
        if not key:
            self._reply(conn, 400, "text/plain; charset=utf-8", b"bad handshake")
            return False
        accept = base64.b64encode(
            hashlib.sha1((key + WS_GUID).encode()).digest()
        ).decode()
        conn.sendall(
            (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
            ).encode()
        )
        return True

    def _send_hello(self, client: _Client) -> None:
        status = self.status()
        client.send_json(
            {
                "cmd": "hello",
                "status": status,
                "transcript": status.get("transcript", []),
                "sessions": self.sessions(),
            }
        )

    def _ws_loop(self, client: _Client) -> None:
        while True:
            opcode, payload = read_frame(client.sock)
            if opcode == OPCODE_CLOSE:
                break
            if opcode == OPCODE_PING:
                client.send(payload, OPCODE_PONG)
                continue
            if opcode == OPCODE_TEXT:
                text = payload.decode("utf-8", errors="replace")
                print(f"[WS] 收到文本: {text[:100]!r}", flush=True)
                reply = None
                if self.on_text is not None:
                    try:
                        reply = self.on_text(text)
                    except Exception as e:  # 控制指令出错不该拖垮听写
                        print(f"[WS] 处理文本帧失败: {e}", flush=True)
                if reply:
                    client.send(reply.encode("utf-8"), OPCODE_TEXT)
                self._broadcast_status()
                continue
            if opcode == OPCODE_BINARY:
                self._push_audio(client, payload)

    def _push_audio(self, client: _Client, payload: bytes) -> None:
        """收音频。只有「当前麦克风」那个连接的音频算数。

        麦克风归属不抢：先来的那个一直是麦克风，后来者只收到提示，
        免得两个标签页互相抢、音频来回打架。
        """
        with self._lock:
            if self._sender is None:
                self._sender = client
                self._session += 1  # 换人了：引擎据此清掉跨连接复用的状态
            is_sender = self._sender is client
        if not is_sender:
            if not client.told_takeover:
                client.told_takeover = True
                client.send_json(
                    {"cmd": "superseded", "message": "这个页面已有别的标签页在用麦克风"}
                )
            return
        if payload:
            self._audio.put(payload)

    def _broadcast_status(self) -> None:
        self.broadcast({"cmd": "status", "status": self.status()})

    # ---------- 音频 ----------

    def chunks(self, chunk_ms: int = 100) -> Iterator:
        """从队列里取音频，凑够 chunk_ms 毫秒给上层。"""
        import numpy as np

        want = max(2, int(self.sample_rate * chunk_ms / 1000) * 2)
        buf = bytearray()
        while True:
            try:
                block = self._audio.get(timeout=0.2)
            except queue.Empty:
                continue
            if block is _EOF:
                break
            buf.extend(block)
            while len(buf) >= want:
                piece = bytes(buf[:want])
                del buf[:want]
                yield np.frombuffer(piece, dtype=np.int16).astype(np.float32) / 32768.0
        # 断开前最后那点零头也要送出去，别白白丢掉最后几百毫秒
        if len(buf) % 2:
            buf = buf[:-1]
        if buf:
            yield np.frombuffer(bytes(buf), dtype=np.int16).astype(np.float32) / 32768.0
