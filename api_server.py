import os
import time
import logging
import httpx
import uuid
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

# 导入模块化组件
from app.core.config import (
    SERVICE_PORT, REQUEST_TIMEOUT, HTTP_CONNECT_TIMEOUT,
    HTTP_READ_TIMEOUT, HTTP_WRITE_TIMEOUT, HTTP_POOL_TIMEOUT,
    HTTP_POOL_MAX_CONNECTIONS, HTTP_POOL_MAX_KEEPALIVE,
    HTTP_POOL_KEEPALIVE_EXPIRY, HTTP_USE_HTTP2,
    KIRO_PROXY_BASE, logger
)
from app.api import api_router
from app.services.streaming import set_http_client_getter

# ==================== 全局 HTTP 客户端 ====================

http_client: httpx.AsyncClient = None

def get_http_client() -> httpx.AsyncClient:
    global http_client
    if http_client is None:
        raise RuntimeError("HTTP client not initialized")
    return http_client

# 注入到 streaming service
set_http_client_getter(get_http_client)

@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    global http_client

    logger.info("初始化全局 HTTP 客户端 (模块化高并发模式)...")

    limits = httpx.Limits(
        max_connections=HTTP_POOL_MAX_CONNECTIONS,
        max_keepalive_connections=HTTP_POOL_MAX_KEEPALIVE,
        keepalive_expiry=HTTP_POOL_KEEPALIVE_EXPIRY,
    )

    timeout = httpx.Timeout(
        timeout=REQUEST_TIMEOUT,
        connect=HTTP_CONNECT_TIMEOUT,
        read=HTTP_READ_TIMEOUT,
        write=HTTP_WRITE_TIMEOUT,
        pool=HTTP_POOL_TIMEOUT,
    )

    http_client = httpx.AsyncClient(
        timeout=timeout,
        limits=limits,
        http2=HTTP_USE_HTTP2,
    )

    # 将客户端和代理地址存储在 app.state 中
    app.state.http_client = http_client
    app.state.kiro_proxy_url = f"{KIRO_PROXY_BASE}/kiro/v1/chat/completions"

    logger.info(f"HTTP 客户端已初始化: max_conn={HTTP_POOL_MAX_CONNECTIONS}")

    yield

    logger.info("关闭全局 HTTP 客户端...")
    if http_client:
        await http_client.aclose()
        http_client = None
    logger.info("资源清理完成")

# ==================== FastAPI App ====================

app = FastAPI(
    title="AI History Manager API",
    description="模块化重构版：OpenAI 兼容 API，集成智能历史消息管理",
    version="2.0.0",
    lifespan=lifespan,
)

# 添加 CORS 支持
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册所有路由
app.include_router(api_router)

if __name__ == "__main__":
    import uvicorn
    
    print(f"""
╔══════════════════════════════════════════════════════════════╗
║      AI History Manager API Server (Modular Refactored)      ║
╠══════════════════════════════════════════════════════════════╣
║  服务地址: http://0.0.0.0:{SERVICE_PORT}                          ║
║  API 端点: /v1/chat/completions                              ║
║  健康检查: /                                                 ║
║  模型列表: /v1/models                                        ║
║  配置查看: /admin/config                                     ║
╚══════════════════════════════════════════════════════════════╝
""")

    uvicorn.run(app, host="0.0.0.0", port=SERVICE_PORT)
