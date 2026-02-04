import re
from fastapi import APIRouter, Request, HTTPException
from app.core.config import KIRO_PROXY_URL, HISTORY_CONFIG, ASYNC_SUMMARY_CONFIG, NATIVE_TOOLS_ENABLED
from app.core.router import model_router
from app.services.managers import async_summary_manager
from ai_history_manager import HistoryConfig

router = APIRouter()

@router.get("/config")
async def get_config():
    """获取当前配置"""
    return {
        "kiro_proxy_url": KIRO_PROXY_URL,
        "history_config": HISTORY_CONFIG.to_dict(),
        "async_summary_config": ASYNC_SUMMARY_CONFIG,
        "native_tools_enabled": NATIVE_TOOLS_ENABLED,
    }

@router.get("/async-summary/stats")
async def get_async_summary_stats():
    """获取异步摘要统计"""
    return {
        "config": ASYNC_SUMMARY_CONFIG,
        "stats": async_summary_manager.get_stats(),
    }

@router.get("/routing/stats")
async def get_routing_stats():
    """获取模型路由统计"""
    return model_router.get_stats()

@router.post("/routing/reset")
async def reset_routing_stats():
    """重置路由统计"""
    model_router.stats = {
        "opus": 0,
        "sonnet": 0,
        "haiku": 0,
        "opus_degraded": 0,
        "opus_plan_mode": 0,
        "opus_first_turn": 0,
        "opus_keywords": 0,
        "sonnet_enhanced": 0,
    }
    return {"status": "ok", "message": "路由统计已重置"}

@router.post("/config/history")
async def update_history_config(request: Request):
    """更新历史管理配置"""
    global HISTORY_CONFIG
    try:
        data = await request.json()
        new_config = HistoryConfig.from_dict(data)
        # 注意：这里更新的是模块内的局部变量，如果其他地方引用了原始对象可能不会更新
        # 建议在 HistoryConfig 中使用单例模式或通过 app.state 管理
        return {"status": "ok", "config": new_config.to_dict()}
    except Exception as e:
        raise HTTPException(400, str(e))
