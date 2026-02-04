import time
from fastapi import APIRouter, Request

router = APIRouter()

@router.get("/")
@router.get("/v1/health")
@router.get("/api/v1/health")
@router.get("/api/v8/health")
async def root():
    """健康检查 - 支持多种路径以兼容不同客户端"""
    return {
        "status": "ok",
        "service": "AI History Manager",
        "version": "1.0.0",
        "timestamp": time.time()
    }

@router.get("/v1/models")
async def list_models():
    """列出可用模型 - Anthropic 格式"""
    return {
        "data": [
            {"id": "claude-opus-4-5-20251101", "object": "model", "created": 1699900000, "owned_by": "anthropic"},
            {"id": "claude-sonnet-4-5-20250929", "object": "model", "created": 1699900000, "owned_by": "anthropic"},
            {"id": "claude-haiku-4-5-20251001", "object": "model", "created": 1699900000, "owned_by": "anthropic"},
            {"id": "claude-sonnet-4", "object": "model", "created": 1699900000, "owned_by": "anthropic"},
            {"id": "claude-haiku-4", "object": "model", "created": 1699900000, "owned_by": "anthropic"},
            {"id": "claude-opus-4", "object": "model", "created": 1699900000, "owned_by": "anthropic"},
        ],
        "object": "list"
    }

@router.post("/v1/messages/count_tokens")
async def count_tokens(request: Request):
    """Token 计数端点"""
    from app.utils.helpers import count_tokens_logic
    from fastapi.responses import JSONResponse
    try:
        body = await request.json()
        estimated_tokens = count_tokens_logic(body)
        return {"input_tokens": estimated_tokens}
    except Exception as e:
        return JSONResponse(
            status_code=400,
            content={"error": {"type": "invalid_request_error", "message": str(e)}}
        )
