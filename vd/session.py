"""听写记录的落盘：txt / md / srt / json。"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

DEFAULT_NAME = "听写"
_TIME_FORMAT = "%Y%m%d-%H%M%S"

_TXT_SEG = re.compile(r"^\[(\d+:\d+:\d+\.\d+) --> (\d+:\d+:\d+\.\d+)\] (.*)$")
_MD_SEG = re.compile(r"^##\s+(\d+:\d+:\d+)\s+→\s+(\d+:\d+:\d+)\s*$")


def sanitize_name(name: str, fallback: str = DEFAULT_NAME) -> str:
    """把外部给的名字洗成安全的文件名片段。

    浏览器端能把名字推过来，那是外部输入，得当成不可信数据处理：
    去掉路径分隔符和目录穿越、过滤非法字符与控制字符、限长。
    """
    name = (name or "").strip()
    name = re.sub(r"[\x00-\x1f\x7f]", "", name)
    name = name.replace("\\", "/").split("/")[-1]
    name = name.replace("..", "")
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    name = name.strip(". ")
    if not name:
        return fallback
    return name[:60]


def sanitize_stem(stem: str) -> str:
    """把外部给的文件名主干洗成安全的片段（同样防目录穿越）。"""
    stem = (stem or "").strip()
    stem = stem.replace("\\", "/").split("/")[-1]
    stem = stem.replace("..", "")
    stem = re.sub(r"[\x00-\x1f\x7f<>:\"\\|?*]", "_", stem)
    return stem[:120]


def _parse_hms(text: str) -> float:
    """``01:02:03.456`` -> 3723.456"""
    parts = text.strip().split(":")
    if len(parts) != 3:
        return 0.0
    try:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    except ValueError:
        return 0.0


@dataclass
class Segment:
    start: float
    end: float
    text: str
    refined: bool = False


def load_session(out_dir: Path, stem: str) -> tuple[list[Segment], str, datetime | None]:
    """读出一个已存在的会话（优先 json，其次 txt，再次 md）。

    返回 (句子列表, 名称, 开始时间)。文件不存在时返回空。
    """
    out_dir = Path(out_dir)

    path = out_dir / f"{stem}.json"
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            segs = [
                Segment(
                    start=float(s.get("start", 0.0)),
                    end=float(s.get("end", 0.0)),
                    text=s.get("text", ""),
                    refined=bool(s.get("refined", False)),
                )
                for s in data.get("segments", [])
            ]
            sess = data.get("session", {})
            name = str(sess.get("name", ""))
            started = None
            if sess.get("started_at"):
                try:
                    started = datetime.fromisoformat(str(sess["started_at"]))
                except ValueError:
                    started = None
            return segs, name, started
        except (OSError, ValueError, TypeError):
            pass

    path = out_dir / f"{stem}.txt"
    if path.is_file():
        segs: list[Segment] = []
        name = ""
        started = None
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                m = _TXT_SEG.match(line)
                if m:
                    segs.append(Segment(_parse_hms(m.group(1)), _parse_hms(m.group(2)), m.group(3)))
                elif line.startswith("# 名称:"):
                    name = line.split(":", 1)[1].strip()
                elif line.startswith("# 语音听写记录"):
                    ts = line.split("  ")[-1].strip()
                    try:
                        started = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
                    except ValueError:
                        pass
        except OSError:
            pass
        return segs, name, started

    path = out_dir / f"{stem}.md"
    if path.is_file():
        segs = []
        name = ""
        started = None
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return segs, name, started
        for i, line in enumerate(lines):
            if line.startswith("- 名称："):
                name = line.split("：", 1)[1].strip()
            elif line.startswith("- 开始时间："):
                try:
                    started = datetime.strptime(line.split("：", 1)[1].strip(), "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    pass
            m = _MD_SEG.match(line)
            if m:
                text_lines = []
                j = i + 1
                while j < len(lines) and lines[j].strip():
                    text_lines.append(lines[j].strip())
                    j += 1
                segs.append(
                    Segment(_parse_hms(m.group(1)), _parse_hms(m.group(2)), " ".join(text_lines))
                )
        return segs, name, started

    return [], "", None


def list_sessions(out_dir: Path, exclude: str = "") -> list[dict]:
    """列出已有会话，按开始时间倒序（新的在前）。

    ``exclude`` 用来排除当前正在写的那份（它是空的，出现在清单里只会让人困惑）。
    """
    out_dir = Path(out_dir)
    items = []
    for path in out_dir.glob("*.md"):
        stem = path.stem
        if exclude and stem == exclude:
            continue
        segs, name, started = load_session(out_dir, stem)
        if not segs and not name:
            continue
        items.append(
            {
                "stem": stem,
                "name": name or stem,
                "started": started.isoformat(timespec="seconds") if started else "",
                "count": len(segs),
                "duration": round(max((s.end for s in segs), default=0.0), 1),
            }
        )
    items.sort(key=lambda x: x["started"], reverse=True)
    return items


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

    文件名 = ``<名字>-<开始时间>``（如 ``会议-20261025-142211.md``），
    名字可以中途改（``set_name``），改名会同步改磁盘上的文件名。
    """

    out_dir: Path
    formats: tuple[str, ...] = ("txt", "md", "srt", "json")
    meta: dict = field(default_factory=dict)
    name: str = ""

    def __post_init__(self) -> None:
        self.out_dir = Path(self.out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.started_at = datetime.now()
        self.name = sanitize_name(self.name)
        self.stem = f"{self.name}-{self.started_at.strftime(_TIME_FORMAT)}"
        self.segments: list[Segment] = []
        self._txt = None
        if "txt" in self.formats:
            path = self.out_dir / f"{self.stem}.txt"
            self._txt = open(path, "w", encoding="utf-8")
            self._txt.write(f"# 语音听写记录  {self.started_at:%Y-%m-%d %H:%M:%S}\n")
            self._txt.write(f"# 名称: {self.name}\n")
            for k, v in self.meta.items():
                self._txt.write(f"# {k}: {v}\n")
            self._txt.write("\n")
            self._txt.flush()
        self._flush_all()

    @property
    def paths(self) -> list[Path]:
        return [
            self.out_dir / f"{self.stem}.{ext}"
            for ext in ("txt", "md", "srt", "json")
            if (self.out_dir / f"{self.stem}.{ext}").is_file()
        ]

    def set_name(self, name: str) -> str:
        """改名字：把磁盘上的文件一起改名，返回最终的文件名主干。

        时间戳保持不变，所以「会议」改成「评审」不会跟别的记录撞名。
        """
        new_name = sanitize_name(name, fallback=self.name)
        if new_name == self.name:
            return self.stem
        old_stem = self.stem
        self.name = new_name
        self.stem = f"{self.name}-{self.started_at.strftime(_TIME_FORMAT)}"
        for ext in ("txt", "md", "srt", "json"):
            old = self.out_dir / f"{old_stem}.{ext}"
            new = self.out_dir / f"{self.stem}.{ext}"
            if old.is_file() and not new.exists():
                old.replace(new)
        self._flush_all()
        return self.stem

    def attach(self, stem: str) -> float:
        """切到一份已有的记录继续写，返回时间偏移（秒）。

        已有内容会被读回来（句子、名称、开始时间），新说的内容接在后面，
        时间戳从原记录的最后一句之后接着走。返回的偏移量给上层用来平移时间轴。
        """
        stem = sanitize_stem(stem)
        if not stem:
            raise ValueError("记录文件名不能为空")
        if not (self.out_dir / f"{stem}.md").is_file():
            raise FileNotFoundError(f"没有这份记录: {stem}")

        segs, name, started = load_session(self.out_dir, stem)
        offset = max((s.end for s in segs), default=0.0)

        # 刚建好就改投别的记录的话，别把空壳留在目录里
        if not self.segments and self.stem != stem:
            for ext in ("txt", "md", "srt", "json"):
                stale = self.out_dir / f"{self.stem}.{ext}"
                if stale.is_file():
                    try:
                        stale.unlink()
                    except OSError:
                        pass

        if self._txt:
            self._txt.close()
            self._txt = None
        self.stem = stem
        if name:
            self.name = name
        if started:
            self.started_at = started
        self.segments = segs

        if "txt" in self.formats:
            path = self.out_dir / f"{stem}.txt"
            existed = path.is_file()
            self._txt = open(path, "a", encoding="utf-8")
            if not existed:
                self._txt.write(f"# 语音听写记录  {self.started_at:%Y-%m-%d %H:%M:%S}\n")
                self._txt.write(f"# 名称: {self.name}\n")
                for k, v in self.meta.items():
                    self._txt.write(f"# {k}: {v}\n")
                self._txt.write("\n")
                self._txt.flush()
        self._flush_all()
        return offset

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
        return self.paths

    def _write_md(self) -> Path:
        """按对话（每一句）分段写成 markdown。"""
        path = self.out_dir / f"{self.stem}.md"
        speech = sum(max(0.0, s.end - s.start) for s in self.segments)
        lines = [
            "# 语音听写记录",
            "",
            f"- 名称：{self.name}",
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
                "name": self.name,
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
