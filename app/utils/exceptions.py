"""统一异常处理模块

定义应用级异常类层次结构，提供结构化错误处理。
"""

from typing import Optional, Any
from fastapi import HTTPException
from fastapi.responses import JSONResponse


class APIError(Exception):
    """API 错误基类

    所有应用级异常都应继承此类。
    """

    def __init__(
        self,
        message: str,
        code: str = "api_error",
        status_code: int = 500,
        details: Optional[dict[str, Any]] = None,
        request_id: Optional[str] = None,
    ):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code
        self.details = details or {}
        self.request_id = request_id

    def to_dict(self) -> dict:
        """转换为字典格式"""
        result = {
            "type": "error",
            "error": {
                "type": self.code,
                "message": self.message,
            }
        }
        if self.details:
            result["error"]["details"] = self.details
        if self.request_id:
            result["error"]["request_id"] = self.request_id
        return result

    def to_response(self) -> JSONResponse:
        """转换为 JSON 响应"""
        return JSONResponse(
            status_code=self.status_code,
            content=self.to_dict()
        )


# ==================== 客户端错误 (4xx) ====================

class BadRequestError(APIError):
    """请求格式错误 (400)"""

    def __init__(self, message: str = "Bad request", **kwargs):
        super().__init__(message, code="bad_request", status_code=400, **kwargs)


class ValidationError(APIError):
    """请求验证失败 (400)"""

    def __init__(self, message: str = "Validation failed", field: Optional[str] = None, **kwargs):
        details = kwargs.pop("details", {})
        if field:
            details["field"] = field
        super().__init__(message, code="validation_error", status_code=400, details=details, **kwargs)


class AuthenticationError(APIError):
    """认证失败 (401)"""

    def __init__(self, message: str = "Authentication required", **kwargs):
        super().__init__(message, code="authentication_error", status_code=401, **kwargs)


class PermissionError(APIError):
    """权限不足 (403)"""

    def __init__(self, message: str = "Permission denied", **kwargs):
        super().__init__(message, code="permission_error", status_code=403, **kwargs)


class NotFoundError(APIError):
    """资源不存在 (404)"""

    def __init__(self, message: str = "Resource not found", resource: Optional[str] = None, **kwargs):
        details = kwargs.pop("details", {})
        if resource:
            details["resource"] = resource
        super().__init__(message, code="not_found", status_code=404, details=details, **kwargs)


class RateLimitError(APIError):
    """请求频率超限 (429)"""

    def __init__(
        self,
        message: str = "Rate limit exceeded",
        retry_after: Optional[int] = None,
        **kwargs
    ):
        details = kwargs.pop("details", {})
        if retry_after:
            details["retry_after"] = retry_after
        super().__init__(message, code="rate_limit_error", status_code=429, details=details, **kwargs)


class RequestTooLargeError(APIError):
    """请求体过大 (413)"""

    def __init__(
        self,
        message: str = "Request too large",
        max_size: Optional[int] = None,
        **kwargs
    ):
        details = kwargs.pop("details", {})
        if max_size:
            details["max_size"] = max_size
        super().__init__(message, code="request_too_large", status_code=413, details=details, **kwargs)


# ==================== 服务端错误 (5xx) ====================

class InternalError(APIError):
    """内部服务器错误 (500)"""

    def __init__(self, message: str = "Internal server error", **kwargs):
        super().__init__(message, code="internal_error", status_code=500, **kwargs)


class ServiceUnavailableError(APIError):
    """服务不可用 (503)"""

    def __init__(self, message: str = "Service unavailable", **kwargs):
        super().__init__(message, code="service_unavailable", status_code=503, **kwargs)


class GatewayTimeoutError(APIError):
    """网关超时 (504)"""

    def __init__(self, message: str = "Gateway timeout", **kwargs):
        super().__init__(message, code="gateway_timeout", status_code=504, **kwargs)


# ==================== 业务错误 ====================

class ModelNotFoundError(APIError):
    """模型不存在"""

    def __init__(self, model: str, **kwargs):
        super().__init__(
            f"Model '{model}' not found",
            code="model_not_found",
            status_code=404,
            details={"model": model},
            **kwargs
        )


class ContextLengthExceededError(APIError):
    """上下文长度超限"""

    def __init__(
        self,
        message: str = "Context length exceeded",
        max_tokens: Optional[int] = None,
        current_tokens: Optional[int] = None,
        **kwargs
    ):
        details = kwargs.pop("details", {})
        if max_tokens:
            details["max_tokens"] = max_tokens
        if current_tokens:
            details["current_tokens"] = current_tokens
        super().__init__(
            message,
            code="context_length_exceeded",
            status_code=400,
            details=details,
            **kwargs
        )


class ToolCallError(APIError):
    """工具调用错误"""

    def __init__(
        self,
        message: str = "Tool call failed",
        tool_name: Optional[str] = None,
        **kwargs
    ):
        details = kwargs.pop("details", {})
        if tool_name:
            details["tool_name"] = tool_name
        super().__init__(
            message,
            code="tool_call_error",
            status_code=400,
            details=details,
            **kwargs
        )


class StreamError(APIError):
    """流式响应错误"""

    def __init__(self, message: str = "Stream error", **kwargs):
        super().__init__(message, code="stream_error", status_code=500, **kwargs)


class ContinuationError(APIError):
    """续传错误"""

    def __init__(
        self,
        message: str = "Continuation failed",
        continuation_count: int = 0,
        **kwargs
    ):
        details = kwargs.pop("details", {})
        details["continuation_count"] = continuation_count
        super().__init__(
            message,
            code="continuation_error",
            status_code=500,
            details=details,
            **kwargs
        )


class UpstreamError(APIError):
    """上游服务错误"""

    def __init__(
        self,
        message: str = "Upstream service error",
        upstream_status: Optional[int] = None,
        upstream_message: Optional[str] = None,
        **kwargs
    ):
        details = kwargs.pop("details", {})
        if upstream_status:
            details["upstream_status"] = upstream_status
        if upstream_message:
            details["upstream_message"] = upstream_message
        super().__init__(
            message,
            code="upstream_error",
            status_code=502,
            details=details,
            **kwargs
        )


# ==================== 异常处理器 ====================

def create_error_response(
    status_code: int,
    error_type: str,
    message: str,
    request_id: Optional[str] = None,
) -> JSONResponse:
    """创建标准错误响应"""
    content = {
        "type": "error",
        "error": {
            "type": error_type,
            "message": message,
        }
    }
    if request_id:
        content["error"]["request_id"] = request_id
    return JSONResponse(status_code=status_code, content=content)


def http_exception_to_api_error(exc: HTTPException, request_id: Optional[str] = None) -> APIError:
    """将 HTTPException 转换为 APIError"""
    status_map = {
        400: BadRequestError,
        401: AuthenticationError,
        403: PermissionError,
        404: NotFoundError,
        413: RequestTooLargeError,
        429: RateLimitError,
        500: InternalError,
        503: ServiceUnavailableError,
        504: GatewayTimeoutError,
    }

    error_class = status_map.get(exc.status_code, APIError)
    return error_class(
        message=str(exc.detail),
        request_id=request_id,
    )
