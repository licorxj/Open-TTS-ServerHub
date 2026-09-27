#!/bin/bash

# ===========================================
# OmniVoice API Server 启动脚本 (Linux/Mac)
# ===========================================

set -e

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# 打印带颜色的信息
info() {
    echo -e "${GREEN}[信息]${NC} $1"
}

warn() {
    echo -e "${YELLOW}[警告]${NC} $1"
}

error() {
    echo -e "${RED}[错误]${NC} $1"
}

# 获取脚本所在目录
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

echo "=========================================="
echo "   OmniVoice API Server 启动器"
echo "=========================================="
echo

# 检查 Python
if ! command -v python3 &> /dev/null; then
    error "未找到 Python3，请确保 Python 已安装"
    exit 1
fi

PYTHON_CMD="python3"

# 检查虚拟环境
if [ -d "py312" ] && [ -f "py312/bin/activate" ]; then
    info "检测到虚拟环境，正在激活..."
    source "py312/bin/activate"
    PYTHON_CMD="python"
elif [ -d ".venv" ] && [ -f ".venv/bin/activate" ]; then
    info "检测到虚拟环境，正在激活..."
    source ".venv/bin/activate"
    PYTHON_CMD="python"
else
    warn "未检测到虚拟环境，使用系统 Python"
fi

# 检查依赖
info "检查依赖..."
if ! $PYTHON_CMD -c "import fastapi" 2>/dev/null; then
    info "正在安装 API 依赖..."
    pip install -r requirements_api.txt
fi

# 默认参数
HOST="0.0.0.0"
PORT="8000"
DEVICE=""
MODEL="models"
MAX_WORKERS="4"

# 解析命令行参数
while [[ $# -gt 0 ]]; do
    case $1 in
        --host)
            HOST="$2"
            shift 2
            ;;
        --port)
            PORT="$2"
            shift 2
            ;;
        --device)
            DEVICE="$2"
            shift 2
            ;;
        --model)
            MODEL="$2"
            shift 2
            ;;
        --max-workers)
            MAX_WORKERS="$2"
            shift 2
            ;;
        --help|-h)
            echo "用法: $0 [选项]"
            echo ""
            echo "选项:"
            echo "  --host HOST          服务器地址 (默认: 0.0.0.0)"
            echo "  --port PORT          服务器端口 (默认: 8000)"
            echo "  --device DEVICE      计算设备 (cuda/cpu/mps, 默认: 自动检测)"
            echo "  --model PATH         模型路径 (默认: models)"
            echo "  --max-workers N      最大工作线程数 (默认: 4)"
            echo "  --help, -h           显示此帮助信息"
            exit 0
            ;;
        *)
            warn "未知参数: $1"
            shift
            ;;
    esac
done

echo "=========================================="
echo "   启动参数"
echo "=========================================="
echo " 服务地址: $HOST"
echo " 服务端口: $PORT"
echo " 计算设备: ${DEVICE:-auto (自动检测)}"
echo " 模型路径: $MODEL"
echo " 工作线程: $MAX_WORKERS"
echo "=========================================="
echo
echo -e " ${BLUE}API 文档地址: http://localhost:$PORT/docs${NC}"
echo

# 构建命令
CMD="$PYTHON_CMD api_server.py --host $HOST --port $PORT --model $MODEL --max-workers $MAX_WORKERS"
if [ -n "$DEVICE" ]; then
    CMD="$CMD --device $DEVICE"
fi

info "启动 API 服务器..."
info "命令: $CMD"
echo

# 启动服务器
exec $CMD
