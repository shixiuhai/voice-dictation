"""sherpa-onnx 识别封装：流式（实时）和非流式（整句重识别）两种。"""

from __future__ import annotations

import re

import numpy as np

from .models import ModelSpec


class StreamingASR:
    """流式识别器，用于实时出字。

    一个 stream 对应一句：喂音频 -> ``decode`` 取当前假设 -> 句末 ``finish``
    收尾，然后换一个新 stream。
    """

    def __init__(
        self,
        spec: ModelSpec,
        num_threads: int = 2,
        decoding_method: str = "greedy_search",
        hotwords: str = "",
        enable_endpoint: bool = False,
    ):
        import sherpa_onnx

        self.spec = spec
        self._recognizer = self._build(sherpa_onnx, num_threads, decoding_method, hotwords, enable_endpoint)

    def _build(self, sherpa_onnx, num_threads, decoding_method, hotwords, enable_endpoint):
        spec = self.spec
        common = dict(
            tokens=spec.tokens,
            num_threads=num_threads,
            decoding_method=decoding_method,
            enable_endpoint_detection=enable_endpoint,
        )
        if spec.kind == "transducer":
            return sherpa_onnx.OnlineRecognizer.from_transducer(
                encoder=spec.encoder,
                decoder=spec.decoder,
                joiner=spec.joiner,
                hotwords_file=hotwords,
                modeling_unit=spec.modeling_unit,
                **common,
            )
        if spec.kind == "ctc":
            return sherpa_onnx.OnlineRecognizer.from_zipformer2_ctc(model=spec.model, **common)
        if spec.kind == "paraformer":
            return sherpa_onnx.OnlineRecognizer.from_paraformer(
                encoder=spec.encoder, decoder=spec.decoder, **common
            )
        raise ValueError(f"不支持的模型类型: {spec.kind}")

    def new_stream(self):
        return self._recognizer.create_stream()

    def feed(self, stream, samples: np.ndarray, sample_rate: int) -> None:
        if len(samples):
            stream.accept_waveform(sample_rate=sample_rate, waveform=samples)

    def decode(self, stream) -> str:
        """推进解码并返回当前假设（可能是半句）。"""
        while self._recognizer.is_ready(stream):
            self._recognizer.decode_stream(stream)
        return self._recognizer.get_result(stream)

    def is_ready(self, stream) -> bool:
        return self._recognizer.is_ready(stream)

    def finish(self, stream, sample_rate: int, tail_seconds: float = 0.3) -> str:
        """句末收尾：补一段静音再抽干解码队列，返回整句结果。"""
        tail = np.zeros(int(tail_seconds * sample_rate), dtype=np.float32)
        self.feed(stream, tail, sample_rate)
        stream.input_finished()
        while self._recognizer.is_ready(stream):
            self._recognizer.decode_stream(stream)
        return self._recognizer.get_result(stream)

    def is_endpoint(self, stream) -> bool:
        return self._recognizer.is_endpoint(stream)


class OfflineASR:
    """非流式识别器，用于把一句已经说完的话重新识别一遍。

    流式模型为了赶实时会牺牲一点准确率，句子结束后用非流式模型重跑一遍
    通常更准（尤其是中文），代价是这一句要多花几十到几百毫秒。

    ``sense_voice`` 是 SenseVoice 多语种模型（中英日韩粤）。它的卖点是
    **抗背景噪声/音乐**——有实测称嘈杂环境下掉点明显小于 zipformer 系，
    所以「电视/会议/远场」这类场景值得优先拿它做重识别。
    """

    def __init__(self, spec: ModelSpec, num_threads: int = 2, language: str = "auto"):
        import sherpa_onnx

        self.spec = spec
        if spec.kind in ("paraformer", "paraformer_offline"):
            self._recognizer = sherpa_onnx.OfflineRecognizer.from_paraformer(
                paraformer=spec.model, tokens=spec.tokens, num_threads=num_threads
            )
        elif spec.kind == "sense_voice":
            self._recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                model=spec.model,
                tokens=spec.tokens,
                num_threads=num_threads,
                language="" if language in ("", "auto") else language,
                use_itn=True,
            )
        elif spec.kind == "transducer":
            self._recognizer = sherpa_onnx.OfflineRecognizer.from_transducer(
                encoder=spec.encoder,
                decoder=spec.decoder,
                joiner=spec.joiner,
                tokens=spec.tokens,
                num_threads=num_threads,
                decoding_method="greedy_search",
            )
        else:
            raise ValueError(f"不支持做整句重识别的模型类型: {spec.kind}")

    def transcribe(self, samples: np.ndarray, sample_rate: int) -> str:
        if len(samples) == 0:
            return ""
        stream = self._recognizer.create_stream()
        stream.accept_waveform(sample_rate=sample_rate, waveform=samples)
        self._recognizer.decode_stream(stream)
        text = stream.result.text.strip()
        if self.spec.kind == "sense_voice":
            # SenseVoice 会在文本前带上 <|zh|><|NEUTRAL|><|Speech|> 这类标记
            text = _strip_sense_voice_tags(text)
        return text


_SV_TAG = re.compile(r"<\|[^|]*\|>")


def _strip_sense_voice_tags(text: str) -> str:
    """去掉 SenseVoice 输出里的 <|zh|><|NEUTRAL|><|Speech|><|withitn|> 标记。"""
    return _SV_TAG.sub("", text).strip()
