import re
"""速率限制中间件

基于令牌桶算法实现请求速率限制。
"""

import time
import asyncio
from typing import Callable, Optional
from collections import defaultdict
from dataclasses import dataclass, field
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response, JSONResponse

from ..config import get_settings
from ..utils.logging import get_logger
from ..utils.exceptions import RateLimitError

logger = get_logger(__name__)


@dataclass
class TokenBucket:
    """令牌桶"""
    capacity: float  # 桶容量
    tokens: float  # 当前令牌数
    refill_rate: float  # 每秒补充令牌数
    last_refill: float = field(default_factory=time.time)  # 上次补充时间

    def consume(self, tokens: float = 1.0) -> bool:
        """消费令牌

        Args:
            tokens: 需要消费的令牌数

        Returns:
            是否成功消费
        """
        self._refill()

        if self.tokens >= tokens:
            self.tokens -= tokens
            return True
        return False

    def _refill(self):
        """补充令牌"""
        now = time.time()
        elapsed = now - self.last_refill
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
        self.last_refill = now

    def get_wait_time(self, tokens: float = 1.0) -> float:
        """获取等待时间

        Args:
            tokens: 需要的令牌数

        Returns:
            需要等待的秒数
        """
        self._refill()

        if self.tokens >= tokens:
            return 0.0

        needed = tokens - self.tokens
        return needed / self.refill_rate


class RateLimiter:
    """速率限制器

    支持多种限制策略：
    1. 全局限制
    2. 按 IP 限制
    3. 按 API Key 限制
    """

    def __init__(
        self,
        requests_per_second: float = 10.0,
        burst_size: float = 20.0,
        cleanup_interval: float = 300.0,
    ):
        self.requests_per_second = requests_per_second
        self.burst_size = burst_size
        self.cleanup_interval = cleanup_interval

        # 按键存储令牌桶
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = asyncio.Lock()
        self._last_cleanup = time.time()

    async def is_allowed(self, key: str, tokens: float = 1.0) -> bool:
        """检查请求是否允许

        Args:
            key: 限制键（如 IP 地址或 API Key）
            tokens: 需要消费的令牌数

        Returns:
            是否允许
        """
        async with self._lock:
            # 定期清理过期桶
            await self._cleanup_if_needed()

            # 获取或创建令牌桶
            bucket = self._get_or_create_bucket(key)

            return bucket.consume(tokens)

    async def get_wait_time(self, key: str, tokens: float = 1.0) -> float:
        """获取等待时间

        Args:
            key: 限制键
            tokens: 需要的令牌数

        Returns:
            需要等待的秒数
        """
        async with self._lock:
            bucket = self._get_or_create_bucket(key)
            return bucket.get_wait_time(tokens)

    def _get_or_create_bucket(self, key: str) -> TokenBucket:
        """获取或创建令牌桶"""
        if key not in self._buckets:
            self._buckets[key] = TokenBucket(
                capacity=self.burst_size,
                tokens=self.burst_size,
                refill_rate=self.requests_per_second,
            )
        return self._buckets[key]

    async def _cleanup_if_needed(self):
        """清理过期的令牌桶"""
        now = time.time()
        if now - self._last_cleanup < self.cleanup_interval:
            return

        self._last_cleanup = now

        # 移除长时间未使用的桶
        expired_keys = [
            key for key, bucket in self._buckets.items()
            if now - bucket.last_refill > self.cleanup_interval
        ]

        for key in expired_keys:
            del self._buckets[key]

        if expired_keys:
            logger.debug(f"Cleaned up {len(expired_keys)} expired rate limit buckets")

    def get_stats(self) -> dict:
        """获取统计信息"""
        return {
            "active_buckets": len(self._buckets),
            "requests_per_second": self.requests_per_second,
            "burst_size": self.burst_size,
        }


class RateLimiterMiddleware(BaseHTTPMiddleware):
    """速率限制中间件

    对请求进行速率限制，超限时返回 429 错误。
    """

    def __init__(
        self,
        app,
        limiter: Optional[RateLimiter] = None,
        key_func: Optional[Callable[[Request], str]] = None,
        exclude_paths: Optional[list[str]] = None,
    ):
        super().__init__(app)

        settings = get_settings()

        self.limiter = limiter or RateLimiter(
            requests_per_second=settings.rate_limit.requests_per_second,
            burst_size=settings.rate_limit.burst_size,
        )

        self.key_func = key_func or self._default_key_func
        self.exclude_paths = exclude_paths or ["/health", "/metrics", "/docs", "/openapi.json"]
        self.enabled = settings.rate_limit.enabled

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # 检查是否启用
        if not self.enabled:
            return await call_next(request)

        # 检查是否排除的路径
        if self._is_excluded(request.url.path):
            return await call_next(request)

        # 获取限制键
        key = self.key_func(request)

        # 检查速率限制
        if not await self.limiter.is_allowed(key):
            wait_time = await self.limiter.get_wait_time(key)
            logger.warning(
                f"Rate limit exceeded for {key}, wait_time={wait_time:.2f}s"
            )

            return JSONResponse(
                status_code=429,
                content={
                    "type": "error",
                    "error": {
                        "type": "rate_limit_error",
                        "message": "Rate limit exceeded",
                        "retry_after": int(wait_time) + 1,
                    }
                },
                headers={
                    "Retry-After": str(int(wait_time) + 1),
                    "X-RateLimit-Reset": str(int(time.time() + wait_time)),
                }
            )

        return await call_next(request)

    def _is_excluded(self, path: str) -> bool:
        """检查路径是否排除"""
        return any(path.startswith(p) for p in self.exclude_paths)

    def _default_key_func(self, request: Request) -> str:
        """默认的限制键函数

        优先使用 API Key，其次使用客户端 IP。
        """
        # 尝试从请求头获取 API Key
        api_key = (
            request.headers.get("X-API-Key") or
            request.headers.get("Authorization", "").replace("Bearer ", "")
        )

        if api_key:
            # 使用 API Key 的哈希作为键
            return f"key:{api_key[:16]}"

        # 使用客户端 IP
        client_ip = self._get_client_ip(request)
        return f"ip:{client_ip}"

    def _get_client_ip(self, request: Request) -> str:
        """获取客户端 IP

        支持代理场景。
        """
        # 检查代理头
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            # 取第一个 IP（原始客户端）
            return forwarded.split(",")[0].strip()

        real_ip = request.headers.get("X-Real-IP")
        if real_ip:
            return real_ip

        # 直接连接的客户端
        if request.client:
            return request.client.host

        return "unknown"
