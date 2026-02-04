import re
"""请求上下文中间件

为每个请求设置唯一 ID 和上下文信息。
"""

import time
from typing import Callable
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from ..utils.logging import (
    set_request_id,
    get_request_id,
    generate_request_id,
    get_logger,
    metrics,
)

logger = get_logger(__name__)


class RequestContextMiddleware(BaseHTTPMiddleware):
    """请求上下文中间件

    功能：
    1. 为每个请求生成唯一 ID
    2. 记录请求开始/结束时间
    3. 添加响应头
    4. 记录请求指标
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # 从请求头获取或生成请求 ID
        request_id = (
            request.headers.get("X-Request-ID") or
            request.headers.get("X-Correlation-ID") or
            generate_request_id()
        )

        # 设置请求上下文
        set_request_id(request_id)

        # 记录请求开始
        start_time = time.time()
        method = request.method
        path = request.url.path

        logger.info(f"Request started: {method} {path}")

        try:
            # 处理请求
            response = await call_next(request)

            # 计算耗时
            duration_ms = (time.time() - start_time) * 1000

            # 添加响应头
            response.headers["X-Request-ID"] = request_id
            response.headers["X-Response-Time"] = f"{duration_ms:.2f}ms"

            # 记录指标
            metrics.record_timing("request_duration", duration_ms)
            metrics.increment(f"request_status_{response.status_code}")

            # 记录请求完成
            logger.info(
                f"Request completed: {method} {path} "
                f"status={response.status_code} duration={duration_ms:.2f}ms"
            )

            return response

        except Exception as e:
            # 计算耗时
            duration_ms = (time.time() - start_time) * 1000

            # 记录错误
            logger.error(
                f"Request failed: {method} {path} "
                f"error={type(e).__name__}: {e} duration={duration_ms:.2f}ms"
            )

            # 记录指标
            metrics.record_timing("request_duration", duration_ms)
            metrics.increment("request_errors")

            raise


def get_current_request_id() -> str:
    """获取当前请求 ID

    便捷函数，用于在请求处理过程中获取请求 ID。
    """
    return get_request_id() or "unknown"
