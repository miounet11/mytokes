"""API 路由模块"""

from fastapi import APIRouter

from .messages import router as messages_router
from .health import router as health_router
from .models import router as models_router
from .chat_completions import router as chat_completions_router

# 创建主路由
api_router = APIRouter()

# 注册子路由
api_router.include_router(messages_router, prefix="/v1", tags=["messages"])
api_router.include_router(chat_completions_router, prefix="/v1", tags=["chat"])
api_router.include_router(models_router, prefix="/v1", tags=["models"])
api_router.include_router(health_router, tags=["health"])

__all__ = [
    "api_router",
    "messages_router",
    "chat_completions_router",
    "health_router",
    "models_router",
]
