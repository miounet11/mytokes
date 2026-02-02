"""模型列表路由

提供可用模型列表端点。
"""

from fastapi import APIRouter

from ..config import get_settings
from ..utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter()


@router.get("/models")
async def list_models():
    """列出可用模型

    返回支持的 Claude 模型列表。
    """
    settings = get_settings()
    routing = settings.model_routing

    models = [
        {
            "id": routing.opus_model,
            "object": "model",
            "created": 1698959748,
            "owned_by": "anthropic",
            "capabilities": {
                "vision": True,
                "function_calling": True,
            },
            "context_window": 200000,
            "description": "Most capable model for complex tasks",
        },
        {
            "id": routing.sonnet_model,
            "object": "model",
            "created": 1698959748,
            "owned_by": "anthropic",
            "capabilities": {
                "vision": True,
                "function_calling": True,
            },
            "context_window": 200000,
            "description": "Balanced performance and cost",
        },
        {
            "id": routing.haiku_model,
            "object": "model",
            "created": 1698959748,
            "owned_by": "anthropic",
            "capabilities": {
                "vision": True,
                "function_calling": True,
            },
            "context_window": 200000,
            "description": "Fast and cost-effective",
        },
    ]

    return {
        "object": "list",
        "data": models,
    }


@router.get("/models/{model_id}")
async def get_model(model_id: str):
    """获取模型详情

    Args:
        model_id: 模型 ID
    """
    settings = get_settings()
    routing = settings.model_routing

    # 模型信息映射
    model_info = {
        routing.opus_model: {
            "id": routing.opus_model,
            "object": "model",
            "created": 1698959748,
            "owned_by": "anthropic",
            "capabilities": {
                "vision": True,
                "function_calling": True,
            },
            "context_window": 200000,
            "description": "Most capable model for complex tasks",
        },
        routing.sonnet_model: {
            "id": routing.sonnet_model,
            "object": "model",
            "created": 1698959748,
            "owned_by": "anthropic",
            "capabilities": {
                "vision": True,
                "function_calling": True,
            },
            "context_window": 200000,
            "description": "Balanced performance and cost",
        },
        routing.haiku_model: {
            "id": routing.haiku_model,
            "object": "model",
            "created": 1698959748,
            "owned_by": "anthropic",
            "capabilities": {
                "vision": True,
                "function_calling": True,
            },
            "context_window": 200000,
            "description": "Fast and cost-effective",
        },
    }

    # 检查模型是否存在
    if model_id in model_info:
        return model_info[model_id]

    # 检查是否是别名
    model_lower = model_id.lower()
    if "opus" in model_lower:
        return model_info[routing.opus_model]
    elif "sonnet" in model_lower:
        return model_info[routing.sonnet_model]
    elif "haiku" in model_lower:
        return model_info[routing.haiku_model]

    # 返回通用模型信息
    return {
        "id": model_id,
        "object": "model",
        "created": 1698959748,
        "owned_by": "anthropic",
        "capabilities": {
            "vision": True,
            "function_calling": True,
        },
        "context_window": 200000,
    }
