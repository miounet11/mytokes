import re
"""错误处理中间件

统一处理应用中的异常，返回标准化错误响应。
"""

import traceback
from typing import Callable
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response, JSONResponse
from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError

from ..utils.exceptions import (
    APIError,
    BadRequestError,
    InternalError,
    create_error_response,
)
from ..utils.logging import get_logger, get_request_id

logger = get_logger(__name__)


class ErrorHandlerMiddleware(BaseHTTPMiddleware):
    """错误处理中间件

    捕获所有未处理的异常，转换为标准化 JSON 响应。
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        try:
            return await call_next(request)
        except Exception as e:
            return self._handle_exception(e, request)

    def _handle_exception(self, exc: Exception, request: Request) -> JSONResponse:
        """处理异常"""
        request_id = get_request_id()

        # 处理自定义 API 错误
        if isinstance(exc, APIError):
            exc.request_id = request_id
            logger.warning(
                f"API error: {exc.code} - {exc.message}",
                exc_info=False
            )
            return exc.to_response()

        # 处理 HTTP 异常
        if isinstance(exc, HTTPException):
            logger.warning(
                f"HTTP error: {exc.status_code} - {exc.detail}",
                exc_info=False
            )
            return create_error_response(
                status_code=exc.status_code,
                error_type="http_error",
                message=str(exc.detail),
                request_id=request_id,
            )

        # 处理验证错误
        if isinstance(exc, RequestValidationError):
            errors = exc.errors()
            message = self._format_validation_errors(errors)
            logger.warning(f"Validation error: {message}")
            return create_error_response(
                status_code=400,
                error_type="validation_error",
                message=message,
                request_id=request_id,
            )

        # 处理未知异常
        logger.error(
            f"Unhandled exception: {type(exc).__name__}: {exc}",
            exc_info=True
        )

        # 生产环境不暴露详细错误信息
        return create_error_response(
            status_code=500,
            error_type="internal_error",
            message="An internal error occurred",
            request_id=request_id,
        )

    def _format_validation_errors(self, errors: list) -> str:
        """格式化验证错误"""
        messages = []
        for error in errors:
            loc = " -> ".join(str(l) for l in error.get("loc", []))
            msg = error.get("msg", "Invalid value")
            messages.append(f"{loc}: {msg}")
        return "; ".join(messages) if messages else "Validation failed"


def setup_exception_handlers(app: FastAPI):
    """设置异常处理器

    为 FastAPI 应用注册全局异常处理器。

    Args:
        app: FastAPI 应用实例
    """

    @app.exception_handler(APIError)
    async def api_error_handler(request: Request, exc: APIError):
        """处理 API 错误"""
        exc.request_id = get_request_id()
        logger.warning(f"API error: {exc.code} - {exc.message}")
        return exc.to_response()

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        """处理 HTTP 异常"""
        return create_error_response(
            status_code=exc.status_code,
            error_type="http_error",
            message=str(exc.detail),
            request_id=get_request_id(),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        """处理验证错误"""
        errors = exc.errors()
        messages = []
        for error in errors:
            loc = " -> ".join(str(l) for l in error.get("loc", []))
            msg = error.get("msg", "Invalid value")
            messages.append(f"{loc}: {msg}")

        return create_error_response(
            status_code=400,
            error_type="validation_error",
            message="; ".join(messages) if messages else "Validation failed",
            request_id=get_request_id(),
        )

    @app.exception_handler(Exception)
    async def general_exception_handler(request: Request, exc: Exception):
        """处理未知异常"""
        logger.error(
            f"Unhandled exception: {type(exc).__name__}: {exc}",
            exc_info=True
        )
        return create_error_response(
            status_code=500,
            error_type="internal_error",
            message="An internal error occurred",
            request_id=get_request_id(),
        )
