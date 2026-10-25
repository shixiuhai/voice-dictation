"""vd —— 实时语音听写（voice dictation）核心包。

模块划分：

- ``audio``   音频输入源：ALSA 麦克风 / WAV 文件 / 标准输入 / TCP 推流
- ``vad``     Silero VAD 封装，负责把连续音频切成句子
- ``asr``     sherpa-onnx 流式 / 非流式识别封装
- ``punct``   标点恢复
- ``models``  模型登记表与文件自动识别
- ``session`` 听写记录的落盘（txt / md / srt / json）
"""

__all__ = ["audio", "asr", "models", "punct", "session", "vad"]
