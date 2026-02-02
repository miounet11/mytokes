"""健康检查路由

提供服务健康状态和指标端点。
"""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ..config import get_settings
from ..utils.logging import metrics, get_logger
from ..services.http_client import HTTPClientManager

logger = get_logger(__name__)

router = APIRouter()


@router.get("/health")
async def health_check():
    """健康检查端点

    返回服务的基本健康状态。
    """
    settings = get_settings()

    # 检查 HTTP 客户端状态
    http_client_healthy = await HTTPClientManager().health_check()

    status = "healthy" if http_client_healthy else "degraded"

    return JSONResponse(
        status_code=200 if status == "healthy" else 503,
        content={
            "status": status,
            "service": "ai-history-manager",
            "version": "2.0.0",
            "checks": {
                "http_client": "ok" if http_client_healthy else "error",
            }
        }
    )


@router.get("/health/live")
async def liveness_check():
    """存活检查端点

    用于 Kubernetes 存活探针。
    """
    return {"status": "alive"}


@router.get("/health/ready")
async def readiness_check():
    """就绪检查端点

    用于 Kubernetes 就绪探针。
    """
    # 检查关键依赖
    http_client_healthy = await HTTPClientManager().health_check()

    if not http_client_healthy:
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "reason": "http_client_unavailable"}
        )

    return {"status": "ready"}


@router.get("/metrics")
async def get_metrics():
    """获取服务指标

    返回请求统计、性能指标等。
    """
    settings = get_settings()

    return {
        "service": "ai-history-manager",
        "metrics": metrics.get_all_stats(),
        "http_client": HTTPClientManager().get_stats(),
        "config": {
            "model_routing_enabled": settings.model_routing.enabled,
            "continuation_enabled": settings.continuation.enabled,
            "rate_limit_enabled": settings.rate_limit.enabled,
        }
    }


@router.get("/")
async def root():
    """根路径

    返回 API 基本信息。
    """
    return {
        "service": "AI History Manager",
        "version": "2.0.0",
        "description": "Anthropic Messages API compatible proxy with intelligent routing",
        "endpoints": {
            "messages": "/v1/messages",
            "models": "/v1/models",
            "health": "/health",
            "metrics": "/metrics",
        }
    }
