#!/usr/bin/env python3
"""实测各流式模型在这台机器上的实时率（RTF）。

RTF = 推理耗时 / 音频时长。RTF < 1 才能边听边出字，0.3 表示比实时快 3 倍。

用法::

    python3 scripts/bench.py                       # 用模型自带的测试音频
    python3 scripts/bench.py --model zh-14m --model bilingual
    python3 scripts/bench.py --wav a.wav --wav b.wav
"""

from __future__ import annotations

import argparse
import sys
import time
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vd import models as model_registry
from vd.asr import StreamingASR


def read_wav(path: str):
    with wave.open(path, "rb") as w:
        rate = w.getframerate()
        channels = w.getnchannels()
        raw = w.readframes(w.getnframes())
    data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    return rate, data


def default_wavs() -> list[str]:
    """挑一组固定的测试音频，保证不同次跑出来的 RTF 能横向比较。

    优先级：双语模型自带的中英混说音频 > 其它模型自带的音频。
    只取 16 kHz、3 秒以上的。
    """
    root = model_registry.model_root()
    prefer = [
        "sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20",
        "sherpa-onnx-streaming-zipformer-small-bilingual-zh-en-2023-02-16",
    ]
    dirs = [root / "asr" / name for name in prefer]
    dirs += [d for d in sorted((root / "asr").glob("*")) if d not in dirs]

    for directory in dirs:
        wavs = []
        for path in sorted((directory / "test_wavs").glob("*.wav")):
            try:
                with wave.open(str(path), "rb") as w:
                    if w.getframerate() != 16000:
                        continue
                    if w.getnframes() / w.getframerate() < 3.0:
                        continue
            except wave.Error:
                continue
            wavs.append(str(path))
        if len(wavs) >= 2:
            return wavs[:4]
    return []


def bench_model(spec, wavs, num_threads: int) -> tuple[float, list[str], float]:
    recognizer = StreamingASR(spec, num_threads=num_threads)

    def run(path: str, measure: bool) -> tuple[float, float, str]:
        rate, data = read_wav(path)
        stream = recognizer.new_stream()
        start = time.monotonic()
        step = int(rate * 0.1)
        for pos in range(0, len(data), step):
            recognizer.feed(stream, data[pos : pos + step], rate)
            recognizer.decode(stream)
        text = recognizer.finish(stream, rate)
        elapsed = time.monotonic() - start
        return elapsed, len(data) / rate, text

    run(wavs[0], False)  # 预热：第一次推理包含图初始化，单独算会偏低
    total_audio = 0.0
    total_elapsed = 0.0
    texts = []
    for path in wavs:
        elapsed, audio, text = run(path, True)
        total_audio += audio
        total_elapsed += elapsed
        texts.append(text)
    rtf = total_elapsed / total_audio if total_audio else 0.0
    return rtf, texts, total_audio


def main() -> int:
    p = argparse.ArgumentParser(description="实测流式模型的实时率")
    p.add_argument("--model", action="append", default=[], help="要测的模型名，可重复")
    p.add_argument("--wav", action="append", default=[], help="测试音频，可重复")
    p.add_argument("--threads", type=int, default=2)
    args = p.parse_args()

    wavs = args.wav or default_wavs()
    if not wavs:
        print("没有可用的测试音频")
        return 1

    specs = model_registry.list_models()
    if args.model:
        wanted = set(args.model)
        specs = [s for s in specs if s.name in wanted]

    print(f"测试音频 {len(wavs)} 段，线程数 {args.threads}\n")
    print(f"{'模型':<18}{'类型':<20}{'大小':>9}{'RTF':>8}{'倍速':>7}   识别结果（第一段）")
    print("-" * 116)
    for spec in specs:
        if spec.kind == "paraformer_offline":
            continue  # 非流式模型不参与实时率对比
        rtf, texts, audio = bench_model(spec, wavs, args.threads)
        sample = texts[0][:38] if texts else ""
        speed = f"{1 / rtf:.1f}x" if rtf else "-"
        print(
            f"{spec.name:<18}{spec.kind:<20}{spec.size_mb:>7.0f}MB{rtf:>8.3f}{speed:>7}   {sample}"
        )
    print("-" * 116)
    print("RTF = 推理耗时 / 音频时长，越小越快；< 1 才能边听边出字")
    return 0


if __name__ == "__main__":
    sys.exit(main())
