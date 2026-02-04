from fastapi import APIRouter
from .anthropic import router as anthropic_router
from .openai import router as openai_router
from .admin import router as admin_router
from .base import router as base_router

api_router = APIRouter()

# Anthropic 兼容端点
api_router.include_router(anthropic_router, prefix="/v1", tags=["anthropic"])

# OpenAI 兼容端点
api_router.include_router(openai_router, prefix="/v1", tags=["openai"])

# 管理端点
api_router.include_router(admin_router, prefix="/admin", tags=["admin"])

# 基础端点 (健康检查, 模型列表等)
api_router.include_router(base_router, tags=["base"])
