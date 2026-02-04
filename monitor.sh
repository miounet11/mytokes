#!/bin/bash
# AI History Manager - 进程监控和保活脚本
# 功能：监控 workers、自动重启、健康检查、资源监控

set -e

# ==================== 配置 ====================
SERVICE_NAME="ai-history-manager"
PORT=8100
EXPECTED_WORKERS=32
HEALTH_URL="http://127.0.0.1:${PORT}/"
LOG_FILE="/var/log/${SERVICE_NAME}-monitor.log"
PROJECT_DIR="/www/wwwroot/ai-history-manager"
MAX_MEMORY_PERCENT=80
MAX_RESTART_COUNT=5
RESTART_WINDOW=300  # 5分钟内最大重启次数

# 状态文件
STATE_DIR="/var/run/${SERVICE_NAME}"
RESTART_COUNT_FILE="${STATE_DIR}/restart_count"
LAST_RESTART_FILE="${STATE_DIR}/last_restart"

# ==================== 初始化 ====================
mkdir -p "$STATE_DIR"
touch "$LOG_FILE"

# ==================== 函数定义 ====================

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

log_error() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ❌ ERROR: $1" | tee -a "$LOG_FILE"
}

log_warn() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ⚠️ WARN: $1" | tee -a "$LOG_FILE"
}

log_ok() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ✅ $1" | tee -a "$LOG_FILE"
}

# 获取主进程 PID
get_main_pid() {
    pgrep -f "uvicorn api_server:app.*--port $PORT" | head -1
}

# 获取 worker 数量（排除线程，只计算进程）
get_worker_count() {
    local main_pid=$(get_main_pid)
    if [ -n "$main_pid" ]; then
        # 使用 grep -oP 只匹配进程，排除线程 {python3}
        local count=$(pstree -p "$main_pid" 2>/dev/null | grep -oP "python3\(\d+\)" | wc -l)
        # 减去1（主进程本身）
        echo $((count > 0 ? count - 1 : 0))
    else
        echo 0
    fi
}

# 健康检查
health_check() {
    local response=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 "$HEALTH_URL" 2>/dev/null)
    [ "$response" = "200" ]
}

# 检查内存使用
check_memory() {
    local main_pid=$(get_main_pid)
    if [ -n "$main_pid" ]; then
        # 获取所有相关进程的内存使用
        local mem_percent=$(ps -p "$main_pid" -o %mem --no-headers 2>/dev/null | awk '{sum+=$1} END {print int(sum)}')
        echo "${mem_percent:-0}"
    else
        echo 0
    fi
}

# 检查重启频率
check_restart_frequency() {
    local now=$(date +%s)
    local last_restart=$(cat "$LAST_RESTART_FILE" 2>/dev/null || echo 0)
    local restart_count=$(cat "$RESTART_COUNT_FILE" 2>/dev/null || echo 0)

    # 如果超过时间窗口，重置计数
    if [ $((now - last_restart)) -gt $RESTART_WINDOW ]; then
        echo 0 > "$RESTART_COUNT_FILE"
        restart_count=0
    fi

    echo "$restart_count"
}

# 记录重启
record_restart() {
    local count=$(check_restart_frequency)
    echo $((count + 1)) > "$RESTART_COUNT_FILE"
    date +%s > "$LAST_RESTART_FILE"
}

# 重启服务
restart_service() {
    local reason="$1"
    local restart_count=$(check_restart_frequency)

    if [ "$restart_count" -ge "$MAX_RESTART_COUNT" ]; then
        log_error "重启次数过多 ($restart_count/$MAX_RESTART_COUNT in ${RESTART_WINDOW}s)，停止重启"
        log_error "原因: $reason"
        return 1
    fi

    log_warn "正在重启服务... 原因: $reason"
    record_restart

    cd "$PROJECT_DIR"
    bash start.sh >> "$LOG_FILE" 2>&1

    # 等待启动完成
    sleep 10

    if health_check; then
        log_ok "服务重启成功"
        return 0
    else
        log_error "服务重启后健康检查失败"
        return 1
    fi
}

# ==================== 主监控逻辑 ====================

monitor_once() {
    local main_pid=$(get_main_pid)
    local worker_count=$(get_worker_count)
    local mem_usage=$(check_memory)

    # 检查1: 主进程是否存在
    if [ -z "$main_pid" ]; then
        log_error "主进程不存在"
        restart_service "主进程不存在"
        return
    fi

    # 检查2: 健康检查
    if ! health_check; then
        log_error "健康检查失败"
        restart_service "健康检查失败"
        return
    fi

    # 检查3: Worker 数量
    if [ "$worker_count" -lt "$EXPECTED_WORKERS" ]; then
        log_warn "Worker 数量不足: $worker_count/$EXPECTED_WORKERS"
        # 如果 worker 数量低于一半，重启
        if [ "$worker_count" -lt $((EXPECTED_WORKERS / 2)) ]; then
            restart_service "Worker 数量严重不足 ($worker_count/$EXPECTED_WORKERS)"
            return
        fi
    fi

    # 检查4: 内存使用
    if [ "$mem_usage" -gt "$MAX_MEMORY_PERCENT" ]; then
        log_warn "内存使用过高: ${mem_usage}%"
        restart_service "内存使用过高 (${mem_usage}%)"
        return
    fi

    log_ok "服务正常 - PID:$main_pid Workers:$worker_count Mem:${mem_usage}%"
}

# ==================== 命令处理 ====================

case "${1:-monitor}" in
    monitor)
        monitor_once
        ;;
    daemon)
        log "启动守护进程模式..."
        while true; do
            monitor_once
            sleep 30
        done
        ;;
    status)
        main_pid=$(get_main_pid)
        worker_count=$(get_worker_count)
        mem_usage=$(check_memory)

        echo "==================== 服务状态 ===================="
        echo "  主进程 PID: ${main_pid:-无}"
        echo "  Worker 数量: $worker_count/$EXPECTED_WORKERS"
        echo "  内存使用: ${mem_usage}%"
        echo "  健康检查: $(health_check && echo '✅ 通过' || echo '❌ 失败')"
        echo "  重启计数: $(check_restart_frequency)/$MAX_RESTART_COUNT"
        echo "=================================================="
        ;;
    reset)
        echo 0 > "$RESTART_COUNT_FILE"
        log "重启计数已重置"
        ;;
    *)
        echo "用法: $0 {monitor|daemon|status|reset}"
        echo "  monitor - 执行一次监控检查"
        echo "  daemon  - 守护进程模式（持续监控）"
        echo "  status  - 显示服务状态"
        echo "  reset   - 重置重启计数"
        exit 1
        ;;
esac
