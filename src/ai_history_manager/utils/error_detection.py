"""错误检测工具

提供内容长度超限错误的检测功能。
"""

from enum import Enum
from typing import Optional


class ErrorType(str, Enum):
    """错误类型枚举"""

    CONTENT_TOO_LONG = "content_too_long"
    RATE_LIMITED = "rate_limited"
    AUTH_FAILED = "auth_failed"
    SERVICE_UNAVAILABLE = "service_unavailable"
    UNKNOWN = "unknown"


def is_content_length_error(status_code: int, error_text: str) -> bool:
    """检查是否为内容长度超限错误

    支持多种 API 的错误格式：
    - Kiro: CONTENT_LENGTH_EXCEEDS_THRESHOLD
    - OpenAI: context_length_exceeded
    - Anthropic: input is too long

    Args:
        status_code: HTTP 状态码
        error_text: 错误响应文本

    Returns:
        是否为内容长度超限错误
    """
    if not error_text:
        return False

    # Kiro API 错误
    if "CONTENT_LENGTH_EXCEEDS_THRESHOLD" in error_text:
        return True

    # 通用长度超限关键词
    if "Input is too long" in error_text:
        return True

    # OpenAI 风格错误
    if "context_length_exceeded" in error_text:
        return True

    if "maximum context length" in error_text.lower():
        return True

    # 更宽松的匹配
    lowered = error_text.lower()
    if "too long" in lowered and (
        "input" in lowered
        or "content" in lowered
        or "message" in lowered
        or "context" in lowered
    ):
        return True

    # Token 超限
    if "token" in lowered and ("limit" in lowered or "exceed" in lowered):
        return True

    return False


def classify_error(status_code: int, error_text: str) -> ErrorType:
    """分类错误类型

    Args:
        status_code: HTTP 状态码
        error_text: 错误响应文本

    Returns:
        错误类型
    """
    if is_content_length_error(status_code, error_text):
        return ErrorType.CONTENT_TOO_LONG

    if status_code == 429:
        return ErrorType.RATE_LIMITED

    if status_code in (401, 403):
        return ErrorType.AUTH_FAILED

    if status_code in (500, 502, 503, 504):
        return ErrorType.SERVICE_UNAVAILABLE

    return ErrorType.UNKNOWN


def should_retry_on_error(error_type: ErrorType) -> bool:
    """判断是否应该重试

    Args:
        error_type: 错误类型

    Returns:
        是否应该重试
    """
    return error_type in (
        ErrorType.CONTENT_TOO_LONG,
        ErrorType.RATE_LIMITED,
        ErrorType.SERVICE_UNAVAILABLE,
    )
