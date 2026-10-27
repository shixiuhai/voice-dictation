#!/usr/bin/env python3
"""实时语音听写记录程序。

麦克风（或任意 PCM 流）进来的音频 -> VAD 切句 -> 流式识别实时出字 ->
句子落盘成 txt / md / srt / json。

用法示例::

    python3 dictation.py                          # 麦克风 + 默认模型
    python3 dictation.py --model zh-14m           # 换一个更轻的模型
    python3 dictation.py --file a.wav --realtime  # 用文件按实时节奏跑一遍
    python3 dictation.py --source tcp --port 8765 # 接收别的机器推来的音频
    python3 dictation.py --list-devices           # 看看有哪些录音设备
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from vd import models as model_registry
from vd.asr import OfflineASR, StreamingASR
from vd.audio import (
    SAMPLE_RATE,
    AlsaMic,
    LevelNormalizer,
    PulseSource,
    RawStdin,
    SoundDeviceMic,
    TcpServer,
    WavFile,
    WebSocketMic,
    list_capture_devices,
    list_pulse_sources,
    list_sounddevice_inputs,
)
from vd.punct import Punctuator
from vd.final import FinalCommitter, Sentence, normalize_cjk_spacing
from vd.session import Terminal, TranscriptWriter, list_sessions
from vd.vad import SpeechVAD

# 断句落在标点前面时，新句开头会粘上标点，把它挪回上一句
_LEADING_PUNCT = re.compile(r"^[，。！？；：、,.!?;:]+")

DEFAULT_OUT_DIR = Path(__file__).resolve().parent / "transcripts"


class NoCaptureDevice(RuntimeError):
    """系统里没有可用的录音设备。"""


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="实时语音听写记录（sherpa-onnx 流式 ASR + Silero VAD）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--model", default=model_registry.DEFAULT_MODEL, help="流式识别模型名或模型目录")
    p.add_argument(
        "--refine-model",
        default=model_registry.DEFAULT_REFINE_MODEL,
        help="整句重识别用的非流式模型名（留空关闭）",
    )
    p.add_argument(
        "--source",
        choices=["mic", "file", "stdin", "tcp", "pulse", "ws"],
        default="mic",
        help="音频来源：mic 麦克风 / file 文件 / stdin 标准输入 / tcp 网络推流 / pulse PulseAudio 采集源 / ws 浏览器麦克风",
    )
    p.add_argument("--file", default="", help="--source file 时的音频文件")
    p.add_argument(
        "--device",
        default="",
        help="录音设备名：mic 用 ALSA 设备名（default），pulse 用 PulseAudio 采集源名（default / RDPSource）",
    )
    p.add_argument("--host", default="0.0.0.0", help="--source tcp 时的监听地址")
    p.add_argument("--port", type=int, default=8765, help="--source tcp 时的监听端口")
    p.add_argument("--realtime", action="store_true", help="读文件时按 1 倍速喂数据")
    p.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR), help="听写记录输出目录")
    p.add_argument(
        "--out-name",
        default="",
        help="记录文件名前缀，最终文件名是 <前缀>-<开始时间>.md（默认「听写」）",
    )
    p.add_argument(
        "--append",
        default="",
        metavar="STEM",
        help="追加到已有的记录（文件名主干，如 会议纪要-20261025-142211）",
    )
    p.add_argument(
        "--list-sessions",
        action="store_true",
        help="列出输出目录里已有的记录后退出",
    )
    p.add_argument(
        "--formats",
        default="txt,md,srt,json",
        help="输出文件格式，逗号分隔（txt/md/srt/json）",
    )
    p.add_argument("--no-punct", action="store_true", help="不恢复标点")
    p.add_argument("--no-vad", action="store_true", help="不用 VAD，靠识别器的端点检测切句")
    p.add_argument(
        "--commit",
        choices=["vad", "final"],
        default="vad",
        help="切句方式：vad 先用 VAD 切段再识别（默认）；final 一个模型搞定——"
             "流式识别一直吃连续音频，用 token 时间戳判断「这段已经定型」就落盘",
    )
    p.add_argument(
        "--final-margin",
        type=float,
        default=1.0,
        help="final 模式：token 的时间戳比当前音频位置早多少秒就算定型（秒）",
    )
    p.add_argument(
        "--final-pause",
        type=float,
        default=1.2,
        help="final 模式：说话停顿超过多少秒就断句（模型不带标点时靠这个）",
    )
    p.add_argument(
        "--final-max-sentence",
        type=float,
        default=30.0,
        help="final 模式：单句最长多少秒就强制断句",
    )
    p.add_argument(
        "--agc",
        dest="agc",
        action="store_true",
        default=True,
        help="电平归一化：输入偏轻时自动补增益（远场/电视声必需，默认开）",
    )
    p.add_argument("--no-agc", dest="agc", action="store_false", default=argparse.SUPPRESS,
                   help="关掉电平归一化")
    p.add_argument(
        "--agc-max-gain",
        type=float,
        default=64.0,
        help="电平归一化最多补多少倍增益（64 倍 = +36 dB，够救很轻的音源）",
    )
    p.add_argument(
        "--vad-threshold",
        type=float,
        default=0.35,
        help="VAD 语音判定阈值：调低更容易切出句子（转播/音乐底噪多的场合有用）",
    )
    p.add_argument(
        "--vad-silence",
        type=float,
        default=0.3,
        help="多长的静音算一句说完（秒）",
    )
    p.add_argument(
        "--vad-max-speech",
        type=float,
        default=8.0,
        help="单句最长多少秒就强制切一刀。电视/直播这种连续有声、句间没有静音的"
             "音频，VAD 切不出句，只能靠这个兜底；默认 8 秒（30 秒的话半分钟才出一句）",
    )
    p.add_argument("--hotwords", default="", help="热词文件，每行一个词/短语")
    p.add_argument("--threads", type=int, default=2, help="推理线程数")
    p.add_argument("--chunk-ms", type=int, default=100, help="每次读取的音频长度（毫秒）")
    p.add_argument("--vad-model", default="", help="VAD 模型路径")
    p.add_argument("--punct-model", default="", help="标点模型路径")
    p.add_argument("--models-dir", default="", help="模型根目录")
    p.add_argument(
        "--once",
        action="store_true",
        help="只服务一个客户端：客户端断开就结束本次听写并退出（ws / tcp 源默认继续等下一个）",
    )
    p.add_argument("--list-devices", action="store_true", help="列出录音设备后退出")
    p.add_argument("--list-models", action="store_true", help="列出已下载模型后退出")
    return p.parse_args(argv)


def list_devices(term: Terminal) -> None:
    """列出系统里能用的采集设备。"""
    term.info("== ALSA 录音设备 ==")
    devices = list_capture_devices()
    if devices:
        for line in devices:
            term.info("  " + line)
    elif Path("/proc/asound/pcm").is_file():
        term.info("  没有 capture 设备")
    else:
        term.info("  /proc/asound/pcm 不存在：内核里没有 ALSA 音频驱动")
        term.info("  常见于 WSL2 / 容器 / 云主机，这种环境拿不到本机麦克风")

    term.info("")
    term.info("== PulseAudio 采集源 ==")
    pulse = list_pulse_sources()
    if pulse:
        for line in pulse:
            term.info("  " + line)
    else:
        term.info("  没有可用的 PulseAudio 源（pactl 不可用或没有 Pulse 服务）")

    term.info("")
    term.info("== sounddevice 输入设备 ==")
    sd = list_sounddevice_inputs()
    if sd:
        for line in sd:
            term.info("  " + line)
    else:
        term.info("  没有（pip install sounddevice 可启用跨平台麦克风后端）")

    term.info("")
    term.info("== 改用其它音频来源 ==")
    term.info("  --source file  <wav>   听写一个音频文件")
    term.info("  --source stdin          从标准输入读 16k s16le PCM")
    term.info("  --source pulse          抓 PulseAudio 采集源（WSLg 上可用 RDPSource 抓 Windows 会话音频）")
    term.info("  --source tcp            监听 TCP，让另一台机器把麦克风推过来")


def list_models(term: Terminal) -> None:
    term.info("== 已下载模型 ==")
    specs = model_registry.list_models()
    if not specs:
        term.info(f"  （空）模型目录：{model_registry.model_root()}")
        term.info("  先跑 scripts/download_models.sh")
        return
    for spec in specs:
        term.info("  " + spec.summary())


def build_source(args):
    if args.source == "mic":
        if list_capture_devices():
            return AlsaMic(args.device or "default")
        # 没有 ALSA 采集设备：退回跨平台后端（Windows / macOS / 无 ALSA 的 Linux）
        try:
            return SoundDeviceMic(args.device)
        except ImportError:
            raise NoCaptureDevice()
    if args.source == "file":
        if not args.file:
            raise SystemExit("--source file 需要同时给 --file")
        return WavFile(args.file, realtime=args.realtime)
    if args.source == "stdin":
        return RawStdin()
    if args.source == "pulse":
        return PulseSource(args.device or "default")
    if args.source == "ws":
        return WebSocketMic(args.host, args.port, once=args.once)
    return TcpServer(args.host, args.port, once=args.once)


class ControlMixin:
    """浏览器发来的控制指令（改名 / 追加到已有记录 / 列记录）。

    两个引擎都要用，抽出来放前面。
    """

    def handle_control(self, text: str) -> str:
        """处理浏览器发来的控制指令，返回要回给浏览器的字符串（没有就返回空）。

        - ``{"cmd": "name", "name": "会议"}`` —— 改记录文件名
        - ``{"cmd": "append", "stem": "会议-20261025-142211"}`` —— 追加到已有记录
        - ``{"cmd": "list"}`` —— 列已有记录

        名字和文件名主干都来自浏览器，是外部输入：
        ``sanitize_name`` / ``sanitize_stem`` 负责消毒，这里只做调度。
        """
        import json

        try:
            msg = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return ""
        if not isinstance(msg, dict):
            return ""

        cmd = msg.get("cmd")
        if cmd == "name":
            stem = self.writer.set_name(str(msg.get("name", ""))[:120])
            path = self.writer.out_dir / (stem + ".md")
            self.term.info(f"记录文件: {path}")
            return json.dumps({"cmd": "name_ok", "stem": stem}, ensure_ascii=False)

        if cmd == "append":
            try:
                offset = self.writer.attach(str(msg.get("stem", "")))
            except (ValueError, FileNotFoundError) as e:
                return json.dumps({"cmd": "error", "message": str(e)}, ensure_ascii=False)
            self._offset = offset
            path = self.writer.out_dir / (self.writer.stem + ".md")
            self.term.info(f"追加到: {path}（时间轴从 {offset:.1f}s 接着走）")
            return json.dumps(
                {"cmd": "append_ok", "stem": self.writer.stem, "offset": offset},
                ensure_ascii=False,
            )

        if cmd == "list":
            return json.dumps(
                {
                    "cmd": "sessions",
                    "items": list_sessions(self.writer.out_dir, exclude=self.writer.stem),
                },
                ensure_ascii=False,
            )
        return ""


class DictationEngine(ControlMixin):
    """把音频流转成一句一句的识别结果。"""

    def __init__(
        self,
        source,
        asr,
        refine,
        vad,
        writer,
        term,
        punct=None,
        chunk_ms: int = 100,
        normalizer=None,
    ):
        self.source = source
        self.asr = asr
        self.refine = refine
        self.vad = vad
        self.writer = writer
        self.term = term
        self.punct = punct
        self.sample_rate = source.sample_rate
        # 电平归一化只作用在「喂给 VAD 的那一路」，识别拿到的是补过增益的音频。
        # 它保证等长，所以下面的采样计数 / 时间轴都不用改。
        self.norm = normalizer
        self._stream = asr.new_stream()
        self._fed = 0  # 当前这句已经喂给识别器的采样数
        self._total = 0  # 已读采样总数
        self._cur_start = None  # 当前 VAD 段的起始采样偏移
        self._seg_start = 0.0  # 不用 VAD 时，这句的开始时间
        self._chunk_ms = chunk_ms
        self._offset = 0.0  # 追加到旧记录时，时间轴要往后平移这么多
        self._last_partial = ""  # 上次推给前端的半句，避免重复刷

    def _chunk(self, chunk: np.ndarray) -> np.ndarray:
        """原始分片 -> 归一化后的分片。"""
        if self.norm is not None:
            return self.norm.process(chunk)
        return chunk

    def _push(self, payload: dict) -> None:
        """把一条事件推给前端（没有前端时是空操作）。"""
        try:
            self.source.push_event(payload)
        except Exception:
            pass

    def _push_partial(self, text: str) -> None:
        if text == self._last_partial:
            return
        self._last_partial = text
        self._push({"cmd": "partial", "text": text})

    def _emit(self, start: float, end: float, samples, text: str) -> None:
        refined = False
        if self.refine is not None and samples is not None and len(samples):
            better = self.refine.transcribe(samples, self.sample_rate)
            if better.strip():
                text = better
                refined = True
        if self.punct is not None:
            text = self.punct.add(text)
        text = text.strip()
        if not text:
            self._push_partial("")
            return
        self.writer.add(start, end, text, refined=refined)
        self.term.segment(start, end, text, refined=refined)
        self._last_partial = ""
        self._push(
            {
                "cmd": "segment",
                "start": start,
                "end": end,
                "text": text,
                "refined": refined,
                "count": len(self.writer.segments),
            }
        )

    def handle_control(self, text: str) -> str:
        """见 ``ControlMixin.handle_control``。"""
        return super().handle_control(text)

    def run(self) -> None:
        for chunk in self.source.chunks(chunk_ms=self._chunk_ms):
            chunk = self._chunk(chunk)
            if not len(chunk):
                continue
            self._total += len(chunk)
            if self.vad is None:
                self._step_endpoint(chunk)
            else:
                self._step_vad(chunk)
        self.finalize()

    def _step_endpoint(self, chunk: np.ndarray) -> None:
        """不用 VAD：靠识别器自己的端点检测切句。"""
        self.asr.feed(self._stream, chunk, self.sample_rate)
        text = self.asr.decode(self._stream)
        if text:
            self.term.partial(text)
            self._push_partial(text)
        if self.asr.is_endpoint(self._stream):
            text = self.asr.finish(self._stream, self.sample_rate)
            self._emit(self._seg_start + self._offset,
                      self._total / self.sample_rate + self._offset, None, text)
            self._stream = self.asr.new_stream()
            self._seg_start = self._total / self.sample_rate

    def _step_vad(self, chunk: np.ndarray) -> None:
        self.vad.accept(chunk)
        cur = self.vad.current
        if cur is None:
            self._close_segments()
            return

        if self._cur_start is None or cur.start != self._cur_start:
            # 换段了：先把还排在 VAD 队列里的旧段收尾，再开新段
            self._close_segments()
            self._cur_start = cur.start
            self._fed = 0
        new = cur.samples[self._fed :]
        if len(new):
            self.asr.feed(self._stream, new, self.sample_rate)
            self._fed += len(new)
        text = self.asr.decode(self._stream)
        if text:
            self.term.partial(text)
            self._push_partial(text)

    def _close_segments(self, fallback=None) -> None:
        """把说完的段收尾：出文本、落盘、换新流。

        已经喂进识别流的音频包含了 VAD 完成的片段（完成的片段只是把尾部静音
        裁掉了），所以这里不再重复喂音频。
        """
        segs = []
        while self.vad.has_segment:
            segs.append(self.vad.pop_segment())

        if self._cur_start is not None:
            seg = segs.pop(0) if segs else fallback
            text = self.asr.finish(self._stream, self.sample_rate)
            if seg is not None:
                self._emit(
                    seg.start / self.sample_rate + self._offset,
                    seg.end / self.sample_rate + self._offset,
                    seg.samples,
                    text,
                )
            self._stream = self.asr.new_stream()
            self._fed = 0
            self._cur_start = None

        # 队列里剩下的段（极少见）各自单独识别
        for seg in segs:
            stream = self.asr.new_stream()
            self.asr.feed(stream, seg.samples, self.sample_rate)
            text = self.asr.finish(stream, self.sample_rate)
            self._emit(
                seg.start / self.sample_rate + self._offset,
                seg.end / self.sample_rate + self._offset,
                seg.samples,
                text,
            )

    def finalize(self) -> None:
        """输入结束时把最后一句收尾。

        先补 100 ms 静音：输入突然断掉时，识别器还欠着一点音频没吐字，
        VAD 也要靠这点静音才肯把最后一段入队。
        """
        tail = self._chunk(np.zeros(int(self.sample_rate * 0.1), dtype=np.float32))
        if len(tail):
            self._total += len(tail)
            if self.vad is None:
                self._step_endpoint(tail)
            else:
                self._step_vad(tail)
        if self.vad is not None:
            cur = self.vad.current
            self.vad.flush()
            self._close_segments(fallback=cur)
        else:
            text = self.asr.finish(self._stream, self.sample_rate)
            self._emit(self._seg_start + self._offset,
                      self._total / self.sample_rate + self._offset, None, text)


class FinalEngine(ControlMixin):
    """单模型听写：一条流式识别流到底，自己判断定型并落盘。

    和 ``DictationEngine`` 的区别：

    - 音频**不切段**，识别器一直吃连续流，每句都有左上下文；
    - 不需要 VAD，也不需要额外的离线模型：识别器自己给出每个 token 的
      时间戳，够老的就是定型了的（见 ``vd/final.py``）；
    - 提交过的文字不再修改（final 的意思），实时预览显示的是当前这句。

    前提是识别模型自带标点（``--model x-asr``）。模型不带标点时，
    断句就退化成「按说话停顿断」，可以再挂一个标点模型兜底。
    """

    def __init__(
        self,
        source,
        asr,
        writer,
        term,
        punct=None,
        chunk_ms: int = 100,
        normalizer=None,
        margin: float = 1.0,
        pause: float = 1.2,
        max_sentence: float = 30.0,
    ):
        self.source = source
        self.writer = writer
        self.term = term
        self.punct = punct
        self.norm = normalizer
        self.sample_rate = source.sample_rate
        self._chunk_ms = chunk_ms
        self._offset = 0.0
        self._last_partial = ""
        self._held: Sentence | None = None  # 暂存一句，等下一句来了再落盘
        self._asr = asr
        self._commit_kwargs = dict(
            sample_rate=self.sample_rate,
            margin=margin,
            pause=pause,
            max_sentence=max_sentence,
        )
        self._commit = FinalCommitter(asr, **self._commit_kwargs)
        # ws / tcp 断开后还能接下一个客户端：换人时必须把这条复用的识别流扔掉，
        # 否则上一条连接的残留假设会当成新连接的输出吐出来（串台）。
        self._session = getattr(source, "session", 0)
        # 换会话收尾时把上一条连接最后几句落盘，但别再推给新客户端
        self._mute_push = False

    def _chunk(self, chunk: np.ndarray) -> np.ndarray:
        if self.norm is not None:
            return self.norm.process(chunk)
        return chunk

    def _push(self, payload: dict) -> None:
        try:
            self.source.push_event(payload)
        except Exception:
            pass

    def _push_partial(self, text: str) -> None:
        if text == self._last_partial:
            return
        self._last_partial = text
        self._push({"cmd": "partial", "text": text})

    def _emit(self, sentence) -> None:
        """收下一句定稿的话。

        断句常常落在标点前面（模型比音频慢 1~2 秒，句末的逗号会晚一步吐出来），
        所以这里**晚一句再落盘**：新句开头粘着的标点挪回上一句，
        上一句才真正写进文件。这样 txt / md / srt / json 的内容始终一致。
        """
        text = normalize_cjk_spacing(sentence.text.strip())
        m = _LEADING_PUNCT.match(text)
        if m:
            if self._held is not None:
                self._held.text = (self._held.text + m.group(0)).strip()
                self._held.end = max(self._held.end, sentence.start)
            text = text[m.end() :].strip()
            if not text:
                return
        if not text:
            return
        if self._held is not None:
            self._write(self._held)
        self._held = Sentence(text, sentence.start, sentence.end)

    def _write(self, sentence: Sentence) -> None:
        text = sentence.text
        if self.punct is not None:
            text = self.punct.add(text).strip()
            if not text:
                return
        start = sentence.start + self._offset
        end = sentence.end + self._offset
        self.writer.add(start, end, text, refined=False)
        self.term.segment(start, end, text, refined=False)
        self._last_partial = ""
        if self._mute_push:
            return
        self._push(
            {
                "cmd": "segment",
                "start": start,
                "end": end,
                "text": text,
                "refined": False,
                "count": len(self.writer.segments),
            }
        )

    def run(self) -> None:
        for chunk in self.source.chunks(chunk_ms=self._chunk_ms):
            # 换客户端了：先把上一条连接的最后一句收尾落盘，再重开一条识别流
            cur = getattr(self.source, "session", self._session)
            if cur != self._session:
                self._session = cur
                self._reset()
            chunk = self._chunk(chunk)
            if not len(chunk):
                continue
            pending, sentences = self._commit.feed(chunk)
            if pending:
                pending = normalize_cjk_spacing(pending)
                self.term.partial(pending)
                self._push_partial(pending)
            for sentence in sentences:
                self._emit(sentence)
        self.finalize()

    def _reset(self) -> None:
        """换客户端：收尾上一条连接，丢掉跨连接复用的识别流。"""
        # 上一条连接的最后几句仍然落盘，但不要再推给刚连上的新客户端
        self._mute_push = True
        try:
            for sentence in self._commit.finalize():
                self._emit(sentence)
            if self._held is not None:
                self._write(self._held)
                self._held = None
        finally:
            self._mute_push = False
        self._commit = FinalCommitter(self._asr, **self._commit_kwargs)

    def finalize(self) -> None:
        for sentence in self._commit.finalize():
            self._emit(sentence)
        if self._held is not None:
            self._write(self._held)
            self._held = None


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.file and args.source == "mic":
        args.source = "file"  # 给了文件就直接听写文件
    if args.models_dir:
        os.environ["VD_MODEL_DIR"] = str(Path(args.models_dir).expanduser())

    term = Terminal(color=sys.stdout.isatty(), live=sys.stdout.isatty())
    if args.list_devices:
        list_devices(term)
        return 0
    if args.list_models:
        list_models(term)
        return 0
    if args.list_sessions:
        term.info(f"== 已有记录（{Path(args.out_dir).expanduser()}）==")
        items = list_sessions(Path(args.out_dir).expanduser())
        if not items:
            term.info("  （空）")
        for item in items:
            term.info(
                f"  {item['stem']}  {item['started']}  {item['count']} 句  {item['duration']}s"
            )
        return 0

    try:
        spec = model_registry.resolve(args.model)
    except (FileNotFoundError, KeyError, ValueError) as e:
        term.info(f"模型问题: {e}")
        available = [s.name for s in model_registry.list_models()]
        if available:
            term.info(f"本机已下载: {', '.join(available)}")
        term.info("下载模型: bash scripts/download_models.sh")
        return 2

    # 先把音频源准备好（ws/tcp 会在这里绑定端口），浏览器才不会因为
    # 模型加载慢而连不上；之后再慢慢加载模型。
    try:
        source = build_source(args)
    except NoCaptureDevice:
        term.info("系统里没有可用的录音设备，无法直接用本机麦克风。")
        list_devices(term)
        return 3
    except Exception as e:
        term.info(f"打开音频来源失败: {e}")
        list_devices(term)
        return 3

    final_mode = args.commit == "final"

    refine = None
    refine_spec = None
    if args.refine_model and not final_mode:
        try:
            refine_spec = model_registry.resolve(args.refine_model)
        except (FileNotFoundError, KeyError, ValueError) as e:
            term.info(f"整句重识别模型不可用（{e}），本次只用实时结果")
            refine_spec = None
        if refine_spec is not None:
            refine = OfflineASR(refine_spec, num_threads=args.threads)
    elif final_mode:
        term.info("单模型模式：不用整句重识别，识别器自己判断定型")

    vad = None
    if not args.no_vad and not final_mode:
        vad_path = Path(args.vad_model) if args.vad_model else model_registry.vad_model_path()
        if not vad_path.is_file():
            term.info(f"找不到 VAD 模型 {vad_path}，改用端点检测切句（--no-vad）")
        else:
            vad = SpeechVAD(
                str(vad_path),
                sample_rate=SAMPLE_RATE,
                threshold=args.vad_threshold,
                min_silence_duration=args.vad_silence,
                max_speech_duration=args.vad_max_speech,
                num_threads=1,
            )
            term.info(
                f"VAD: 阈值 {args.vad_threshold}，静音 {args.vad_silence}s 切句，"
                f"单句最长 {args.vad_max_speech}s"
            )

    normalizer = None
    if args.agc and (vad is not None or final_mode):
        # 只在有 VAD 时才有意义：阈值判的是语音概率，电平太低会整体漏检
        normalizer = LevelNormalizer(sample_rate=SAMPLE_RATE, max_gain=args.agc_max_gain)
        term.info(f"电平归一化: 开（最多补 {args.agc_max_gain:.0f} 倍 = 有意压低也能识别）")
    elif args.agc:
        term.info("电平归一化: 跳过（没启用 VAD，用不上）")

    punct = None
    if not args.no_punct:
        if final_mode and spec.has_punct:
            # 识别模型自带标点（X-ASR 的 punct 版本），再挂标点模型会重复加标点
            term.info("标点: 识别模型自带，不再挂额外的标点模型")
        else:
            punct_path = (
                Path(args.punct_model) if args.punct_model else model_registry.punctuation_model_path()
            )
            if punct_path and punct_path.is_file():
                punct = Punctuator(str(punct_path))
            else:
                term.info("找不到标点模型，输出不带标点")

    term.info(f"模型: {spec.summary()}")
    if refine_spec is not None:
        term.info(f"整句重识别: {refine_spec.summary()}")
    term.info(f"音频来源: {source.describe()}")
    if args.source == "ws":
        term.info("在 Windows 的 Edge/Chrome 里打开上面的地址，点「开始说话」即可")
    term.info("开始听写，Ctrl+C 结束\n")

    decoding_method = "greedy_search"
    if args.hotwords:
        # sherpa-onnx 规定：带热词必须用 beam search
        decoding_method = "modified_beam_search"
        term.info("已启用热词，识别改用 modified_beam_search（更慢一点，更准）")
    asr = StreamingASR(
        spec,
        num_threads=args.threads,
        decoding_method=decoding_method,
        hotwords=args.hotwords,
        enable_endpoint=args.no_vad,
    )

    formats = tuple(f.strip() for f in args.formats.split(",") if f.strip())
    writer = TranscriptWriter(
        out_dir=Path(args.out_dir).expanduser(),
        formats=formats,
        name=args.out_name,
        meta={
            "模型": spec.name,
            "整句重识别": refine_spec.name if refine_spec else "关闭",
            "来源": source.describe(),
        },
    )
    term.info(f"记录文件: {writer.out_dir / (writer.stem + '.md')}")

    offset = 0.0
    if args.append:
        try:
            offset = writer.attach(args.append)
        except (ValueError, FileNotFoundError) as e:
            term.info(f"追加失败: {e}")
            return 2
        term.info(
            f"已追加到: {writer.out_dir / (writer.stem + '.md')}"
            f"（时间轴从 {offset:.1f}s 接着走）"
        )

    if final_mode:
        engine = FinalEngine(
            source, asr, writer, term, punct, chunk_ms=args.chunk_ms,
            normalizer=normalizer,
            margin=args.final_margin,
            pause=args.final_pause,
            max_sentence=args.final_max_sentence,
        )
        term.info(
            f"单模型模式: 定型判定 margin {args.final_margin}s，"
            f"停顿 {args.final_pause}s 断句，单句最长 {args.final_max_sentence}s"
        )
    else:
        engine = DictationEngine(
            source, asr, refine, vad, writer, term, punct, chunk_ms=args.chunk_ms,
            normalizer=normalizer,
        )
    engine._offset = offset
    if hasattr(source, "on_text"):
        source.on_text = engine.handle_control

    # 浏览器页面要显示这些，所以注册成一个「随时可取」的状态提供器。
    # 闭包读的是 writer 的当前值，改名 / 追加之后状态也跟着变。
    def status_provider() -> dict:
        return {
            "model": spec.name,
            "refine_model": refine_spec.name if refine_spec else "",
            "commit": args.commit,
            "source": source.describe(),
            "stem": writer.stem,
            "name": writer.name,
            "out_dir": str(writer.out_dir),
            "count": len(writer.segments),
            "duration": round(max((s.end for s in writer.segments), default=0.0), 1),
            "transcript": [
                {"start": s.start, "end": s.end, "text": s.text, "refined": s.refined}
                for s in writer.segments
            ],
        }

    source.set_status_provider(status_provider)
    source.set_sessions_provider(lambda: list_sessions(writer.out_dir, exclude=writer.stem))
    source.set_ready(True)

    try:
        engine.run()
    except KeyboardInterrupt:
        term.info("\n收到 Ctrl+C，正在收尾…")
        engine.finalize()
    finally:
        source.close()
        written = writer.close()

    term.info(f"\n共 {len(writer.segments)} 句，记录已写入：")
    for path in written:
        term.info(f"  {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
