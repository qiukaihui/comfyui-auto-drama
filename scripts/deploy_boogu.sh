#!/usr/bin/env bash
set -euo pipefail

# Boogu-Image 本地生图服务一键部署脚本（Apple Silicon / macOS，MLX）
# 用法：bash scripts/deploy_boogu.sh [--skip-models]
#   --skip-models  跳过模型下载（已下载过时使用）

BOOGU_ROOT="${BOOGU_ROOT:-$HOME/.boogu}"
PKG_DIR="$BOOGU_ROOT/boogu-image-mlx"
MODEL_DIR="$BOOGU_ROOT/models"
SKIP_MODELS=0
[[ "${1:-}" == "--skip-models" ]] && SKIP_MODELS=1

echo "==> 1/4 检查环境（需要 macOS + Apple Silicon）"
if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "错误：Boogu-Image MLX 仅支持 macOS（Apple Silicon）。" >&2
  echo "Windows/Linux 用户请使用云端生图：config.json 的 image_gen.provider 设为 cloud，" >&2
  echo "并填写 OpenAI 兼容接口（base_url / api_key / model），详见 CONFIG.md。" >&2
  exit 1
fi
if [[ "$(uname -m)" != "arm64" ]]; then
  echo "错误：需要 Apple Silicon（arm64）。" >&2
  exit 1
fi
command -v python3 >/dev/null || { echo "缺少 python3" >&2; exit 1; }

echo "==> 2/4 安装 Python 依赖（mlx / mlx-vlm / fastapi / uvicorn / pillow）"
python3 -m pip install --quiet mlx mlx-vlm fastapi "uvicorn[standard]" pillow

echo "==> 3/4 安装 boogu-image-mlx 管线"
if [ ! -d "$PKG_DIR/.git" ]; then
  mkdir -p "$BOOGU_ROOT"
  git clone --depth 1 https://github.com/xocialize/boogu-image-mlx "$PKG_DIR"
else
  echo "    已存在，跳过克隆"
fi
python3 -m pip install --quiet -e "$PKG_DIR"

if [ "$SKIP_MODELS" -eq 0 ]; then
  echo "==> 4/4 下载模型（约 10-20GB，视网络而定）"
  command -v huggingface-cli >/dev/null || python3 -m pip install --quiet "huggingface_hub[hf_xet]"
  mkdir -p "$MODEL_DIR"
  huggingface-cli download --local-dir "$MODEL_DIR/Boogu-Image-0.1-Turbo-8bit" mlx-community/Boogu-Image-0.1-Turbo-8bit
  HF_ENDPOINT=https://hf-mirror.com huggingface-cli download --local-dir "$MODEL_DIR/Qwen3-VL-8B-Instruct-abliterated-v2-MLX-4bit" LethalDonkey/Qwen3-VL-8B-Instruct-abliterated-v2-MLX-4bit
else
  echo "==> 4/4 跳过模型下载（--skip-models）"
fi

echo ""
echo "部署完成。启动服务："
echo "  BOOGU_MODEL=$MODEL_DIR/Boogu-Image-0.1-Turbo-8bit \\"
echo "  BOOGU_QWEN=$MODEL_DIR/Qwen3-VL-8B-Instruct-abliterated-v2-MLX-4bit \\"
echo "  python3 scripts/boogu_server.py"
echo ""
echo "验证：curl http://127.0.0.1:8081/v1/models"
