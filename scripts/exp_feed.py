#!/usr/bin/env python3
"""实验：流式模型该吃「VAD 切好的段」还是「连续音频流」。

对照两种喂法，对**同一段**的语音，比较谁的输出更接近离线模型的基准：

- **cold（现状）**：VAD 切段，每段喂给一个新建的 stream —— 识别器没有左上下文。
- **cont（提案）**：整条音频连续喂给同一条 stream，VAD 只在旁边标边界。
  每段的文本 = 连续假设在「段起点」到「段终点 + flush」之间新增的部分。

另外测 **pre**：每段新 stream，但先喂 0.3 / 0.6 / 1.0 秒真实音频预热。

指标：
- 空段率：输出为空的段占比（现状最致命的失败模式）
- 召回：离线基准的字符能在流式输出里按顺序对上的比例

用法::

    python3 scripts/exp_feed.py
    python3 scripts/exp_feed.py --file samples/0.wav
"""

from __future__ import annotations

import argparse
import difflib
import sys
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vd import models as model_registry
from vd.asr import OfflineASR, StreamingASR
from vd.vad import SpeechVAD

SR = 16000
FLUSH = 0.5  # 段结束后等多久算「这段的字已经吐完」


def read_wav(path: str) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        rate = w.getframerate()
        channels = w.getnchannels()
        raw = w.readframes(w.getnframes())
    data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    if rate != SR:
        n = int(len(data) * SR / rate)
        data = np.interp(np.linspace(0, len(data) - 1, n), np.arange(len(data)), data).astype(
            np.float32
        )
    return data


def recall(ref: str, hyp: str) -> float:
    """ref 的字符里有多少能在 hyp 里按顺序对上。"""
    ref = "".join(ref.split())
    hyp = "".join(hyp.split())
    if not ref:
        return 1.0
    sm = difflib.SequenceMatcher(None, ref, hyp)
    return sum(b.size for b in sm.get_matching_blocks()) / len(ref)


def added_text(hyp_end: str, hyp_start: str) -> str:
    """连续假设里 hyp_start 之后新增的部分。"""
    if not hyp_start:
        return hyp_end
    if hyp_end.startswith(hyp_start):
        return hyp_end[len(hyp_start) :]
    n = difflib.SequenceMatcher(None, hyp_start, hyp_end).find_longest_match(
        0, len(hyp_start), 0, len(hyp_end)
    ).size
    return hyp_end[n:]


def segments_of(data: np.ndarray) -> list[tuple[int, int, np.ndarray]]:
    vad = SpeechVAD(
        str(model_registry.vad_model_path()),
        sample_rate=SR,
        threshold=0.35,
        min_silence_duration=0.3,
        max_speech_duration=8.0,
    )
    vad.accept(data)
    vad.flush()
    segs = []
    while vad.has_segment:
        s = vad.pop_segment()
        segs.append((s.start, s.end, s.samples))
    return segs


def run_file(path: Path, asr: StreamingASR, off: OfflineASR, prers: list[float], verbose: bool):
    data = read_wav(str(path))
    segs = segments_of(data)
    if not segs:
        return None
    refs = [off.transcribe(sm, SR).strip() for _, _, sm in segs]

    variants: dict[str, list[str]] = {"cold": [], "cont": []}
    for p in prers:
        variants[f"pre{p}"] = []

    for i, (a, _, sm) in enumerate(segs):
        st = asr.new_stream()
        asr.feed(st, sm, SR)
        variants["cold"].append(asr.finish(st, SR).strip())
        for p in prers:
            pre = data[max(0, a - int(p * SR)) : a]
            st = asr.new_stream()
            if len(pre):
                asr.feed(st, pre, SR)
            asr.feed(st, sm, SR)
            variants[f"pre{p}"].append(asr.finish(st, SR).strip())

    # 连续流：记下每个时刻的假设，按「段起点 → 段终点+flush」取新增文本
    st = asr.new_stream()
    snaps: list[tuple[float, str]] = []
    step = int(SR * 0.1)
    pos = 0
    while pos < len(data):
        asr.feed(st, data[pos : pos + step], SR)
        pos += step
        snaps.append((pos / SR, asr.decode(st)))
    snaps.append((pos / SR, asr.finish(st, SR)))

    def hyp_at(t: float) -> str:
        best = ""
        for ts, h in snaps:
            if ts <= t:
                best = h
            else:
                break
        return best

    for a, b, _ in segs:
        variants["cont"].append(added_text(hyp_at(b / SR + FLUSH), hyp_at(a / SR)).strip())

    if verbose:
        print(f"\n=== {path}（{len(segs)} 段）===")
        for i, ref in enumerate(refs):
            print(f"  段{i} 离线基准 : {ref!r}")
            for name in ["cold", "cont"] + [f"pre{p}" for p in prers]:
                print(f"      {name:8s}: {variants[name][i]!r}")

    out = {"n": len(segs)}
    for name, texts in variants.items():
        out[name] = {
            "empty": sum(1 for t in texts if not t.strip()) / max(1, len(texts)),
            "recall": sum(recall(r, t) for r, t in zip(refs, texts)) / max(1, len(refs)),
            "texts": texts,
        }
    # 整段拼接（最终落盘的东西）
    ref_total = "".join(refs)
    out["_total"] = {
        "ref": ref_total,
        "cold": "".join(variants["cold"]),
        "cont": "".join(variants["cont"]),
    }
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--threads", type=int, default=2)
    ap.add_argument("--model", default="small-bilingual")
    ap.add_argument("--file", default="", help="只测这一个文件")
    ap.add_argument("--quiet", action="store_true", help="不逐段打印")
    args = ap.parse_args()

    asr = StreamingASR(model_registry.resolve(args.model), num_threads=args.threads)
    off = OfflineASR(model_registry.resolve("paraformer-zh"), num_threads=args.threads)
    prers = [0.3, 0.6, 1.0]

    if args.file:
        files = [Path(args.file)]
    else:
        files = [Path("samples/0.wav"), Path("samples/1.wav")]
        tdir = (
            model_registry.model_root()
            / "asr"
            / "sherpa-onnx-streaming-zipformer-small-bilingual-zh-en-2023-02-16"
            / "test_wavs"
        )
        files += sorted(p for p in tdir.glob("*.wav"))

    agg: dict[str, dict] = {}
    tot: dict[str, list] = {"ref": [], "cold": [], "cont": []}
    for f in files:
        if not f.is_file():
            continue
        r = run_file(f, asr, off, prers, verbose=not args.quiet)
        if not r:
            continue
        for name in r:
            if name in ("n", "_total"):
                continue
            a = agg.setdefault(name, {"empty": 0.0, "recall": 0.0, "n": 0})
            a["empty"] += r[name]["empty"]
            a["recall"] += r[name]["recall"]
            a["n"] += 1
        for k in tot:
            tot[k].append(r["_total"][k])

    print("\n=== 汇总（各文件平均）===")
    print(f"{'方案':10s} {'空段率':>8s} {'离线基准召回':>14s}")
    for name in ["cold", "cont"] + [f"pre{p}" for p in prers]:
        a = agg.get(name)
        if not a:
            continue
        print(f"{name:10s} {a['empty']/a['n']:8.2%} {a['recall']/a['n']:14.2%}")

    ref_all = "".join(tot["ref"])
    print("\n=== 整段拼接 vs 离线基准（最终落盘文本）===")
    for name in ("cold", "cont"):
        hyp_all = "".join(tot[name])
        print(f"  {name:6s} 召回 {recall(ref_all, hyp_all):.2%}   长度 {len(hyp_all)}/{len(ref_all)}")
    print(f"  离线基准: {ref_all!r}")
    print(f"  cold    : {''.join(tot['cold'])!r}")
    print(f"  cont    : {''.join(tot['cont'])!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
