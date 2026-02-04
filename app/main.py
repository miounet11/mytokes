import re
"""AI History Manager 主应用

Anthropic Messages API 兼容代理服务，支持智能模型路由和响应续传。
"""

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import get_settings, Settings
from .utils.logging import setup_logging, get_logger
from .middleware import (
    RequestContextMiddleware,
    ErrorHandlerMiddleware,
    RateLimiterMiddleware,
)
from .middleware.error_handler import setup_exception_handlers
from .api import api_router
from .services.http_client import close_http_client


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    logger = get_logger(__name__)
    settings = get_settings()

    # 启动时
    logger.info("Starting AI History Manager...")
    logger.info(f"Environment: {settings.environment}")
    logger.info(f"Model routing: {'enabled' if settings.model_routing.enabled else 'disabled'}")
    logger.info(f"Continuation: {'enabled' if settings.continuation.enabled else 'disabled'}")

    yield

    # 关闭时
    logger.info("Shutting down AI History Manager...")
    await close_http_client()
    logger.info("Shutdown complete")


def create_app(settings: Settings = None) -> FastAPI:
    """创建 FastAPI 应用

    Args:
        settings: 可选的配置对象，用于测试

    Returns:
        FastAPI 应用实例
    """
    if settings is None:
        settings = get_settings()

    # 设置日志
    setup_logging(
        level=settings.log_level,
        json_format=settings.environment == "production",
    )

    # 创建应用
    app = FastAPI(
        title="AI History Manager",
        description="Anthropic Messages API compatible proxy with intelligent routing",
        version="2.0.0",
        lifespan=lifespan,
        docs_url="/docs" if settings.environment != "production" else None,
        redoc_url="/redoc" if settings.environment != "production" else None,
    )

    # 添加中间件（顺序重要：后添加的先执行）
    _setup_middleware(app, settings)

    # 设置异常处理器
    setup_exception_handlers(app)

    # 注册路由
    app.include_router(api_router)

    return app


def _setup_middleware(app: FastAPI, settings: Settings):
    """设置中间件"""
    # CORS 中间件
    if settings.cors.enabled:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors.allow_origins,
            allow_credentials=settings.cors.allow_credentials,
            allow_methods=settings.cors.allow_methods,
            allow_headers=settings.cors.allow_headers,
        )

    # 速率限制中间件
    if settings.rate_limit.enabled:
        app.add_middleware(RateLimiterMiddleware)

    # 错误处理中间件
    app.add_middleware(ErrorHandlerMiddleware)

    # 请求上下文中间件（最先执行）
    app.add_middleware(RequestContextMiddleware)


# 创建默认应用实例
app = create_app()


if __name__ == "__main__":
    import uvicorn

    settings = get_settings()

    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.environment == "development",
        workers=1 if settings.environment == "development" else 4,
    )
