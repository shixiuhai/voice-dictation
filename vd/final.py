"""单模型听写：流式识别自己判断「这段文字已经定型」，定型了就落盘。

原来的流程是「VAD 切段 → 每段新开一个流式识别 → 段末再跑一遍离线模型」。
这套流程有个硬伤：**每个 VAD 边界都重开识别器**，句子开头没有左上下文，
实测会整句丢字（``samples/0.wav`` 的第二句，流式输出是空字符串）。

这里的做法是**只用一个模型**：

- 识别器一直吃连续音频流，不切段、不重置，所以每句都有左上下文；
- 识别器不停给出「当前假设」，越靠前的部分越不会变；
- **盯着假设文本的稳定前缀**：一段文字连续 ``margin`` 秒没被改过，
  就认为它已经定型，可以提交。改过的部分退回「还在变」。

这跟识别器自己的 token 时间戳无关，也不依赖 VAD——纯粹是「这段字不再变了」
的判断，跟 Whisper streaming 的 local agreement、ElevenLabs 的 commit
策略是同一类做法。

定型的文字攒进当前句，遇到句末标点（模型自带）就整句提交落盘；
模型不带标点时，退化成「说话停顿就断句」。**提交过的文字不再修改**，
这就是 final 的含义。

X-ASR 的 punct 版本把标点和大小写直接烤进了词表，所以标点不需要额外的
标点模型，VAD 也不需要了，一个模型就够。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import numpy as np

# 句末标点（中英文）。句中的逗号、顿号不算。
_TERMINATORS = "。！？!?…"

# 中文之间的空格要去掉（BPE 模型的输出是「昨 天 是」这样带空格的）
_CJK_RANGE = r"㐀-䶿一-鿿豈-﫿"
_CJK_PUNCT = re.escape("，。！？；：、（）《》〈〉【】「」『』“”‘’")
_ASCII_PUNCT_NO_LEADING_SPACE = re.escape(",.!?;:%)]}")


def normalize_cjk_spacing(text: str) -> str:
    """去掉中文字符之间的空格。

    X-ASR 用的是 BPE 词表，输出里汉字之间带空格（「昨 天 是 Monday」），
    官方就是这么规整的。
    """
    if not text:
        return text
    text = re.sub(rf"(?<=[{_CJK_RANGE}])\s+(?=[{_CJK_RANGE}])", "", text)
    text = re.sub(rf"(?<=[{_CJK_RANGE}])\s+(?=[{_CJK_PUNCT}])", "", text)
    text = re.sub(rf"(?<=[{_CJK_PUNCT}])\s+(?=[{_CJK_RANGE}])", "", text)
    text = re.sub(rf"(?<=[{_CJK_PUNCT}])\s+(?=[{_CJK_PUNCT}])", "", text)
    text = re.sub(rf"\s+(?=[{_ASCII_PUNCT_NO_LEADING_SPACE}])", "", text)
    return text


@dataclass
class Sentence:
    """一句已经定型、可以落盘的话。

    ``uncertain`` 表示断句点不够确定（靠说话停顿断的，不是模型自己标了句号）。
    这种句子先存着，等下一句来了把可能粘在句首的标点并回来再落盘。
    """

    text: str
    start: float
    end: float
    uncertain: bool = False


class FinalCommitter:
    """把流式识别的假设切成「已定型」和「还在变」两半。

    只用一条 stream，音频连续喂进去，永不重置。
    """

    def __init__(
        self,
        asr,
        sample_rate: int,
        margin: float = 1.0,
        pause: float = 1.2,
        max_sentence: float = 30.0,
    ):
        self._asr = asr
        self._sr = sample_rate
        self._margin = margin
        self._pause = pause
        self._max_sentence = max_sentence

        self._stream = asr.new_stream()
        self._fed = 0  # 已喂进去的采样数
        self._hyp = ""  # 当前的假设文本
        self._history: list[tuple[float, str]] = []  # (时刻, 当时的假设)
        self._settled = 0  # 已经定型、并进当前句的字符数
        self._buf = ""  # 当前句攒着的文字
        self._buf_pos = 0  # 当前句在假设文本里的起始字符下标
        self._buf_start = 0.0  # 当前句的起始音频时间
        self._last_growth = 0.0  # 假设最后一次变长的时刻（用来判断说话停没停）
        self._tokens: list[str] = []
        self._stamps: list[float] = []

    @property
    def elapsed(self) -> float:
        """已经过去的音频时长（秒）。"""
        return self._fed / self._sr

    def feed(self, samples: np.ndarray) -> tuple[str, list[Sentence]]:
        """喂一块音频。

        返回 ``(当前半句, 本次新定型的句子)``。当前半句是还没提交的部分，
        用来做实时预览。
        """
        if len(samples):
            self._asr.feed(self._stream, samples, self._sr)
            self._fed += len(samples)
        self._asr.decode(self._stream)
        return self._collect()

    def finalize(self) -> list[Sentence]:
        """输入结束时把最后一句收尾（补一段静音逼出尾字）。"""
        tail = np.zeros(int(self._sr * 1.0), dtype=np.float32)
        self._asr.feed(self._stream, tail, self._sr)
        self._fed += len(tail)
        self._asr.decode(self._stream)
        self._refresh()
        if len(self._hyp) > self._settled:
            if not self._buf:
                self._buf_pos = self._settled
                self._buf_start = self._time_at(self._buf_pos)[0]
            self._buf += self._hyp[self._settled :]
            self._settled = len(self._hyp)
        return self._flush(self.elapsed, force=True)

    def _refresh(self) -> None:
        """更新假设文本和稳定前缀。"""
        res = self._asr.result_all(self._stream)
        self._tokens = list(res.tokens)
        self._stamps = list(res.timestamps)
        hyp = "".join(self._tokens)
        now = self.elapsed

        if len(hyp) > len(self._hyp):
            self._last_growth = now
        self._hyp = hyp
        self._history.append((now, hyp))
        # 只留最近 margin 之内 + 一条更早的做参照，别让历史无限增长
        cutoff = now - 2 * self._margin
        while len(self._history) > 1 and self._history[1][0] <= cutoff:
            self._history.pop(0)

    def _stable_prefix(self, now: float) -> int:
        """已经定型的长度 = 当前假设和 ``margin`` 秒之前的假设的公共前缀。

        这就是 Whisper streaming / 流式听写常用的「局部一致性（local
        agreement）」：一个词只要连续 ``margin`` 秒没被改写，就认为它定型了。

        不能拿「当前假设 vs 上一次假设」来比——流式假设是单调追加的，
        下一次的公共前缀永远等于上一次的全长，那样永远判不出定型。
        得跟**一段时间之前**的假设比，中间那些被改写的部分才会露出来。
        """
        cutoff = now - self._margin
        old = ""
        for ts, h in self._history:
            if ts <= cutoff:
                old = h
            else:
                break
        if not old:
            return 0
        return _common_prefix(old, self._hyp)

    def _collect(self) -> tuple[str, list[Sentence]]:
        self._refresh()
        now = self.elapsed

        safe = self._stable_prefix(now)
        if safe > self._settled:
            if not self._buf:
                self._buf_pos = self._settled
                self._buf_start = self._time_at(self._buf_pos)[0]
            self._buf += self._hyp[self._settled : safe]
            self._settled = safe

        sentences = self._flush(now)
        pending = self._buf + self._hyp[self._settled :]
        return pending, sentences

    def _flush(self, now: float, force: bool = False) -> list[Sentence]:
        """把攒够的句子提交出去。"""
        out: list[Sentence] = []

        # 句末标点 -> 断句
        while True:
            idx = _find_terminator(self._buf)
            if idx is None:
                break
            end_pos = self._buf_pos + idx + 1
            text = self._buf[: idx + 1].strip()
            if text:
                # 模型自己标了句末标点，断句点是确定的
                out.append(
                    Sentence(text, self._buf_start, self._time_at(end_pos)[1], uncertain=False)
                )
            self._buf = self._buf[idx + 1 :]
            self._buf_pos = end_pos
            if self._buf:
                self._buf_start = self._time_at(end_pos)[0]
            else:
                self._buf = ""
                self._buf_pos = 0
                self._buf_start = 0.0

        # 说话停顿了 -> 断句（模型不带标点时靠这个）
        # 或者这句太长了 -> 断句（防止一直不出句）
        if self._buf:
            stopped = now - self._last_growth > self._pause
            too_long = now - self._buf_start > self._max_sentence
            if force or stopped or too_long:
                text = self._buf.strip()
                if text:
                    # 靠停顿（或超时）断的，断句点不一定准，标点可能会粘到下一句
                    out.append(
                        Sentence(
                            text,
                            self._buf_start,
                            self._time_at(self._buf_pos + len(self._buf))[1],
                            uncertain=True,
                        )
                    )
                self._buf = ""
                self._buf_pos = 0
                self._buf_start = 0.0
        return out

    def _time_at(self, pos: int) -> tuple[float, float]:
        """字符位置 pos 处的音频时间。

        返回 ``(前一个 token 的结束时刻, 该处 token 的结束时刻)``。
        token 的时间戳是它在音频里的**结束**时刻，所以起点取前一个 token 的
        结束时刻。模型给的时间戳有零点几秒到一秒的偏差，只用来标时间轴。
        """
        if not self._tokens:
            return 0.0, 0.0
        cur = 0
        prev_ts = None
        for token, ts in zip(self._tokens, self._stamps):
            nxt = cur + len(token)
            if nxt >= pos:
                return (prev_ts if prev_ts is not None else ts), ts
            prev_ts = ts
            cur = nxt
        return (prev_ts or 0.0), (prev_ts or 0.0)


def _common_prefix(a: str, b: str) -> int:
    """两个字符串的公共前缀长度。"""
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


def _find_terminator(text: str) -> int | None:
    """句子里第一个句末标点的位置（字符下标）。"""
    for i, ch in enumerate(text):
        if ch in _TERMINATORS:
            return i
    return None
