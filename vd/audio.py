"""音频输入源。

所有输入源统一输出 16 kHz、单声道、float32（幅度范围 [-1, 1]）的 PCM 分片，
上层不需要关心来源是麦克风、文件还是网络流。
"""

from __future__ import annotations

import socket
import sys
import time
import wave
from pathlib import Path
from typing import Iterator

import numpy as np

SAMPLE_RATE = 16000


def resample_linear(samples: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """线性插值重采样。够用于语音特征提取，比引入 scipy 轻。"""
    if src_rate == dst_rate or samples.size == 0:
        return samples.astype(np.float32, copy=False)
    n_out = int(round(samples.size * dst_rate / src_rate))
    if n_out <= 0:
        return np.zeros(0, dtype=np.float32)
    x_old = np.linspace(0.0, 1.0, num=samples.size, endpoint=False)
    x_new = np.linspace(0.0, 1.0, num=n_out, endpoint=False)
    return np.interp(x_new, x_old, samples).astype(np.float32)


class AudioSource:
    """音频源基类。"""

    sample_rate = SAMPLE_RATE

    def chunks(self, chunk_ms: int = 100) -> Iterator[np.ndarray]:
        raise NotImplementedError

    def close(self) -> None:
        pass

    def describe(self) -> str:
        return type(self).__name__


def list_capture_devices() -> list[str]:
    """列出 ALSA 录音设备（来自 /proc/asound/pcm）。

    没有这个内核接口就返回空列表——WSL2、容器、云主机上通常就是这样，
    这时候程序没法直接用本机麦克风，得改走 file / stdin / tcp。
    """
    pcm = Path("/proc/asound/pcm")
    if not pcm.is_file():
        return []
    devices = []
    for line in pcm.read_text().splitlines():
        if "capture" in line:
            devices.append(line.strip())
    return devices



class AlsaMic(AudioSource):
    """ALSA 录音设备。

    用 sherpa-onnx 自带的 ALSA 绑定（静态链接了 libasound），不额外依赖
    sounddevice / portaudio。WSL2、桌面 Linux 均可直接用。
    """

    def __init__(self, device_name: str = "default", sample_rate: int = SAMPLE_RATE):
        import sherpa_onnx

        self._alsa = sherpa_onnx.Alsa(device_name)
        self._device_name = device_name
        actual = self._alsa.actual_sample_rate
        self.sample_rate = int(actual) if actual else sample_rate

    def chunks(self, chunk_ms: int = 100) -> Iterator[np.ndarray]:
        n = max(1, int(self.sample_rate * chunk_ms / 1000))
        while True:
            samples = self._alsa.read(n)
            if not samples:
                break
            yield np.asarray(samples, dtype=np.float32)

    def describe(self) -> str:
        return f"ALSA 设备 {self._device_name} @ {self.sample_rate} Hz"


class WavFile(AudioSource):
    """本地 WAV 文件。

    ``realtime=True`` 时按 1 倍速喂数据，用来在没有麦克风的机器上验证
    「实时」这条链路是否正常（解码节奏、增量输出、切句时机）。
    """

    def __init__(self, path: str, realtime: bool = False, sample_rate: int = SAMPLE_RATE):
        self._path = path
        self._realtime = realtime
        with wave.open(path, "rb") as w:
            src_rate = w.getframerate()
            n_channels = w.getnchannels()
            width = w.getsampwidth()
            raw = w.readframes(w.getnframes())
        if width != 2:
            raise ValueError(f"{path}: 只支持 16 bit PCM，实际 {width * 8} bit")
        data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        if n_channels > 1:
            data = data.reshape(-1, n_channels).mean(axis=1)
        self._data = resample_linear(data, src_rate, sample_rate)
        self.sample_rate = sample_rate

    def chunks(self, chunk_ms: int = 100) -> Iterator[np.ndarray]:
        n = max(1, int(self.sample_rate * chunk_ms / 1000))
        next_at = time.monotonic()
        for pos in range(0, len(self._data), n):
            chunk = self._data[pos : pos + n]
            if self._realtime:
                next_at += chunk_ms / 1000.0
                delay = next_at - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
            if len(chunk):
                yield chunk

    def describe(self) -> str:
        mode = "实时节奏" if self._realtime else "全速"
        return f"WAV 文件 {self._path}（{mode}，{len(self._data) / self.sample_rate:.1f}s）"


class RawStdin(AudioSource):
    """从标准输入读 16 kHz / 单声道 / s16le 裸 PCM。

    方便把任意采集程序的输出接进来，例如::

        arecord -t raw -f S16_LE -r 16000 -c 1 | python3 dictation.py --source stdin
    """

    def __init__(self, sample_rate: int = SAMPLE_RATE):
        self.sample_rate = sample_rate

    def chunks(self, chunk_ms: int = 100) -> Iterator[np.ndarray]:
        n = max(1, int(self.sample_rate * chunk_ms / 1000))
        buf = bytearray()
        stream = sys.stdin.buffer
        while True:
            data = stream.read(n * 2)
            if not data:
                break
            buf.extend(data)
            while len(buf) >= n * 2:
                block = bytes(buf[: n * 2])
                del buf[: n * 2]
                yield np.frombuffer(block, dtype=np.int16).astype(np.float32) / 32768.0
        if buf:
            yield np.frombuffer(bytes(buf), dtype=np.int16).astype(np.float32) / 32768.0

    def describe(self) -> str:
        return "标准输入（16 kHz s16le PCM）"


class TcpServer(AudioSource):
    """监听 TCP，接受一个客户端推送 16 kHz / 单声道 / s16le PCM。

    用于「麦克风在别的机器上、识别在这台机器上」的场景，例如手机 App
    或者另一台装了声卡的电脑，通过 ``scripts/send_mic.py`` 把音频推过来。
    """

    def __init__(self, host: str = "0.0.0.0", port: int = 8765, sample_rate: int = SAMPLE_RATE):
        self.sample_rate = sample_rate
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((host, port))
        self._sock.listen(1)

    def chunks(self, chunk_ms: int = 100) -> Iterator[np.ndarray]:
        print(f"[TCP] 等待客户端连接 {self._sock.getsockname()[:2]} ...", flush=True)
        conn, addr = self._sock.accept()
        print(f"[TCP] 已连接 {addr[0]}:{addr[1]}", flush=True)
        n = max(1, int(self.sample_rate * chunk_ms / 1000))
        buf = bytearray()
        try:
            while True:
                data = conn.recv(n * 2)
                if not data:
                    break
                buf.extend(data)
                while len(buf) >= n * 2:
                    block = bytes(buf[: n * 2])
                    del buf[: n * 2]
                    yield np.frombuffer(block, dtype=np.int16).astype(np.float32) / 32768.0
            if buf:
                yield np.frombuffer(bytes(buf), dtype=np.int16).astype(np.float32) / 32768.0
        finally:
            conn.close()

    def close(self) -> None:
        self._sock.close()

    def describe(self) -> str:
        return f"TCP 推流 {self._sock.getsockname()[:2]}"
