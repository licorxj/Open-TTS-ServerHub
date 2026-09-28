#!/usr/bin/env bash
set -euo pipefail

echo "============================================"
echo "  install_flash_attn (Linux / macOS)"
echo "============================================"

# 可选环境变量覆盖:
#   FLASH_ATTN_VERSION=2.8.3   指定版本
#   PYTHON=/path/to/python      指定解释器
PY="${PYTHON:-$(command -v python3 || command -v python)}"

# 检测 pip
if "$PY" -m pip --version >/dev/null 2>&1; then
  PIP="$PY -m pip"
else
  PIP="$(command -v pip3 || command -v pip)"
fi

echo "使用 Python: $("$PY" --version 2>&1)"
echo "使用 Pip:    $PIP"

# 若脚本同目录的 whl/ 下存在本地 flash_attn whl，则离线安装（与 Windows 行为一致）
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCAL_WHL="$(ls "$SCRIPT_DIR"/whl/flash_attn*.whl 2>/dev/null | head -n1 || true)"

if [ -n "$LOCAL_WHL" ]; then
  echo "检测到本地 whl: $LOCAL_WHL，离线安装..."
  $PIP install --no-index --find-links "$SCRIPT_DIR/whl" "$LOCAL_WHL"
else
  echo "未找到本地 whl，在线安装 flash-attn${FLASH_ATTN_VERSION:+==$FLASH_ATTN_VERSION}"
  # --no-build-isolation: 复用已安装的 torch，避免重复下载/构建依赖
  if [ -n "${FLASH_ATTN_VERSION:-}" ]; then
    $PIP install "flash-attn==$FLASH_ATTN_VERSION" --no-build-isolation
  else
    $PIP install flash-attn --no-build-isolation
  fi
fi

echo
echo "验证安装..."
"$PY" -c "import flash_attn; print('[OK] flash_attn', flash_attn.__version__)"
echo "完成。"
