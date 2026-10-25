"""音频输入源。

所有输入源统一输出 16 kHz、单声道、float32（幅度范围 [-1, 1]）的 PCM 分片，
上层不需要关心来源是麦克风、文件还是网络流。
"""

from __future__ import annotations

import collections
import socket
import struct
import sys
import time
import wave
from pathlib import Path
from typing import Iterator

import numpy as np

SAMPLE_RATE = 16000

# WAV 头最多攒这么多字节；还认不出来就当裸 PCM 处理
_WAV_HEAD_MAX = 4096

# 客户端断开时补多少秒静音，好让 VAD 把当前这句结掉
_TAIL_SILENCE = 0.5


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

    def push_event(self, payload: dict) -> None:
        """把一条事件推给前端（只有 ws 源有前端，其余源忽略）。"""

    def set_ready(self, ready: bool = True) -> None:
        """音频源是否已就绪（ws 源用它表示模型加载完了没）。"""

    def set_status_provider(self, fn) -> None:
        """注册一个返回状态字典的函数，给前端的 /api/status 用。"""

    def set_sessions_provider(self, fn) -> None:
        """注册一个返回历史记录清单的函数。"""


def list_capture_devices() -> list[str]:
    """列出 ALSA 录音设备（来自 /proc/asound/pcm）。

    没有这个内核接口就返回空列表——WSL2、容器、云主机上通常就是这样，
    这时候程序没法直接用本机麦克风，得改走 file / stdin / tcp / pulse。
    """
    pcm = Path("/proc/asound/pcm")
    if not pcm.is_file():
        return []
    devices = []
    for line in pcm.read_text().splitlines():
        if "capture" in line:
            devices.append(line.strip())
    return devices


def list_pulse_sources() -> list[str]:
    """列出 PulseAudio 的采集源（需要 pactl，pulseaudio-utils 自带）。"""
    import shutil
    import subprocess

    if shutil.which("pactl") is None:
        return []
    try:
        out = subprocess.run(
            ["pactl", "list", "short", "sources"],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
    except Exception:
        return []
    sources = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[1] != "module-null-sink":
            sources.append(f"{parts[1]}（{parts[0]}）{parts[2] if len(parts) > 2 else ''}")
    return sources


def list_sounddevice_inputs() -> list[str]:
    """列出 sounddevice 看到的输入设备（跨平台）。"""
    try:
        import sounddevice as sd
    except ImportError:
        return []
    try:
        devices = sd.query_devices()
    except Exception:
        return []
    return [
        f"{i}: {d['name']}（{d.get('max_input_channels', 0)} 通道）"
        for i, d in enumerate(devices)
        if d.get("max_input_channels", 0) > 0
    ]



class LevelNormalizer:
    """电平归一化：只在输入**整体偏轻**时往上补增益，语音本身逐采样不变。

    为什么需要它：Silero VAD 的 ``threshold`` 判的是「语音概率」，但输入电平
    太低时模型输出的概率整体偏低，阈值根本跨不过去——表现是**一句都不出**，
    而不是出得不准。实测（``samples/1.wav`` 逐级衰减）：

        衰减      电平        VAD=0.5
        -24 dB   -48.9 dBFS   正常
        -30 dB   -54.9 dBFS   开始漏字
        -36 dB   -60.9 dBFS   0 句，完全没有输出

    补上增益后一直到约 -79 dBFS 都能正常切句。

    这里的判据是**「最近有没有够响的内容」**（``healthy_window`` 秒内的峰值），
    而不是瞬时电平。这一点很关键：早先用「瞬时 RMS 对目标值归一化」，正常语音
    句间的停顿也会被补上近 20 倍增益（停顿处 RMS 很低），把「today is 礼拜二」
    识别成了「today is li 巴 a」。现在只要最近 5 秒内出现过 ≥0.1 的峰值，
    增益就锁死在 1.0，语音一个采样都不动（实测 `samples/*.wav` 的语音帧
    0 个被改动，被改的只有开头那 0.8 秒静音）。

    另外两个刻意设计：

    - **只补不削**，增益夹在 ``[1, max_gain]``，不会引入电平归一化的抽吸失真。
    - **降增益立刻生效、升增益限速**（``max_rise``），既防削顶也防抽吸。

    底噪会不会被放大成「假语音」？实测纯噪声在 -30 ~ -75 dBFS 各档放大后
    仍然是 0 句。
    """

    def __init__(
        self,
        sample_rate: int = SAMPLE_RATE,
        frame_ms: int = 100,
        target_rms: float = 0.05,
        max_gain: float = 20.0,
        healthy_peak: float = 0.1,
        healthy_window: float = 5.0,
        release_tau: float = 0.5,
        max_rise: float = 2.0,
    ):
        self.frame = max(1, int(sample_rate * frame_ms / 1000))
        self.target_rms = target_rms
        self.max_gain = max(1.0, float(max_gain))
        # 最近 healthy_window 秒内出现过这么高的峰值，就认为电平健康，不再补增益
        self.healthy_peak = healthy_peak
        self._peaks = collections.deque(maxlen=max(1, int(healthy_window * 1000 / frame_ms)))
        self.release = float(np.exp(-(frame_ms / 1000.0) / release_tau))
        self.max_rise = max(1.0, float(max_rise))
        self.gain = 1.0
        self._follow = 0.0
        self._buf = np.zeros(0, dtype=np.float32)

    def process(self, samples: np.ndarray) -> np.ndarray:
        """返回与输入**等长**的音频（时间轴不动，时间戳才不会漂）。"""
        if samples.size == 0:
            return samples
        if self.max_gain <= 1.0:
            return samples
        self._buf = np.concatenate([self._buf, samples.astype(np.float32, copy=False)])
        n_block = len(self._buf) // self.frame
        processed = np.zeros(0, dtype=np.float32)
        if n_block:
            blocks = self._buf[: n_block * self.frame].reshape(n_block, self.frame)
            self._buf = self._buf[n_block * self.frame :]
            out = np.empty_like(blocks)
            for i in range(n_block):
                blk = blocks[i]
                rms = float(np.sqrt(np.mean(blk.astype(np.float64) ** 2)))
                # 「最近够不够响」看一段窗口内的最大峰值，而不是瞬时电平：
                # 否则正常语音句间的停顿（瞬时电平很低）也会被补上大增益
                self._peaks.append(float(np.max(np.abs(blk))))
                self._follow = max(rms, self._follow * self.release)
                if max(self._peaks) >= self.healthy_peak or self._follow <= 1e-6:
                    want = 1.0
                else:
                    want = min(max(self.target_rms / self._follow, 1.0), self.max_gain)
                # 降增益立刻生效（防削顶），升增益慢慢来（防抽吸）
                self.gain = want if want < self.gain else min(want, self.gain * self.max_rise)
                out[i] = blk * self.gain
            processed = np.clip(out, -1.0, 1.0).reshape(-1).astype(np.float32)
        # 不足一帧的余数用当前增益放行，绝不吞样本
        tail = self._buf * self.gain
        self._buf = np.zeros(0, dtype=np.float32)
        if len(tail):
            tail = np.clip(tail, -1.0, 1.0).astype(np.float32)
            return np.concatenate([processed, tail]) if len(processed) else tail
        return processed


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


class SoundDeviceMic(AudioSource):
    """用 sounddevice(PortAudio) 录音。

    这是跨平台的后端：Windows / macOS / 没有 ALSA 的 Linux 都能用。
    依赖 ``pip install sounddevice``（Windows/macOS 的轮子自带 portaudio）。
    """

    def __init__(self, device: str | int = "", sample_rate: int = SAMPLE_RATE):
        import sounddevice as sd

        self._sd = sd
        if device == "":
            index = None
        elif isinstance(device, int) or str(device).isdigit():
            index = int(device)
        else:
            index = self._find_by_name(device)
        self._index = index
        self.sample_rate = sample_rate
        self._stream = sd.InputStream(
            samplerate=sample_rate, channels=1, dtype="int16", device=index
        )
        self._stream.start()

    def _find_by_name(self, name: str) -> int:
        for i, dev in enumerate(self._sd.query_devices()):
            if name.lower() in dev["name"].lower():
                return i
        raise ValueError(f"找不到音频输入设备: {name}")

    def chunks(self, chunk_ms: int = 100) -> Iterator[np.ndarray]:
        n = max(1, int(self.sample_rate * chunk_ms / 1000))
        while True:
            data, _ = self._stream.read(n)
            yield np.asarray(data, dtype=np.float32).reshape(-1) / 32768.0

    def close(self) -> None:
        try:
            self._stream.stop()
            self._stream.close()
        except Exception:
            pass

    def describe(self) -> str:
        name = "默认设备"
        if self._index is not None:
            try:
                name = self._sd.query_devices(self._index)["name"]
            except Exception:
                pass
        return f"声卡输入 {name} @ {self.sample_rate} Hz（sounddevice）"


class WebSocketMic(AudioSource):
    """浏览器当麦克风：网页采音频，WebSocket 推过来。

    这台 WSL2 没有声卡，Windows 上又常常没装 Python / ffmpeg，
    而 Edge / Chrome 是现成的——所以给了一条零安装的路径：
    浏览器打开服务端给的页面，点一下就能说话。

    同一个端口上还兼着 HTTP 服务（页面 / 状态 / 下载），HTTP 线程在构造时
    就起来了，所以端口一绑上页面就能打开，不用等模型加载完。
    """

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8765,
        sample_rate: int = SAMPLE_RATE,
        once: bool = False,
    ):
        from .wsserver import MicWebServer

        page = Path(__file__).with_name("mic_client.html").read_text(encoding="utf-8")
        self._server = MicWebServer(
            host=host, port=port, page=page, sample_rate=sample_rate, once=once
        )
        self.sample_rate = sample_rate

    @property
    def on_text(self):
        """浏览器用文本帧下发控制指令（改文件名等）时的回调。"""
        return self._server.on_text

    @on_text.setter
    def on_text(self, fn):
        self._server.on_text = fn

    @property
    def address(self) -> tuple[str, int]:
        return self._server.address

    def chunks(self, chunk_ms: int = 100) -> Iterator[np.ndarray]:
        yield from self._server.chunks(chunk_ms=chunk_ms)

    def close(self) -> None:
        self._server.close()

    def push_event(self, payload: dict) -> None:
        """把识别结果（半句 / 定稿句）推给浏览器。"""
        self._server.broadcast(payload)

    def set_ready(self, ready: bool = True) -> None:
        self._server.set_ready(ready)

    def set_status_provider(self, fn) -> None:
        self._server.set_status_provider(fn)

    def set_sessions_provider(self, fn) -> None:
        self._server.set_sessions_provider(fn)

    def describe(self) -> str:
        host, port = self.address
        shown = "127.0.0.1" if host in ("0.0.0.0", "") else host
        return f"浏览器麦克风（http://{shown}:{port}/）"


class PulseSource(AudioSource):
    """从 PulseAudio / PipeWire 的采集源录音。

    靠 ``parec``（pulseaudio-utils 自带）取流，输出直接就是 16 kHz 单声道 s16le，
    不需要额外的 Python 库。在 WSLg 上 ``RDPSource`` 能抓到 Windows 会话的音频，
    是没有本机麦克风时最省事的取流方式。
    """

    def __init__(self, device: str = "default", sample_rate: int = SAMPLE_RATE):
        self._cmd = [
            "parec",
            f"--device={device}",
            "--format=s16le",
            f"--rate={sample_rate}",
            "--channels=1",
        ]
        self._device = device
        self.sample_rate = sample_rate
        self._proc = None
        self._name = device

    def chunks(self, chunk_ms: int = 100) -> Iterator[np.ndarray]:
        import shutil
        import subprocess

        if shutil.which("parec") is None:
            raise RuntimeError(
                "找不到 parec。装一下：sudo apt install pulseaudio-utils"
                "（Windows/macOS 用 --source mic 即可）"
            )
        self._proc = subprocess.Popen(
            self._cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
        n = max(1, int(self.sample_rate * chunk_ms / 1000))
        try:
            while True:
                block = self._proc.stdout.read(n * 2)
                if not block or len(block) < n * 2:
                    break
                yield np.frombuffer(block, dtype=np.int16).astype(np.float32) / 32768.0
        finally:
            self.close()

    def close(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=2)
            except Exception:
                self._proc.kill()
        self._proc = None

    def describe(self) -> str:
        return f"PulseAudio 源 {self._device} @ {self.sample_rate} Hz"


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
        # 剩下的零头：半个采样凑不成 int16，丢掉（不然 frombuffer 直接报错）
        if len(buf) % 2:
            print("[stdin] 结尾多出 1 个字节（凑不成整采样），已丢弃", flush=True)
            del buf[-1:]
        if buf:
            yield np.frombuffer(bytes(buf), dtype=np.int16).astype(np.float32) / 32768.0

    def describe(self) -> str:
        return "标准输入（16 kHz s16le PCM）"


def _wav_data_chunk(buf: bytes) -> tuple[int, int, int] | None:
    """解析 WAV 文件头，返回 (数据段起点, 采样率, 声道数)。

    - ``None``：头还没收全，继续攒字节
    - 采样率 ``0``：是 WAV，但格式认不了（不是 16 bit PCM），交给调用方处理
    """
    pos = 12  # RIFF....WAVE
    rate = 0
    channels = 0
    while pos + 8 <= len(buf):
        cid = buf[pos : pos + 4]
        size = struct.unpack("<I", buf[pos + 4 : pos + 8])[0]
        body = pos + 8
        if cid == b"fmt ":
            if body + 16 > len(buf):
                return None
            _, channels, rate, _, _, bits = struct.unpack(
                "<HHIIHH", buf[body : body + 16]
            )
            # 只认 16 bit、1~8 声道，别的当不了（免得畸形文件把内存吃光）
            if bits != 16 or channels <= 0 or channels > 8:
                rate = 0
        elif cid == b"data":
            if rate < 8000 or rate > 192000:
                rate = 0
            return body, rate, channels
        pos = body + size + (size & 1)
    return None


class TcpServer(AudioSource):
    """监听 TCP，接受客户端推送 16 kHz / 单声道 / s16le PCM。

    用于「麦克风在别的机器上、识别在这台机器上」的场景，例如手机 App
    或者另一台装了声卡的电脑，通过 ``scripts/send_mic.py`` 把音频推过来。

    收到的当字节流看，**不假设客户端送来的一定对齐**：多出来的半个采样先
    攒着，凑成整采样再交给识别器，流结束时剩下那半个采样丢掉（不会崩）。

    客户端断开后继续等下一个（``once=True`` 才退出），跟 ws 源一个脾气——
    客户端崩了不用重启服务端重新加载模型。

    客户端直接把 WAV 文件推过来也行（``cat a.wav | nc``）：开头是 RIFF 头
    就跳过，采样率不是 16 kHz 顺手重采样。
    """

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8765,
        sample_rate: int = SAMPLE_RATE,
        once: bool = False,
    ):
        self.sample_rate = sample_rate
        self.once = once
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((host, port))
        self._sock.listen(5)

    def chunks(self, chunk_ms: int = 100) -> Iterator[np.ndarray]:
        n = max(1, int(self.sample_rate * chunk_ms / 1000))
        while True:
            yield from self._session(n)
            if self.once:
                return
            print("[TCP] 客户端断开，继续等下一个（Ctrl+C 结束）", flush=True)

    def _session(self, n: int) -> Iterator[np.ndarray]:
        """服务一个客户端，直到它断开。"""
        print(f"[TCP] 等待客户端连接 {self._sock.getsockname()[:2]} ...", flush=True)
        try:
            conn, addr = self._sock.accept()
        except OSError:
            return
        print(f"[TCP] 已连接 {addr[0]}:{addr[1]}", flush=True)

        buf = bytearray()
        src_rate = self.sample_rate  # 收到的字节按这个采样率解释
        channels = 1
        pending = True  # 开头几个字节还没判断是不是 WAV 头
        try:
            while True:
                try:
                    data = conn.recv(65536)
                except ConnectionResetError:
                    print("[TCP] 客户端被强制断开", flush=True)
                    break
                if not data:
                    break
                buf.extend(data)

                if pending:
                    if len(buf) < 4:
                        continue
                    if buf[:4] != b"RIFF":
                        pending = False
                    elif len(buf) < 12:
                        continue
                    elif buf[8:12] != b"WAVE":
                        pending = False
                    else:
                        info = _wav_data_chunk(bytes(buf))
                        if info is None:
                            if len(buf) < _WAV_HEAD_MAX:
                                continue
                            print("[TCP] WAV 头太大，按裸 PCM 处理", flush=True)
                            pending = False
                        else:
                            offset, rate, ch = info
                            if rate == 0:
                                print(
                                    "[TCP] 只支持 16 bit PCM 的 WAV，这个文件按裸 PCM 处理",
                                    flush=True,
                                )
                                pending = False
                            else:
                                del buf[:offset]
                                src_rate = rate
                                channels = ch
                                pending = False
                                if rate != self.sample_rate:
                                    print(
                                        f"[TCP] 客户端推的是 {rate} Hz，"
                                        f"正在重采样到 {self.sample_rate} Hz",
                                        flush=True,
                                    )

                frames = max(channels, (n // channels) * channels)
                need = frames * channels * 2
                while len(buf) >= need:
                    block = bytes(buf[:need])
                    del buf[:need]
                    yield self._to_samples(block, src_rate, channels)

            # 流结束了，处理剩下的零头
            if pending:
                print("[TCP] 客户端只推来一个残缺的 WAV 头，已丢弃", flush=True)
                buf.clear()
            elif buf:
                tail = len(buf) % (channels * 2)
                if tail:
                    print(
                        f"[TCP] 结尾多出 {tail} 个字节（凑不成整采样），已丢弃",
                        flush=True,
                    )
                    del buf[-tail:]
                if buf:
                    yield self._to_samples(bytes(buf), src_rate, channels)

            # 补一段静音，让 VAD 把当前这句结掉：sherpa-onnx 的 VAD 一旦
            # flush 过就不再认新音频，只能靠静音自然收尾（下一个客户端接新的一句）
            yield np.zeros(int(self.sample_rate * _TAIL_SILENCE), dtype=np.float32)
        finally:
            conn.close()

    def _to_samples(self, block: bytes, src_rate: int, channels: int) -> np.ndarray:
        """一整块原始字节 -> 单声道 float32（必要时降混 + 重采样）。"""
        data = np.frombuffer(block, dtype=np.int16).astype(np.float32) / 32768.0
        if channels > 1:
            data = data.reshape(-1, channels).mean(axis=1)
        if src_rate != self.sample_rate:
            data = resample_linear(data, src_rate, self.sample_rate)
        return data

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass

    def describe(self) -> str:
        return f"TCP 推流 {self._sock.getsockname()[:2]}"
