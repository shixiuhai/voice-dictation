"""vd —— 实时语音听写（voice dictation）核心包。

模块划分：

- ``audio``   音频输入源：ALSA / sounddevice 麦克风 / 浏览器 WS / TCP 推流 /
              PulseAudio / WAV 文件 / 标准输入，以及电平归一化 LevelNormalizer
- ``vad``     Silero VAD 封装，负责把连续音频切成句子
- ``asr``     sherpa-onnx 流式 / 非流式识别封装
- ``final``   单模型 final 模式：定型判定与断句（FinalCommitter，纯算法）
- ``punct``   标点恢复
- ``models``  模型登记表与文件自动识别
- ``session`` 听写记录的落盘（txt / md / srt / json）与终端显示
- ``wsserver`` 手搓的最小 WebSocket + HTTP 服务端（浏览器麦克风用，无第三方依赖）
"""

__all__ = ["audio", "asr", "final", "models", "punct", "session", "vad", "wsserver"]
