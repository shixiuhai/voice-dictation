"""最小 WebSocket 服务端：让浏览器直接把麦克风音频推进来。

为什么需要它：这台 WSL2 没有声卡，Windows 上又常常没装 Python / ffmpeg。
浏览器（Edge / Chrome 自带）能用 getUserMedia 采麦克风，再用 WebSocket 把
16 kHz s16le PCM 推过来，Windows 侧**零安装**。

协议只用到最小子集：HTTP/1.1 升级握手 + 二进制帧（客户端->服务端带掩码）。
"""

from __future__ import annotations

import base64
import hashlib
import socket
import struct
from typing import Iterator

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OPCODE_CONT = 0x0
OPCODE_TEXT = 0x1
OPCODE_BINARY = 0x2
OPCODE_CLOSE = 0x8
OPCODE_PING = 0x9
OPCODE_PONG = 0xA


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


class MicWebServer:
    """HTTP 页面 + WebSocket 音频流。

    - ``GET /``          返回麦克风采集页面
    - ``GET /ws``        WebSocket 升级，之后客户端推 16 kHz s16le PCM 二进制帧
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
        self._sock.listen(4)
        self._conn: socket.socket | None = None
        # 浏览器可以用文本帧下发控制指令（比如改记录文件名）
        self.on_text = None
        # 浏览器连上时先推一条消息（比如历史记录清单）
        self.on_connect = None

    @property
    def address(self) -> tuple[str, int]:
        return self._sock.getsockname()[:2]

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except OSError:
                pass
        self._sock.close()

    def _read_request(self, conn: socket.socket) -> dict[str, str]:
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = conn.recv(4096)
            if not chunk:
                break
            data += chunk
        lines = data.decode("latin-1", errors="ignore").split("\r\n")
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        headers["__path"] = lines[0].split(" ")[1] if lines and " " in lines[0] else "/"
        return headers

    def _serve_page(self, conn: socket.socket) -> None:
        conn.sendall(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/html; charset=utf-8\r\n"
            b"Content-Length: " + str(len(self._page)).encode() + b"\r\n"
            b"Cache-Control: no-store\r\n"
            b"Connection: close\r\n\r\n" + self._page
        )

    def _handshake(self, conn: socket.socket, headers: dict[str, str]) -> bool:
        key = headers.get("sec-websocket-key")
        if not key:
            conn.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
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

    def chunks(self, chunk_ms: int = 100) -> Iterator[bytes]:
        """先服务网页，再等 WebSocket 客户端推 PCM 进来。"""
        import numpy as np

        want = int(self.sample_rate * chunk_ms / 1000) * 2
        while True:
            conn, addr = self._sock.accept()
            try:
                headers = self._read_request(conn)
                path = headers.get("__path", "/")
                if "websocket" not in headers.get("upgrade", "").lower():
                    self._serve_page(conn)
                    continue
                if path != "/ws":
                    conn.sendall(b"HTTP/1.1 404 Not Found\r\n\r\n")
                    continue
                if not self._handshake(conn, headers):
                    continue
                print(f"[WS] 浏览器已连接 {addr[0]}:{addr[1]}", flush=True)
                self._conn = conn
                if self.on_connect is not None:
                    try:
                        payload = self.on_connect()
                        if payload:
                            send_frame(conn, payload.encode("utf-8"), OPCODE_TEXT)
                    except Exception as e:
                        print(f"[WS] 推送初始消息失败: {e}", flush=True)
                buf = bytearray()
                while True:
                    opcode, payload = read_frame(conn)
                    if opcode == OPCODE_CLOSE:
                        break
                    if opcode == OPCODE_PING:
                        send_frame(conn, payload, OPCODE_PONG)
                        continue
                    if opcode == OPCODE_TEXT:
                        text = payload.decode("utf-8", errors="replace")
                        print(f"[WS] 收到文本: {text[:100]!r}", flush=True)
                        if self.on_text is not None:
                            try:
                                reply = self.on_text(text)
                            except Exception as e:  # 控制指令出错不该拖垮听写
                                print(f"[WS] 处理文本帧失败: {e}", flush=True)
                                reply = None
                            if reply:
                                send_frame(conn, reply.encode("utf-8"), OPCODE_TEXT)
                        continue
                    buf.extend(payload)
                    while len(buf) >= want:
                        block = bytes(buf[:want])
                        del buf[:want]
                        yield np.frombuffer(block, dtype=np.int16).astype(np.float32) / 32768.0
                print("[WS] 浏览器断开，记录已实时保存（再打开页面可继续，Ctrl+C 结束）", flush=True)
            except (ConnectionError, OSError) as e:
                print(f"[WS] 连接结束: {e}", flush=True)
            finally:
                try:
                    conn.close()
                except OSError:
                    pass
                if self._conn is conn:
                    self._conn = None
            if self.once:
                return
