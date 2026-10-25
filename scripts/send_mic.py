#!/usr/bin/env python3
"""把本机麦克风推给另一台机器上的听写程序（--source tcp 模式）。

用法（在有麦克风的机器上跑）::

    python3 scripts/send_mic.py --host 192.168.6.10 --port 8765

需要 sounddevice（读麦克风）::

    pip install sounddevice

也可以直接推一个 wav 文件做联调::

    python3 scripts/send_mic.py --host 192.168.6.10 --port 8765 --file a.wav --realtime
"""

from __future__ import annotations

import argparse
import socket
import sys
import time
import wave

import numpy as np

SAMPLE_RATE = 16000


def mic_chunks(duration_ms: int):
    import sounddevice as sd

    with sd.InputStream(channels=1, dtype="int16", samplerate=SAMPLE_RATE) as stream:
        while True:
            data, _ = stream.read(int(SAMPLE_RATE * duration_ms / 1000))
            yield np.asarray(data, dtype=np.int16)


def wav_chunks(path: str, duration_ms: int, realtime: bool):
    with wave.open(path, "rb") as w:
        rate = w.getframerate()
        channels = w.getnchannels()
        raw = w.readframes(w.getnframes())
    data = np.frombuffer(raw, dtype=np.int16)
    if channels > 1:
        data = data.reshape(-1, channels)[:, 0]
    if rate != SAMPLE_RATE and rate > 0:
        n_out = int(len(data) * SAMPLE_RATE / rate)
        data = np.interp(
            np.linspace(0, len(data) - 1, n_out), np.arange(len(data)), data
        ).astype(np.int16)
    step = int(SAMPLE_RATE * duration_ms / 1000)
    next_at = time.monotonic()
    for pos in range(0, len(data), step):
        block = data[pos : pos + step]
        if realtime:
            next_at += duration_ms / 1000.0
            delay = next_at - time.monotonic()
            if delay > 0:
                time.sleep(delay)
        yield block


def stdin_chunks():
    """把标准输入的 16k s16le PCM 原样转发出去。"""
    while True:
        data = sys.stdin.buffer.read(3200)
        if not data:
            break
        yield np.frombuffer(data, dtype=np.int16)


def main() -> int:
    p = argparse.ArgumentParser(description="把麦克风音频推到听写服务器")
    p.add_argument("--host", default="127.0.0.1", help="听写服务器地址")
    p.add_argument("--port", type=int, default=8765, help="听写服务器端口")
    p.add_argument("--file", default="", help="改推 wav 文件而不是麦克风")
    p.add_argument("--realtime", action="store_true", help="推文件时按 1 倍速")
    p.add_argument("--chunk-ms", type=int, default=100, help="每次发送的长度")
    p.add_argument(
        "--stdin",
        action="store_true",
        help="从标准输入转发 16k s16le PCM（配合 ffmpeg 等采集程序）",
    )
    args = p.parse_args()

    if args.stdin:
        chunks = stdin_chunks()
    elif args.file:
        chunks = wav_chunks(args.file, args.chunk_ms, args.realtime)
    else:
        chunks = mic_chunks(args.chunk_ms)

    sock = socket.create_connection((args.host, args.port))
    print(f"已连接 {args.host}:{args.port}，开始推流（Ctrl+C 结束）", flush=True)
    try:
        for block in chunks:
            sock.sendall(block.astype(np.int16).tobytes())
    except KeyboardInterrupt:
        print("\n结束推流", flush=True)
    finally:
        sock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
