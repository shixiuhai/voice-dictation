"""听写记录的落盘：txt / md / srt / json。"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass
class Segment:
    start: float
    end: float
    text: str
    refined: bool = False


def _hms(seconds: float, comma: bool = False) -> str:
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    if comma:
        return f"{h:02d}:{m:02d}:{s:06.3f}".replace(".", ",")
    return f"{h:02d}:{m:02d}:{s:06.3f}"


@dataclass
class TranscriptWriter:
    """把一句一句的识别结果写成听写记录文件。

    所有格式都是**实时落盘**：每加一句就重写一遍 md/srt/json，
    进程被 kill、浏览器断连也不会丢内容。
    """

    out_dir: Path
    formats: tuple[str, ...] = ("txt", "md", "srt", "json")
    meta: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.out_dir = Path(self.out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.started_at = datetime.now()
        self.stem = self.started_at.strftime("%Y%m%d-%H%M%S")
        self.segments: list[Segment] = []
        self._txt = None
        if "txt" in self.formats:
            path = self.out_dir / f"{self.stem}.txt"
            self._txt = open(path, "w", encoding="utf-8")
            self._txt.write(f"# 语音听写记录  {self.started_at:%Y-%m-%d %H:%M:%S}\n")
            for k, v in self.meta.items():
                self._txt.write(f"# {k}: {v}\n")
            self._txt.write("\n")
            self._txt.flush()
        self._flush_all()

    def add(self, start: float, end: float, text: str, refined: bool = False) -> Segment:
        seg = Segment(start=start, end=end, text=text, refined=refined)
        self.segments.append(seg)
        if self._txt:
            self._txt.write(f"[{_hms(start)} --> {_hms(end)}] {text}\n")
            self._txt.flush()
        self._flush_all()
        return seg

    def _flush_all(self) -> None:
        """把 md / srt / json 重写一遍，保证磁盘上的内容是最新的。"""
        if "md" in self.formats:
            self._write_md()
        if "srt" in self.formats:
            self._write_srt()
        if "json" in self.formats:
            self._write_json()

    def close(self) -> list[Path]:
        if self._txt:
            self._txt.close()
            self._txt = None
        self._flush_all()
        written = []
        for ext in ("txt", "md", "srt", "json"):
            path = self.out_dir / f"{self.stem}.{ext}"
            if path.is_file():
                written.append(path)
        return written

    def _write_md(self) -> Path:
        """按对话（每一句）分段写成 markdown。"""
        path = self.out_dir / f"{self.stem}.md"
        speech = sum(max(0.0, s.end - s.start) for s in self.segments)
        lines = [
            "# 语音听写记录",
            "",
            f"- 开始时间：{self.started_at:%Y-%m-%d %H:%M:%S}",
            f"- 结束时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
            f"- 句数：{len(self.segments)}",
            f"- 语音时长：{speech:.1f} s",
        ]
        for k, v in self.meta.items():
            lines.append(f"- {k}：{v}")
        lines += ["", "---", ""]
        for s in self.segments:
            lines.append(f"## {_hms(s.start)[:8]} → {_hms(s.end)[:8]}")
            lines.append("")
            lines.append(s.text)
            lines.append("")
        path.write_text("\n".join(lines), encoding="utf-8")
        return path

    def _write_srt(self) -> Path:
        path = self.out_dir / f"{self.stem}.srt"
        blocks = []
        for i, s in enumerate(self.segments, 1):
            blocks.append(
                f"{i}\n{_hms(s.start, comma=True)} --> {_hms(s.end, comma=True)}\n{s.text}\n"
            )
        path.write_text("\n".join(blocks), encoding="utf-8")
        return path

    def _write_json(self) -> Path:
        path = self.out_dir / f"{self.stem}.json"
        payload = {
            "session": {
                "started_at": self.started_at.isoformat(timespec="seconds"),
                "ended_at": datetime.now().isoformat(timespec="seconds"),
                "segments": len(self.segments),
                **self.meta,
            },
            "segments": [
                {"start": s.start, "end": s.end, "text": s.text, "refined": s.refined}
                for s in self.segments
            ],
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path


class Terminal:
    """终端输出：正在说的半句用灰色原地刷新，说完的句子另起一行定格。

    输出不是终端（比如重定向到文件、或者跑在管道里）时不刷半句，只输出定稿句子。
    """

    def __init__(self, stream=None, color: bool = True, live: bool = True):
        import sys

        self._out = stream or sys.stdout
        self._color = color
        self._live = live
        self._partial = ""

    def _c(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self._color else text

    def partial(self, text: str) -> None:
        if not self._live or text == self._partial:
            return
        self._partial = text
        line = self._c("2", f"  … {text}") if text else ""
        self._out.write("\r\033[2K" + line)
        self._out.flush()

    def clear_partial(self) -> None:
        if self._live and self._partial:
            self._out.write("\r\033[2K")
            self._out.flush()
        self._partial = ""

    def segment(self, start: float, end: float, text: str, refined: bool = False) -> None:
        self.clear_partial()
        stamp = self._c("36", f"[{_hms(start)[:8]} → {_hms(end)[:8]}]")
        body = self._c("1", text) if refined else text
        self._out.write(f"{stamp} {body}\n")
        self._out.flush()

    def info(self, text: str) -> None:
        self.clear_partial()
        self._out.write(self._c("33", text) + "\n")
        self._out.flush()
