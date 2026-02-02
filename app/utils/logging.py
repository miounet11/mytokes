"""结构化日志模块

提供带请求追踪的结构化日志功能。
"""

import logging
import json
import sys
import time
import uuid
from typing import Optional, Any
from contextvars import ContextVar
from functools import wraps
from datetime import datetime


# 请求上下文变量
request_id_var: ContextVar[Optional[str]] = ContextVar("request_id", default=None)
request_start_time_var: ContextVar[Optional[float]] = ContextVar("request_start_time", default=None)


def generate_request_id() -> str:
    """生成请求 ID"""
    return f"req_{uuid.uuid4().hex[:16]}"


def get_request_id() -> Optional[str]:
    """获取当前请求 ID"""
    return request_id_var.get()


def set_request_id(request_id: Optional[str] = None) -> str:
    """设置请求 ID"""
    rid = request_id or generate_request_id()
    request_id_var.set(rid)
    request_start_time_var.set(time.time())
    return rid


def get_request_duration() -> Optional[float]:
    """获取请求持续时间(毫秒)"""
    start_time = request_start_time_var.get()
    if start_time:
        return (time.time() - start_time) * 1000
    return None


class StructuredFormatter(logging.Formatter):
    """结构化日志格式化器

    输出 JSON 格式的日志，便于日志聚合和分析。
    """

    def __init__(self, service_name: str = "ai-history-manager"):
        super().__init__()
        self.service_name = service_name

    def format(self, record: logging.LogRecord) -> str:
        log_data = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "service": self.service_name,
        }

        # 添加请求上下文
        request_id = get_request_id()
        if request_id:
            log_data["request_id"] = request_id

        duration = get_request_duration()
        if duration:
            log_data["duration_ms"] = round(duration, 2)

        # 添加位置信息
        log_data["location"] = {
            "file": record.filename,
            "line": record.lineno,
            "function": record.funcName,
        }

        # 添加异常信息
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)

        # 添加额外字段
        if hasattr(record, "extra_data"):
            log_data["data"] = record.extra_data

        return json.dumps(log_data, ensure_ascii=False, default=str)


class ConsoleFormatter(logging.Formatter):
    """控制台友好的日志格式化器"""

    COLORS = {
        "DEBUG": "\033[36m",     # 青色
        "INFO": "\033[32m",      # 绿色
        "WARNING": "\033[33m",   # 黄色
        "ERROR": "\033[31m",     # 红色
        "CRITICAL": "\033[35m",  # 紫色
    }
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        color = self.COLORS.get(record.levelname, "")
        reset = self.RESET

        # 时间戳
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

        # 请求 ID
        request_id = get_request_id()
        rid_str = f"[{request_id[:12]}]" if request_id else ""

        # 持续时间
        duration = get_request_duration()
        dur_str = f" ({duration:.0f}ms)" if duration else ""

        # 格式化消息
        msg = f"{timestamp} {color}{record.levelname:8}{reset} {rid_str} {record.name}: {record.getMessage()}{dur_str}"

        # 添加异常信息
        if record.exc_info:
            msg += "\n" + self.formatException(record.exc_info)

        return msg


class ContextLogger(logging.LoggerAdapter):
    """带上下文的日志适配器"""

    def process(self, msg: str, kwargs: dict) -> tuple[str, dict]:
        # 添加额外数据
        extra = kwargs.get("extra", {})
        if "extra_data" not in extra:
            extra["extra_data"] = {}

        # 合并上下文数据
        if hasattr(self, "context"):
            extra["extra_data"].update(self.context)

        kwargs["extra"] = extra
        return msg, kwargs

    def with_context(self, **context) -> "ContextLogger":
        """创建带额外上下文的日志器"""
        new_logger = ContextLogger(self.logger, self.extra)
        new_logger.context = {**getattr(self, "context", {}), **context}
        return new_logger


def setup_logging(
    level: str = "INFO",
    json_format: bool = False,
    service_name: str = "ai-history-manager",
) -> logging.Logger:
    """配置日志系统

    Args:
        level: 日志级别
        json_format: 是否使用 JSON 格式
        service_name: 服务名称

    Returns:
        配置好的根日志器
    """
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    # 清除现有处理器
    root_logger.handlers.clear()

    # 创建控制台处理器
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.DEBUG)

    if json_format:
        console_handler.setFormatter(StructuredFormatter(service_name))
    else:
        console_handler.setFormatter(ConsoleFormatter())

    root_logger.addHandler(console_handler)

    # 降低第三方库的日志级别
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("uvicorn").setLevel(logging.INFO)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    return root_logger


def get_logger(name: str) -> ContextLogger:
    """获取带上下文的日志器

    Args:
        name: 日志器名称

    Returns:
        ContextLogger 实例
    """
    return ContextLogger(logging.getLogger(name), {})


# ==================== 日志装饰器 ====================

def log_function_call(logger: Optional[logging.Logger] = None):
    """记录函数调用的装饰器"""
    def decorator(func):
        nonlocal logger
        if logger is None:
            logger = logging.getLogger(func.__module__)

        @wraps(func)
        async def async_wrapper(*args, **kwargs):
            func_name = func.__name__
            logger.debug(f"Calling {func_name}")
            start_time = time.time()
            try:
                result = await func(*args, **kwargs)
                duration = (time.time() - start_time) * 1000
                logger.debug(f"{func_name} completed in {duration:.2f}ms")
                return result
            except Exception as e:
                duration = (time.time() - start_time) * 1000
                logger.error(f"{func_name} failed after {duration:.2f}ms: {e}")
                raise

        @wraps(func)
        def sync_wrapper(*args, **kwargs):
            func_name = func.__name__
            logger.debug(f"Calling {func_name}")
            start_time = time.time()
            try:
                result = func(*args, **kwargs)
                duration = (time.time() - start_time) * 1000
                logger.debug(f"{func_name} completed in {duration:.2f}ms")
                return result
            except Exception as e:
                duration = (time.time() - start_time) * 1000
                logger.error(f"{func_name} failed after {duration:.2f}ms: {e}")
                raise

        import asyncio
        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        return sync_wrapper

    return decorator


# ==================== 性能指标 ====================

class MetricsCollector:
    """简单的性能指标收集器"""

    def __init__(self):
        self._metrics: dict[str, list[float]] = {}
        self._counters: dict[str, int] = {}

    def record_timing(self, name: str, duration_ms: float):
        """记录时间指标"""
        if name not in self._metrics:
            self._metrics[name] = []
        self._metrics[name].append(duration_ms)
        # 保留最近 1000 个样本
        if len(self._metrics[name]) > 1000:
            self._metrics[name] = self._metrics[name][-1000:]

    def increment(self, name: str, value: int = 1):
        """增加计数器"""
        self._counters[name] = self._counters.get(name, 0) + value

    def get_stats(self, name: str) -> dict[str, float]:
        """获取指标统计"""
        values = self._metrics.get(name, [])
        if not values:
            return {}

        sorted_values = sorted(values)
        n = len(sorted_values)

        return {
            "count": n,
            "min": sorted_values[0],
            "max": sorted_values[-1],
            "avg": sum(values) / n,
            "p50": sorted_values[n // 2],
            "p95": sorted_values[int(n * 0.95)] if n >= 20 else sorted_values[-1],
            "p99": sorted_values[int(n * 0.99)] if n >= 100 else sorted_values[-1],
        }

    def get_counter(self, name: str) -> int:
        """获取计数器值"""
        return self._counters.get(name, 0)

    def get_all_stats(self) -> dict:
        """获取所有指标"""
        return {
            "timings": {name: self.get_stats(name) for name in self._metrics},
            "counters": dict(self._counters),
        }

    def reset(self):
        """重置所有指标"""
        self._metrics.clear()
        self._counters.clear()


# 全局指标收集器
metrics = MetricsCollector()
