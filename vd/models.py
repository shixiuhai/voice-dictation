"""模型登记表与模型文件自动识别。

模型目录结构（由 ``scripts/download_models.sh`` 下载）::

    models/
      asr/<模型目录>/...
      vad/silero_vad.onnx
      punct/<模型目录>/model.int8.onnx
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_MODEL_ROOT = Path(__file__).resolve().parent.parent / "models"


def model_root() -> Path:
    return Path(os.environ.get("VD_MODEL_DIR", str(DEFAULT_MODEL_ROOT))).expanduser()


@dataclass
class ModelSpec:
    """一个可直接喂给 sherpa-onnx 的模型描述。"""

    name: str
    kind: str  # transducer | ctc | paraformer | paraformer_offline
    directory: Path
    tokens: str = ""
    encoder: str = ""
    decoder: str = ""
    joiner: str = ""
    model: str = ""
    modeling_unit: str = "cjkchar"
    desc: str = ""
    files: dict = field(default_factory=dict)

    @property
    def size_mb(self) -> float:
        total = 0
        for path in self.files.values():
            p = Path(path)
            if p.is_file():
                total += p.stat().st_size
        return total / 1e6

    def summary(self) -> str:
        return f"{self.name} [{self.kind}] {self.size_mb:.0f} MB — {self.desc}"


# name -> (相对 models/ 的目录, 类型, 说明)
REGISTRY: dict[str, tuple[str, str, str]] = {
    "bilingual": (
        "asr/sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20",
        "transducer",
        "中英双语流式 zipformer，int8 编码器 174 MB，准确率最高，2 核机器上约 0.6x 实时",
    ),
    "small-bilingual": (
        "asr/sherpa-onnx-streaming-zipformer-small-bilingual-zh-en-2023-02-16",
        "transducer",
        "中英双语小型流式 zipformer，50 MB，2 核机器上约 4.3x 实时，默认模型",
    ),
    "zh-14m": (
        "asr/sherpa-onnx-streaming-zipformer-zh-14M-2023-02-23-mobile",
        "transducer",
        "中文 14M 参数流式 zipformer，int8 编码器 15 MB，只支持中文",
    ),
    "ctc-zh": (
        "asr/sherpa-onnx-streaming-zipformer-small-ctc-zh-int8-2025-04-01",
        "ctc",
        "中文小型 CTC 流式模型，21 MB，最快，但只支持中文",
    ),
    "paraformer-zh": (
        "asr/sherpa-onnx-paraformer-zh-small-2024-03-09",
        "paraformer_offline",
        "中文非流式 paraformer，78 MB，适合整句重识别（校正文稿），不是流式模型",
    ),
}

DEFAULT_MODEL = "small-bilingual"
DEFAULT_REFINE_MODEL = "paraformer-zh"


def _pick(directory: Path, prefix: str) -> str:
    """在目录里挑一个模型文件：优先 int8，其次 fp32。"""
    candidates = sorted(directory.glob(f"{prefix}*.onnx"))
    if not candidates:
        return ""
    for c in candidates:
        if "int8" in c.name:
            return str(c)
    return str(candidates[0])


def detect(directory: Path, name: str, kind: str) -> ModelSpec:
    """根据目录内容拼出 ModelSpec。"""
    tokens = directory / "tokens.txt"
    spec = ModelSpec(name=name, kind=kind, directory=directory, tokens=str(tokens))
    if kind == "transducer":
        spec.encoder = _pick(directory, "encoder")
        spec.decoder = _pick(directory, "decoder")
        spec.joiner = _pick(directory, "joiner")
        spec.files = {
            "tokens": spec.tokens,
            "encoder": spec.encoder,
            "decoder": spec.decoder,
            "joiner": spec.joiner,
        }
    elif kind in ("ctc", "paraformer_offline"):
        spec.model = _pick(directory, "model")
        spec.files = {"tokens": spec.tokens, "model": spec.model}
    elif kind == "paraformer":
        spec.encoder = _pick(directory, "encoder")
        spec.decoder = _pick(directory, "decoder")
        spec.files = {
            "tokens": spec.tokens,
            "encoder": spec.encoder,
            "decoder": spec.decoder,
        }
    else:
        raise ValueError(f"未知模型类型: {kind}")
    return spec


def resolve(name: str) -> ModelSpec:
    """按名字取模型；名字也可以直接是模型目录的绝对路径。"""
    root = model_root()
    if name in REGISTRY:
        rel, kind, desc = REGISTRY[name]
        directory = root / rel
        if not directory.is_dir():
            raise FileNotFoundError(
                f"模型 {name} 未下载，期望目录 {directory}\n"
                f"先跑 scripts/download_models.sh，或用 --model 指定别的模型"
            )
        spec = detect(directory, name, kind)
        spec.desc = desc
        return spec

    directory = Path(name).expanduser()
    if directory.is_dir():
        return detect(directory, directory.name, _guess_kind(directory))

    raise KeyError(
        f"未知模型 {name}。已登记: {', '.join(sorted(REGISTRY))}；也可以直接给模型目录路径"
    )


def _guess_kind(directory: Path) -> str:
    """没登记过的目录，按文件名猜类型。"""
    if _pick(directory, "encoder") and _pick(directory, "joiner"):
        return "transducer"
    if "paraformer" in directory.name.lower():
        return "paraformer_offline"
    if _pick(directory, "model"):
        return "ctc"
    raise ValueError(f"看不懂 {directory} 里的模型文件")


def list_models() -> list[ModelSpec]:
    """列出所有已下载的模型（含未登记但能识别的目录）。"""
    root = model_root()
    out: list[ModelSpec] = []
    for name, (rel, kind, desc) in sorted(REGISTRY.items()):
        directory = root / rel
        if directory.is_dir():
            spec = detect(directory, name, kind)
            spec.desc = desc
            out.append(spec)
    return out


def vad_model_path() -> Path:
    return model_root() / "vad" / "silero_vad.onnx"


def punctuation_model_path() -> Path | None:
    root = model_root() / "punct"
    if not root.is_dir():
        return None
    for path in sorted(root.glob("*/model.int8.onnx")) + sorted(root.glob("*/model.onnx")):
        return path
    return None
