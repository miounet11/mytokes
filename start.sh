#!/bin/bash
# AI History Manager - 高性能启动脚本

# 配置
PORT=${PORT:-8100}
WORKERS=${WORKERS:-4}
LOG_FILE="/var/log/ai-history-manager.log"

# 检测 CPU 核心数，自动设置 workers
if [ "$WORKERS" = "auto" ]; then
    WORKERS=$(nproc)
    echo "自动检测 CPU 核心数: $WORKERS"
fi

# 停止旧进程
echo "停止旧进程..."
pkill -f "uvicorn api_server:app.*--port $PORT" 2>/dev/null
sleep 1

# 切换到项目目录
cd /www/wwwroot/ai-history-manager

# 检查 uvloop 和 httptools 是否安装
python3 -c "import uvloop" 2>/dev/null
if [ $? -ne 0 ]; then
    echo "安装 uvloop..."
    pip install uvloop -q
fi

python3 -c "import httptools" 2>/dev/null
if [ $? -ne 0 ]; then
    echo "安装 httptools..."
    pip install httptools -q
fi

# 启动服务
echo "启动 AI History Manager..."
echo "  端口: $PORT"
echo "  Workers: $WORKERS"
echo "  日志: $LOG_FILE"

nohup uvicorn api_server:app \
    --host 0.0.0.0 \
    --port $PORT \
    --workers $WORKERS \
    --loop uvloop \
    --http httptools \
    --no-access-log \
    >> "$LOG_FILE" 2>&1 &

sleep 2

# 验证启动
if pgrep -f "uvicorn api_server:app.*--port $PORT" > /dev/null; then
    echo "✓ 服务启动成功"
    echo "  PID: $(pgrep -f "uvicorn api_server:app.*--port $PORT" | head -1)"

    # 健康检查
    curl -s http://127.0.0.1:$PORT/ > /dev/null 2>&1
    if [ $? -eq 0 ]; then
        echo "✓ 健康检查通过"
    else
        echo "⚠ 健康检查失败，请查看日志"
    fi
else
    echo "✗ 服务启动失败，请查看日志: $LOG_FILE"
    tail -20 "$LOG_FILE"
    exit 1
fi
