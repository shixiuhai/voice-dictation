"""标点恢复：给没有标点的识别结果加上逗号句号。"""

from __future__ import annotations


class Punctuator:
    """封装 sherpa-onnx 的 CT-Transformer 标点模型。"""

    def __init__(self, model_path: str, num_threads: int = 1):
        import sherpa_onnx

        config = sherpa_onnx.OfflinePunctuationConfig(
            model=sherpa_onnx.OfflinePunctuationModelConfig(ct_transformer=model_path)
        )
        self._punct = sherpa_onnx.OfflinePunctuation(config)
        self._num_threads = num_threads

    def add(self, text: str) -> str:
        if not text.strip():
            return text
        try:
            return self._punct.add_punctuation(text)
        except Exception:
            return text
