"""Silero VAD 封装：把连续音频切成「一句一句」的语音片段。

sherpa-onnx 的 VAD 有两个状态：

- **当前段**（``current_segment``）：正在说的那一段，从段起点一直到当前缓冲区末尾；
- **已完成段**（``front``）：说完了、静音超过 ``min_silence_duration`` 之后才入队，
  长度比当前段短（尾部静音被裁掉了）。

所以正在喂的音频天然包含已完成段，段结束时**不需要再喂一遍**，
否则同一段音频会被识别两遍（这坑踩过一次）。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class Segment:
    """一段语音。``start`` 是它在音频流里的绝对采样偏移。"""

    start: int
    samples: np.ndarray

    @property
    def end(self) -> int:
        return self.start + len(self.samples)


class SpeechVAD:
    """基于 sherpa-onnx 的 Silero VAD。"""

    def __init__(
        self,
        model_path: str,
        sample_rate: int = 16000,
        threshold: float = 0.35,
        min_silence_duration: float = 0.3,
        min_speech_duration: float = 0.2,
        max_speech_duration: float = 8.0,
        num_threads: int = 1,
    ):
        import sherpa_onnx

        config = sherpa_onnx.VadModelConfig()
        config.silero_vad.model = model_path
        config.silero_vad.threshold = threshold
        config.silero_vad.min_silence_duration = min_silence_duration
        config.silero_vad.min_speech_duration = min_speech_duration
        config.silero_vad.max_speech_duration = max_speech_duration
        config.sample_rate = sample_rate
        config.num_threads = num_threads
        if not config.validate():
            raise ValueError(f"VAD 配置无效: {model_path}")
        self._vad = sherpa_onnx.VoiceActivityDetector(config, buffer_size_in_seconds=100)
        self.sample_rate = sample_rate
        self.window_size = config.silero_vad.window_size
        self._buffer = np.zeros(0, dtype=np.float32)

    def accept(self, samples: np.ndarray) -> None:
        """喂入任意长度的音频，内部按 VAD 窗口大小切片。"""
        self._buffer = np.concatenate([self._buffer, samples])
        while len(self._buffer) >= self.window_size:
            self._vad.accept_waveform(self._buffer[: self.window_size])
            self._buffer = self._buffer[self.window_size :]

    def flush(self) -> None:
        """输入结束时把残留的音频也交给 VAD，让最后一段能入队。"""
        if len(self._buffer):
            self._vad.accept_waveform(self._buffer)
            self._buffer = np.zeros(0, dtype=np.float32)
        self._vad.flush()

    @property
    def is_speech_detected(self) -> bool:
        return self._vad.is_speech_detected()

    @property
    def current(self) -> Segment | None:
        """正在说的那一段；没在说话时返回 None。"""
        if not self._vad.is_speech_detected():
            return None
        seg = self._vad.current_segment
        if seg.start < 0:
            return None
        return Segment(start=int(seg.start), samples=np.asarray(seg.samples, dtype=np.float32))

    @property
    def has_segment(self) -> bool:
        return not self._vad.empty()

    def pop_segment(self) -> Segment:
        """取出一段已经说完的语音并从队列里移除。"""
        seg = self._vad.front
        out = Segment(start=int(seg.start), samples=np.asarray(seg.samples, dtype=np.float32))
        self._vad.pop()
        return out
