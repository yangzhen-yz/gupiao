#!/bin/bash
# macOS 一键启动脚本（双击 .command 文件即可运行）

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CLIENT_DIR="$SCRIPT_DIR/../client-vue"

echo "[1/4] Building frontend..."
cd "$CLIENT_DIR"

if [ ! -d "node_modules" ]; then
    echo "Installing frontend dependencies..."
    pnpm install
fi

pnpm run build
if [ $? -ne 0 ]; then
    echo "Frontend build failed!"
    read -p "Press Enter to exit..."
    exit 1
fi

echo ""
echo "[2/4] Checking Python..."
python3 --version
if [ $? -ne 0 ]; then
    echo "Python3 not found, trying python..."
    python --version
fi

echo ""
echo "[3/4] Installing Python dependencies..."
pip3 install -r "$SCRIPT_DIR/requirements.txt" -i https://pypi.tuna.tsinghua.edu.cn/simple 2>/dev/null || \
pip install -r "$SCRIPT_DIR/requirements.txt" -i https://pypi.tuna.tsinghua.edu.cn/simple 2>/dev/null || \
pip3 install -r "$SCRIPT_DIR/requirements.txt"

echo ""
echo "[4/4] Starting FastAPI..."
echo "  http://localhost:3003"
echo "  API docs: http://localhost:3003/docs"
echo ""

cd "$SCRIPT_DIR"
python3 run.py 2>/dev/null || python run.py
