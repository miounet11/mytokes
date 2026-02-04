#!/bin/bash
# AI History Manager - 高性能启动脚本
# 支持上下文增强、智能摘要、接续机制等功能

set -e  # 遇到错误立即退出

# ==================== 配置 ====================
PORT=${PORT:-8100}
WORKERS=${WORKERS:-32}  # 火力全开：32 workers
LOG_FILE="/var/log/ai-history-manager.log"
PID_FILE="/var/run/ai-history-manager.pid"
PROJECT_DIR="/www/wwwroot/ai-history-manager"

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# ==================== 函数定义 ====================

log_info() {
    echo -e "${GREEN}✓${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}⚠${NC} $1"
}

log_error() {
    echo -e "${RED}✗${NC} $1"
}

log_step() {
    echo -e "${BLUE}→${NC} $1"
}

# 检测 CPU 核心数
get_optimal_workers() {
    local cores=$(nproc 2>/dev/null || echo 4)
    # 推荐 workers = CPU核心数，但不超过8
    if [ "$cores" -gt 8 ]; then
        echo 8
    else
        echo "$cores"
    fi
}

# 强制停止进程（包括子进程）
force_stop() {
    log_step "停止旧进程..."

    # 方法1: 通过端口查找并杀死
    local pids=$(lsof -ti :$PORT 2>/dev/null || true)
    if [ -n "$pids" ]; then
        echo "$pids" | xargs kill -9 2>/dev/null || true
        sleep 1
    fi

    # 方法2: 通过进程名查找
    pkill -9 -f "uvicorn api_server:app.*--port $PORT" 2>/dev/null || true

    # 方法3: 通过 PID 文件
    if [ -f "$PID_FILE" ]; then
        local old_pid=$(cat "$PID_FILE" 2>/dev/null)
        if [ -n "$old_pid" ]; then
            kill -9 "$old_pid" 2>/dev/null || true
        fi
        rm -f "$PID_FILE"
    fi

    # 等待端口释放
    local wait_count=0
    while lsof -ti :$PORT >/dev/null 2>&1; do
        sleep 0.5
        wait_count=$((wait_count + 1))
        if [ $wait_count -gt 10 ]; then
            log_error "端口 $PORT 无法释放"
            exit 1
        fi
    done

    log_info "旧进程已停止"
}

# 检查依赖
check_dependencies() {
    log_step "检查依赖..."

    local missing_deps=()

    # 检查 Python 模块
    for module in uvloop httptools httpx fastapi pydantic; do
        if ! python3 -c "import $module" 2>/dev/null; then
            missing_deps+=("$module")
        fi
    done

    if [ ${#missing_deps[@]} -gt 0 ]; then
        log_warn "安装缺失依赖: ${missing_deps[*]}"
        pip install -q "${missing_deps[@]}"
    fi

    # 检查本地模块
    if ! python3 -c "from ai_history_manager import HistoryManager" 2>/dev/null; then
        log_warn "安装 ai_history_manager 模块..."
        pip install -e "$PROJECT_DIR" -q
    fi

    log_info "依赖检查完成"
}

# 验证配置文件
check_config() {
    log_step "验证配置..."

    # 检查 .env 文件
    if [ -f "$PROJECT_DIR/.env" ]; then
        log_info "加载环境变量: $PROJECT_DIR/.env"
        set -a
        source "$PROJECT_DIR/.env"
        set +a
    fi

    # 检查配置目录
    if [ -d "$PROJECT_DIR/config" ]; then
        log_info "配置目录: $PROJECT_DIR/config"
    fi
}

# 启动服务
start_server() {
    log_step "启动 AI History Manager..."

    # 自动设置 workers
    if [ "$WORKERS" = "auto" ]; then
        WORKERS=$(get_optimal_workers)
        log_info "自动检测 Workers: $WORKERS"
    fi

    echo "  端口: $PORT"
    echo "  Workers: $WORKERS"
    echo "  日志: $LOG_FILE"
    echo "  PID: $PID_FILE"

    # 确保日志目录存在
    mkdir -p "$(dirname "$LOG_FILE")"
    touch "$LOG_FILE"

    # 启动 uvicorn
    cd "$PROJECT_DIR"
    nohup uvicorn api_server:app \
        --host 0.0.0.0 \
        --port $PORT \
        --workers $WORKERS \
        --loop uvloop \
        --http httptools \
        --no-access-log \
        >> "$LOG_FILE" 2>&1 &

    local pid=$!
    echo $pid > "$PID_FILE"

    # 等待启动
    sleep 2
}

# 健康检查
health_check() {
    log_step "执行健康检查..."

    local max_retries=5
    local retry=0

    while [ $retry -lt $max_retries ]; do
        # 检查进程是否存在
        if ! pgrep -f "uvicorn api_server:app.*--port $PORT" > /dev/null; then
            log_error "服务进程不存在"
            tail -30 "$LOG_FILE"
            exit 1
        fi

        # HTTP 健康检查
        local response=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:$PORT/" 2>/dev/null || echo "000")

        if [ "$response" = "200" ]; then
            log_info "健康检查通过 (HTTP $response)"
            return 0
        fi

        retry=$((retry + 1))
        log_warn "健康检查重试 ($retry/$max_retries)..."
        sleep 1
    done

    log_error "健康检查失败"
    tail -20 "$LOG_FILE"
    exit 1
}

# 显示状态
show_status() {
    echo ""
    echo "==================== 服务状态 ===================="

    local main_pid=$(pgrep -f "uvicorn api_server:app.*--port $PORT" | head -1)
    echo "  主进程 PID: $main_pid"

    local worker_count=$(pgrep -f "uvicorn api_server:app" | wc -l)
    echo "  Worker 进程: $worker_count"

    # 显示功能状态
    echo ""
    echo "==================== 功能配置 ===================="
    echo "  上下文增强: ${CONTEXT_ENHANCEMENT_ENABLED:-true}"
    echo "  智能摘要: 启用"
    echo "  接续机制: 启用"
    echo "  同角色合并: ${ANTHROPIC_MERGE_SAME_ROLE_ENABLED:-true}"

    echo ""
    echo "==================== 访问地址 ===================="
    echo "  本地: http://127.0.0.1:$PORT"
    echo "  API: http://127.0.0.1:$PORT/v1/chat/completions"
    echo "  模型: http://127.0.0.1:$PORT/v1/models"
    echo "================================================="
}

# 显示帮助
show_help() {
    echo "AI History Manager 启动脚本"
    echo ""
    echo "用法: $0 [命令]"
    echo ""
    echo "命令:"
    echo "  start     启动服务 (默认)"
    echo "  stop      停止服务"
    echo "  restart   重启服务"
    echo "  status    查看状态"
    echo "  logs      查看日志"
    echo "  help      显示帮助"
    echo ""
    echo "环境变量:"
    echo "  PORT      服务端口 (默认: 8100)"
    echo "  WORKERS   Worker 数量 (默认: 4, 可设为 auto)"
    echo ""
    echo "示例:"
    echo "  $0                    # 启动服务"
    echo "  $0 restart            # 重启服务"
    echo "  PORT=8200 $0          # 使用端口 8200 启动"
    echo "  WORKERS=auto $0       # 自动检测 Worker 数量"
}

# 查看日志
show_logs() {
    if [ -f "$LOG_FILE" ]; then
        tail -50 "$LOG_FILE"
    else
        log_error "日志文件不存在: $LOG_FILE"
    fi
}

# 查看状态
check_status() {
    if pgrep -f "uvicorn api_server:app.*--port $PORT" > /dev/null; then
        log_info "服务运行中"
        show_status
    else
        log_warn "服务未运行"
        exit 1
    fi
}

# ==================== 主逻辑 ====================

case "${1:-start}" in
    start)
        echo "==================== AI History Manager ===================="
        force_stop
        check_dependencies
        check_config
        start_server
        health_check
        show_status
        log_info "服务启动完成!"
        ;;
    stop)
        force_stop
        log_info "服务已停止"
        ;;
    restart)
        echo "==================== 重启服务 ===================="
        force_stop
        check_config
        start_server
        health_check
        show_status
        log_info "服务重启完成!"
        ;;
    status)
        check_status
        ;;
    logs)
        show_logs
        ;;
    help|--help|-h)
        show_help
        ;;
    *)
        log_error "未知命令: $1"
        show_help
        exit 1
        ;;
esac
