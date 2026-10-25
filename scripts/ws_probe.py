#!/usr/bin/env python3
"""模拟浏览器的 WebSocket 客户端，用来端到端验证 ws 方案。

不依赖第三方库：手搓握手和帧，跟浏览器走同一条协议。

用法::

    python3 scripts/ws_probe.py --file samples/1.wav --realtime
    python3 scripts/ws_probe.py --file samples/1.wav --speed 5
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import struct
import sys
import time
import wave

SAMPLE_RATE = 16000
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("连接已断开")
        buf += chunk
    return buf


def read_frame(sock: socket.socket) -> tuple[int, bytes]:
    b1, b2 = recv_exact(sock, 2)
    opcode = b1 & 0x0F
    masked = bool(b2 & 0x80)
    length = b2 & 0x7F
    if length == 126:
        length = struct.unpack(">H", recv_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack(">Q", recv_exact(sock, 8))[0]
    mask = recv_exact(sock, 4) if masked else b""
    payload = recv_exact(sock, length) if length else b""
    if masked:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return opcode, payload


def send_frame(sock: socket.socket, payload: bytes, opcode: int = 0x2, mask: bool = True) -> None:
    """浏览器发给服务端的帧是带掩码的。"""
    n = len(payload)
    header = bytes([0x80 | opcode])
    maskbit = 0x80 if mask else 0x00
    if n < 126:
        header += bytes([maskbit | n])
    elif n < 65536:
        header += bytes([maskbit | 126]) + struct.pack(">H", n)
    else:
        header += bytes([maskbit | 127]) + struct.pack(">Q", n)
    if mask:
        key = os.urandom(4)
        header += key
        payload = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
    sock.sendall(header + payload)


def handshake(sock: socket.socket, host: str, port: int, path: str = "/ws") -> None:
    key = base64.b64encode(os.urandom(16)).decode()
    req = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n"
    )
    sock.sendall(req.encode())
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("握手阶段连接被关闭")
        data += chunk
    head = data.split(b"\r\n\r\n")[0].decode("latin-1")
    if "101" not in head.split("\r\n")[0]:
        body = data.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in data else b""
        raise ConnectionError(f"握手失败: {head.splitlines()[0]} {body[:200]!r}")


def wav_pcm(path: str) -> bytes:
    with wave.open(path, "rb") as w:
        rate = w.getframerate()
        width = w.getsampwidth()
        channels = w.getnchannels()
        raw = w.readframes(w.getnframes())
    if width != 2:
        raise ValueError(f"{path}: 只支持 16 bit PCM")
    if rate != SAMPLE_RATE:
        import numpy as np

        data = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
        if channels > 1:
            data = data.reshape(-1, channels).mean(axis=1)
        n_out = int(len(data) * SAMPLE_RATE / rate)
        data = np.interp(
            np.linspace(0, len(data) - 1, n_out), np.arange(len(data)), data
        ).astype(np.int16)
        raw = data.tobytes()
    return raw


def main() -> int:
    ap = argparse.ArgumentParser(description="模拟浏览器的 ws 客户端")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--file", default="samples/1.wav", help="要推的音频")
    ap.add_argument("--realtime", action="store_true", help="按 1 倍速推")
    ap.add_argument("--speed", type=float, default=0.0, help="推流倍速（>1 更快）")
    ap.add_argument("--name", default="", help="推完后改记录名")
    ap.add_argument("--wait", type=float, default=3.0, help="推完后等多久再断开")
    ap.add_argument(
        "--silence",
        type=float,
        default=1.5,
        help="音频后面补多少秒静音（VAD 靠静音切句，真实麦克风会一直有）",
    )
    args = ap.parse_args()

    pcm = wav_pcm(args.file)
    print(f"音频: {args.file}  {len(pcm) / 2 / SAMPLE_RATE:.1f}s")

    sock = socket.create_connection((args.host, args.port))
    sock.settimeout(10)
    handshake(sock, args.host, args.port)
    print("握手成功", flush=True)

    speed = args.speed if args.speed > 0 else (1.0 if args.realtime else 0.0)
    chunk = 3200  # 100 ms
    next_at = time.monotonic()
    pos = 0
    while pos < len(pcm):
        block = pcm[pos : pos + chunk]
        pos += chunk
        send_frame(sock, block, 0x2)
        if speed > 0:
            next_at += chunk / 2 / SAMPLE_RATE / speed
            delay = next_at - time.monotonic()
            if delay > 0:
                time.sleep(delay)

    # 补一段静音，让 VAD 把最后一句收尾（真实麦克风本来就会一直推静音）
    if args.silence > 0:
        quiet = b"\x00" * 3200
        n_quiet = int(args.silence * SAMPLE_RATE / 1600)
        for _ in range(n_quiet):
            send_frame(sock, quiet, 0x2)
            if speed > 0:
                next_at += 0.1 / speed
                delay = next_at - time.monotonic()
                if delay > 0:
                    time.sleep(delay)

    print(f"音频推完，等待 {args.wait}s 让最后一句收尾…", flush=True)

    # 收服务端推来的消息
    sock.settimeout(args.wait)
    deadline = time.monotonic() + args.wait
    got_text = []
    while time.monotonic() < deadline:
        try:
            opcode, payload = read_frame(sock)
        except socket.timeout:
            break
        if opcode == 0x1:
            try:
                msg = json.loads(payload.decode("utf-8"))
            except ValueError:
                print("收到非 JSON 文本:", payload[:200])
                continue
            cmd = msg.get("cmd")
            if cmd == "segment":
                got_text.append(msg.get("text", ""))
                print(f"[segment] {msg.get('start'):.1f}-{msg.get('end'):.1f} {msg.get('text')}")
            elif cmd == "partial":
                print(f"[partial] {msg.get('text','')}")
            else:
                print(f"[{cmd}] {json.dumps(msg, ensure_ascii=False)[:160]}")

    if args.name:
        send_frame(
            sock, json.dumps({"cmd": "name", "name": args.name}).encode("utf-8"), 0x1
        )
        time.sleep(0.5)

    sock.close()
    print(f"\n共收到 {len(got_text)} 句：")
    for t in got_text:
        print("  " + t)
    return 0


if __name__ == "__main__":
    sys.exit(main())
