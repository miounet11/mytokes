"""HTTP 客户端管理模块

提供全局 HTTP 客户端池，支持连接复用和性能优化。
"""

import httpx
import asyncio
from typing import Optional
from contextlib import asynccontextmanager

from ..config import get_settings
from ..utils.logging import get_logger

logger = get_logger(__name__)


class HTTPClientManager:
    """HTTP 客户端管理器

    管理全局 HTTP 客户端实例，提供连接池复用。
    """

    _instance: Optional["HTTPClientManager"] = None
    _client: Optional[httpx.AsyncClient] = None
    _lock: asyncio.Lock = asyncio.Lock()

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    async def get_client(self) -> httpx.AsyncClient:
        """获取 HTTP 客户端实例

        如果客户端不存在或已关闭，则创建新实例。

        Returns:
            httpx.AsyncClient 实例
        """
        async with self._lock:
            if self._client is None or self._client.is_closed:
                self._client = await self._create_client()
            return self._client

    async def _create_client(self) -> httpx.AsyncClient:
        """创建新的 HTTP 客户端"""
        settings = get_settings()
        pool_config = settings.http_pool
        api_config = settings.api

        # 配置连接限制
        limits = httpx.Limits(
            max_connections=pool_config.max_connections,
            max_keepalive_connections=pool_config.max_keepalive,
            keepalive_expiry=pool_config.keepalive_expiry,
        )

        # 配置超时
        timeout = httpx.Timeout(
            connect=api_config.connect_timeout,
            read=api_config.request_timeout,
            write=api_config.request_timeout,
            pool=30.0,
        )

        # 创建客户端
        client = httpx.AsyncClient(
            limits=limits,
            timeout=timeout,
            http2=pool_config.use_http2,
            follow_redirects=True,
        )

        logger.info(
            f"Created HTTP client: max_connections={pool_config.max_connections}, "
            f"keepalive={pool_config.max_keepalive}, http2={pool_config.use_http2}"
        )

        return client

    async def close(self):
        """关闭 HTTP 客户端"""
        async with self._lock:
            if self._client and not self._client.is_closed:
                await self._client.aclose()
                logger.info("HTTP client closed")
            self._client = None

    async def health_check(self) -> bool:
        """检查客户端健康状态"""
        try:
            client = await self.get_client()
            return not client.is_closed
        except Exception as e:
            logger.error(f"HTTP client health check failed: {e}")
            return False

    def get_stats(self) -> dict:
        """获取客户端统计信息"""
        if self._client is None or self._client.is_closed:
            return {"status": "closed"}

        return {
            "status": "open",
            "http2": self._client._transport is not None,
        }


# 全局客户端管理器实例
_manager = HTTPClientManager()


async def get_http_client() -> httpx.AsyncClient:
    """获取全局 HTTP 客户端

    Returns:
        httpx.AsyncClient 实例
    """
    return await _manager.get_client()


async def close_http_client():
    """关闭全局 HTTP 客户端"""
    await _manager.close()


@asynccontextmanager
async def http_client_context():
    """HTTP 客户端上下文管理器

    用于确保客户端在使用后正确关闭。

    Usage:
        async with http_client_context() as client:
            response = await client.get(url)
    """
    client = await get_http_client()
    try:
        yield client
    finally:
        pass  # 不关闭全局客户端


async def make_request(
    method: str,
    url: str,
    **kwargs
) -> httpx.Response:
    """发送 HTTP 请求

    封装常用的请求逻辑，添加错误处理和日志。

    Args:
        method: HTTP 方法
        url: 请求 URL
        **kwargs: 传递给 httpx 的其他参数

    Returns:
        httpx.Response

    Raises:
        httpx.HTTPError: 请求失败时
    """
    client = await get_http_client()

    logger.debug(f"Making {method} request to {url}")

    try:
        response = await client.request(method, url, **kwargs)
        logger.debug(f"Response: {response.status_code}")
        return response
    except httpx.TimeoutException as e:
        logger.error(f"Request timeout: {url} - {e}")
        raise
    except httpx.HTTPError as e:
        logger.error(f"HTTP error: {url} - {e}")
        raise


async def stream_request(
    method: str,
    url: str,
    **kwargs
):
    """发送流式 HTTP 请求

    Args:
        method: HTTP 方法
        url: 请求 URL
        **kwargs: 传递给 httpx 的其他参数

    Yields:
        响应数据块
    """
    client = await get_http_client()

    logger.debug(f"Making streaming {method} request to {url}")

    async with client.stream(method, url, **kwargs) as response:
        async for chunk in response.aiter_bytes():
            yield chunk
