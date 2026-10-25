#!/usr/bin/env bash
# 下载听写程序需要的模型：流式 ASR、非流式 ASR（整句重识别）、VAD、标点模型。
#
# 用法:
#   bash scripts/download_models.sh                 # 下载默认的几个
#   bash scripts/download_models.sh bilingual       # 只下载指定的模型
#
# 走代理的话直接设环境变量即可：
#   HTTPS_PROXY=http://192.168.6.9:7890 bash scripts/download_models.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODELS="$ROOT/models"
ASR_BASE="https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models"
PUNCT_BASE="https://github.com/k2-fsa/sherpa-onnx/releases/download/punctuation-models"

mkdir -p "$MODELS/asr" "$MODELS/vad" "$MODELS/punct"

fetch() { # fetch <url> <目标文件>
  local url="$1" dest="$2"
  if [ -s "$dest" ]; then
    echo "  已存在，跳过: $(basename "$dest")"
    return 0
  fi
  echo "  下载: $(basename "$dest")"
  curl -fSL --retry 5 --retry-delay 3 -C - -o "$dest" "$url"
}

unpack() { # unpack <压缩包> <解压目录>
  local archive="$1" dest="$2"
  echo "  解压: $(basename "$archive")"
  python3 - "$archive" "$dest" <<'PY'
import sys, tarfile
archive, dest = sys.argv[1], sys.argv[2]
with tarfile.open(archive, "r:bz2") as tf:
    tf.extractall(dest)
PY
}

download_asr() { # download_asr <名字> <压缩包名>
  local tag="$1" archive="$2"
  local dir="$MODELS/asr/sherpa-onnx-$archive"
  if [ -d "$dir" ]; then
    echo "  已存在，跳过: $tag"
    return 0
  fi
  fetch "$ASR_BASE/sherpa-onnx-$archive.tar.bz2" "$MODELS/asr/$archive.tar.bz2"
  unpack "$MODELS/asr/$archive.tar.bz2" "$MODELS/asr"
  rm -f "$MODELS/asr/$archive.tar.bz2"
}

wanted() { # wanted <名字>；不传参数表示全要
  [ $# -eq 0 ] && return 0
  for w in "$@"; do [ "$w" = "$1" ] && return 0; done
  return 1
}

TARGETS=("$@")
if [ ${#TARGETS[@]} -eq 0 ]; then
  TARGETS=(small-bilingual zh-14m ctc-zh paraformer-zh bilingual)
fi

echo "== 流式 / 非流式 ASR 模型 =="
for t in "${TARGETS[@]}"; do
  case "$t" in
    small-bilingual)
      download_asr "$t" "streaming-zipformer-small-bilingual-zh-en-2023-02-16"
      ;;
    bilingual)
      download_asr "$t" "streaming-zipformer-bilingual-zh-en-2023-02-20"
      ;;
    zh-14m)
      download_asr "$t" "streaming-zipformer-zh-14M-2023-02-23-mobile"
      ;;
    ctc-zh)
      download_asr "$t" "streaming-zipformer-small-ctc-zh-int8-2025-04-01"
      ;;
    paraformer-zh)
      download_asr "$t" "paraformer-zh-small-2024-03-09"
      ;;
    *)
      echo "  未知模型名: $t（可选: small-bilingual bilingual zh-14m ctc-zh paraformer-zh）"
      ;;
  esac
done

echo "== VAD（语音活动检测）=="
fetch "$ASR_BASE/silero_vad.onnx" "$MODELS/vad/silero_vad.onnx"

echo "== 标点模型 =="
if [ ! -f "$MODELS/punct/sherpa-onnx-punct-ct-transformer-zh-en-vocab272727-2024-04-12-int8/model.int8.onnx" ]; then
  fetch "$PUNCT_BASE/sherpa-onnx-punct-ct-transformer-zh-en-vocab272727-2024-04-12-int8.tar.bz2" \
        "$MODELS/punct/punct-int8.tar.bz2"
  unpack "$MODELS/punct/punct-int8.tar.bz2" "$MODELS/punct"
  rm -f "$MODELS/punct/punct-int8.tar.bz2"
fi

echo
echo "下载完成，模型目录：$MODELS"
du -sh "$MODELS"/* 2>/dev/null || true
