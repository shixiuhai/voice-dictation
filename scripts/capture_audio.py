#!/usr/bin/env python3
"""浏览器采集「正在播放的音频」并存成 WAV，用来做离线分析。

和 ``dictation.py --source ws`` 的区别：那个是**识别**，这个是**抓原始音频**。
用途是分析——把真实场景的声音（电视剧、会议、远场说话）原封不动存下来，
量一量电平、静音比例、VAD 怎么看它，再拿去调参或对比模型。

页面上有两种采集方式：

- **系统 / 标签页音频**：``getDisplayMedia`` 数字直采，拿到的是播放器解出来的
  原始音频，没有房间混响、没有麦克风 AGC。分析「内容本身有多难」时用这个。
- **麦克风**：``getUserMedia``，跟真实听写链路一样。分析「房间/麦克风把它毁了多少」用这个。

用法::

    python3 scripts/capture_audio.py --port 8770 --out /tmp/tv.wav --seconds 120
    python3 scripts/capture_audio.py --port 8770            # 一直录，Ctrl+C 收尾

然后 Windows 浏览器打开 http://localhost:8770/，选一种采集方式，开始录。
Ctrl+C 会把 WAV 的头部补完整，并打印一份电平 / 静音 / VAD 的分析。
"""

from __future__ import annotations

import argparse

import sys
import time
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vd.session import Terminal  # noqa: E402
from vd.wsserver import MicWebServer  # noqa: E402

SAMPLE_RATE = 16000


def dbfs(x: float) -> float:
    return 20 * np.log10(max(x, 1e-9))


def show_db(x: float) -> str:
    """给终端看的电平：安静到一定程度以下就统一显示「静音」，别刷 -180。"""
    if x < 1e-7:
        return "静音"
    return f"{dbfs(x):.1f} dBFS"


class Recorder:
    """收 PCM 写 WAV，顺便统计。"""

    def __init__(self, path: Path, sample_rate: int = SAMPLE_RATE):
        self.path = path
        self.sample_rate = sample_rate
        path.parent.mkdir(parents=True, exist_ok=True)
        self._w = wave.open(str(path), "wb")
        self._w.setnchannels(1)
        self._w.setsampwidth(2)
        self._w.setframerate(sample_rate)
        self.frames: list[np.ndarray] = []
        self.seconds = 0.0
        self.clipped = 0
        self.total = 0

    def write(self, chunk: np.ndarray) -> None:
        if not len(chunk):
            return
        self.frames.append(chunk.astype(np.float32))
        clipped = np.abs(chunk) >= 0.999
        self.clipped += int(clipped.sum())
        self.total += len(chunk)
        self.seconds += len(chunk) / self.sample_rate
        pcm = np.clip(chunk * 32767.0, -32768, 32767).astype(np.int16)
        self._w.writeframes(pcm.tobytes())

    def close(self) -> None:
        try:
            self._w.close()
        except Exception:
            pass

    def data(self) -> np.ndarray:
        return np.concatenate(self.frames) if self.frames else np.zeros(0, dtype=np.float32)


def analyze(rec: Recorder, term: Terminal) -> None:
    x = rec.data()
    if not len(x):
        term.info("没有收到任何音频。")
        return
    dur = len(x) / rec.sample_rate
    rms = float(np.sqrt(np.mean(x ** 2)))
    peak = float(np.max(np.abs(x)))

    term.info("")
    term.info("=" * 62)
    term.info("录音分析")
    term.info("=" * 62)
    term.info(f"文件      : {rec.path}")
    term.info(f"时长      : {dur:.1f}s（{len(x)} 采样 @ {rec.sample_rate} Hz）")
    term.info(f"整体电平  : RMS {show_db(rms)}   峰值 {show_db(peak)}")
    term.info(f"削顶      : {rec.clipped} 采样（{rec.clipped / max(1, rec.total) * 100:.3f}%）")

    # 逐 100ms 帧的分布
    n = rec.sample_rate // 10
    frames = x[: len(x) // n * n].reshape(-1, n)
    if len(frames):
        fr = np.sqrt(np.mean(frames.astype(np.float64) ** 2, axis=1))
        fd = np.array([dbfs(v) for v in fr])
        loud = fd[fd > -90]
        buckets = [(-90, -60), (-60, -50), (-50, -40), (-40, -30), (-30, -20), (-20, 0)]
        term.info("")
        term.info("电平分布（每 100ms 一帧）:")
        for lo, hi in buckets:
            pct = float(np.mean((fd >= lo) & (fd < hi))) * 100
            bar = "#" * int(pct / 2)
            term.info(f"  {lo:>4}~{hi:>4} dBFS  {pct:5.1f}%  {bar}")
        silent = float(np.mean(fd <= -90)) * 100
        if silent > 0.5:
            term.info(f"  静音帧（≤ -90 dBFS）  {silent:5.1f}%")
        if len(loud):
            term.info(f"  （有声帧里）最安静的 10% 在 {np.percentile(loud, 10):.1f} dBFS 以下，"
                      f"中位数 {np.median(loud):.1f}，最响 1% 在 {np.percentile(loud, 99):.1f} 以上")
        else:
            term.info("  没有任何有声帧——这段音频几乎是静音。")

    # 用和识别链路一样的 VAD 看看它怎么切
    try:
        import sherpa_onnx  # noqa: F401

        from vd import models as model_registry
        from vd.vad import SpeechVAD

        vad_path = model_registry.vad_model_path()
        if vad_path.is_file():
            for thr in (0.5, 0.35):
                vad = SpeechVAD(str(vad_path), sample_rate=rec.sample_rate,
                                threshold=thr, num_threads=1)
                win = vad.window_size
                segs = []
                for i in range(0, len(x) - win, win):
                    vad.accept(x[i:i + win])
                    while vad.has_segment:
                        segs.append(vad.pop_segment())
                vad.flush()
                while vad.has_segment:
                    segs.append(vad.pop_segment())
                covered = sum(len(s.samples) for s in segs) / rec.sample_rate
                lens = [len(s.samples) / rec.sample_rate for s in segs]
                term.info("")
                term.info(f"VAD 阈值 {thr}: {len(segs)} 段，覆盖 {covered:.1f}s "
                          f"({covered / dur * 100:.0f}%)")
                if lens:
                    term.info(f"  段长 中位数 {np.median(lens):.1f}s  "
                              f"最短 {min(lens):.1f}s  最长 {max(lens):.1f}s")
    except Exception as e:
        term.info(f"（VAD 分析跳过: {e}）")

    term.info("")
    term.info(f"音频已存到 {rec.path}，可以直接喂给 dictation.py 试：")
    term.info(f"  python3 dictation.py --file {rec.path}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="浏览器采集音频存 WAV（分析用）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--host", default="0.0.0.0", help="监听地址")
    p.add_argument("--port", type=int, default=8770, help="监听端口")
    p.add_argument("--out", default="/tmp/vd_capture.wav", help="WAV 输出路径")
    p.add_argument("--seconds", type=float, default=0.0,
                   help="录满多少秒就自动收尾并退出（0 = 一直录到 Ctrl+C）")
    p.add_argument("--quiet", action="store_true", help="不打印每秒的状态")
    args = p.parse_args(argv)

    term = Terminal(color=sys.stdout.isatty(), live=sys.stdout.isatty())
    page = Path(__file__).resolve().parent.parent / "vd" / "capture_client.html"
    if not page.is_file():
        term.info(f"找不到页面文件 {page}")
        return 2

    out = Path(args.out).expanduser()
    rec = Recorder(out)
    # once=True：浏览器一点「停止采集」（WebSocket 一断），chunks() 就结束，
    # 立刻收尾并出分析。否则服务端会一直等下一个客户端，录完不给人结果。
    server = MicWebServer(
        host=args.host, port=args.port,
        page=page.read_text(encoding="utf-8"), sample_rate=SAMPLE_RATE, once=True,
    )
    server.set_ready(True)
    host, port = server.address
    shown = "127.0.0.1" if host in ("0.0.0.0", "") else host

    term.info("=" * 62)
    term.info("浏览器采音（分析用）")
    term.info("=" * 62)
    term.info(f"页面      : http://{shown}:{port}/")
    term.info(f"输出      : {out}")
    term.info(f"录音时长  : {'不限（Ctrl+C 收尾）' if args.seconds <= 0 else f'{args.seconds:.0f}s'}")
    term.info("")
    term.info("Windows 的 Edge/Chrome 打开上面地址。页面里两种采集方式：")
    term.info("  · 系统/标签页音频  —— 勾上「分享系统音频」/「分享标签页音频」")
    term.info("                     拿到的是播放器的原始音频，没有房间混响")
    term.info("  · 麦克风          —— 跟真实听写链路一样，有房间和麦克风的影响")
    term.info("")

    stop = {"flag": False}
    started = time.monotonic()
    last_report = started
    try:
        for chunk in server.chunks(chunk_ms=100):
            if not len(chunk):
                continue
            rec.write(chunk)
            now = time.monotonic()
            if not args.quiet and now - last_report >= 1.0:
                last_report = now
                rms = float(np.sqrt(np.mean(np.asarray(chunk) ** 2))) if len(chunk) else 0.0
                bar = int(min(1.0, rms / 0.3) * 30)
                term.info(f"\r  已录 {rec.seconds:7.1f}s  电平 {dbfs(rms):6.1f} dBFS "
                          f"{'|' * bar:<30}  Ctrl+C 收尾")
            if args.seconds > 0 and rec.seconds >= args.seconds:
                term.info("\n录满设定时长，收尾。")
                break
    except KeyboardInterrupt:
        # 让 Ctrl+C 直接冒到最外层收尾（别把它吞在循环里）
        term.info("\n收到 Ctrl+C，收尾。")
    finally:
        server.close()
        rec.close()

    term.info(f"\n实际录到 {rec.seconds:.1f}s（墙钟 {time.monotonic() - started:.1f}s）")
    analyze(rec, term)
    return 0


if __name__ == "__main__":
    sys.exit(main())
