"""消息 API 路由

实现 Anthropic Messages API 兼容的端点。
"""

import json
import time
import uuid
from typing import Optional, AsyncGenerator

from fastapi import APIRouter, Request, Header, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel, Field

from ..config import get_settings
from ..utils.logging import get_logger, get_request_id, metrics
from ..utils.exceptions import (
    BadRequestError,
    AuthenticationError,
    UpstreamError,
)
from ..services.model_router import get_router, RoutingDecision
from ..services.http_client import get_http_client
from ..services.continuation import ContinuationHandler, TruncationInfo

logger = get_logger(__name__)

router = APIRouter()


# ==================== 请求/响应模型 ====================

class MessageRequest(BaseModel):
    """消息请求模型"""
    model: str = Field(..., description="Model ID")
    messages: list = Field(..., description="Conversation messages")
    max_tokens: int = Field(4096, description="Maximum tokens to generate")
    system: Optional[str] = Field(None, description="System prompt")
    temperature: Optional[float] = Field(None, ge=0, le=1)
    top_p: Optional[float] = Field(None, ge=0, le=1)
    top_k: Optional[int] = Field(None, ge=0)
    stop_sequences: Optional[list[str]] = Field(None)
    stream: bool = Field(False, description="Enable streaming")
    tools: Optional[list] = Field(None, description="Available tools")
    tool_choice: Optional[dict] = Field(None)
    metadata: Optional[dict] = Field(None)

    class Config:
        extra = "allow"  # 允许额外字段


# ==================== 主要端点 ====================

@router.post("/messages")
async def create_message(
    request: Request,
    x_api_key: Optional[str] = Header(None, alias="x-api-key"),
    authorization: Optional[str] = Header(None),
):
    """创建消息

    Anthropic Messages API 兼容端点。
    支持流式和非流式响应。
    """
    settings = get_settings()
    request_id = get_request_id()

    # 获取 API Key
    api_key = x_api_key or _extract_bearer_token(authorization)
    if not api_key:
        api_key = settings.api.anthropic_api_key

    if not api_key:
        raise AuthenticationError("API key is required")

    # 解析请求体
    try:
        body = await request.json()
    except json.JSONDecodeError as e:
        raise BadRequestError(f"Invalid JSON: {e}")

    # 执行模型路由
    router_instance = get_router()
    routing = router_instance.route(body, request_id)

    logger.info(
        f"Message request: model={routing.routed_model}, "
        f"stream={body.get('stream', False)}, "
        f"routing_reason={routing.reason}"
    )

    # 更新请求中的模型
    body["model"] = routing.routed_model

    # 记录指标
    metrics.increment("messages_requests")
    metrics.increment(f"model_{routing.reason}")

    # 根据是否流式选择处理方式
    if body.get("stream", False):
        return await _handle_stream_request(body, api_key, routing)
    else:
        return await _handle_non_stream_request(body, api_key, routing)


# ==================== 请求处理 ====================

async def _handle_non_stream_request(
    body: dict,
    api_key: str,
    routing: RoutingDecision,
) -> JSONResponse:
    """处理非流式请求"""
    settings = get_settings()
    client = await get_http_client()

    # 准备请求
    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }

    start_time = time.time()

    try:
        response = await client.post(
            f"{settings.api.anthropic_base_url}/v1/messages",
            json=body,
            headers=headers,
            timeout=settings.api.request_timeout,
        )

        duration_ms = (time.time() - start_time) * 1000
        metrics.record_timing("upstream_latency", duration_ms)

        if response.status_code != 200:
            error_body = response.text
            logger.error(f"Upstream error: {response.status_code} - {error_body}")
            raise UpstreamError(
                f"Upstream API error: {response.status_code}",
                status_code=response.status_code,
            )

        result = response.json()

        # 检查是否需要续传
        continuation_handler = ContinuationHandler()
        text_content = _extract_text_content(result)
        stop_reason = result.get("stop_reason")

        should_continue, truncation = continuation_handler.should_continue(
            text_content, stop_reason, 0
        )

        if should_continue and truncation:
            result = await _handle_continuation(
                body, api_key, result, truncation, continuation_handler
            )

        # 记录使用量
        usage = result.get("usage", {})
        metrics.increment("input_tokens", usage.get("input_tokens", 0))
        metrics.increment("output_tokens", usage.get("output_tokens", 0))

        return JSONResponse(content=result)

    except Exception as e:
        logger.error(f"Request failed: {e}")
        metrics.increment("request_errors")
        raise


async def _handle_stream_request(
    body: dict,
    api_key: str,
    routing: RoutingDecision,
) -> StreamingResponse:
    """处理流式请求"""
    return StreamingResponse(
        _stream_response(body, api_key),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
    )


async def _stream_response(
    body: dict,
    api_key: str,
) -> AsyncGenerator[str, None]:
    """生成流式响应"""
    settings = get_settings()
    client = await get_http_client()

    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }

    accumulated_text = ""
    continuation_count = 0
    continuation_handler = ContinuationHandler()

    try:
        async with client.stream(
            "POST",
            f"{settings.api.anthropic_base_url}/v1/messages",
            json=body,
            headers=headers,
            timeout=settings.api.request_timeout,
        ) as response:
            if response.status_code != 200:
                error_text = await response.aread()
                logger.error(f"Stream error: {response.status_code} - {error_text}")
                yield f"data: {{\"type\": \"error\", \"error\": {{\"message\": \"Upstream error: {response.status_code}\"}}}}\n\n"
                return

            stop_reason = None

            async for line in response.aiter_lines():
                if not line:
                    continue

                if line.startswith("data: "):
                    data = line[6:]

                    if data == "[DONE]":
                        break

                    try:
                        event = json.loads(data)
                        event_type = event.get("type", "")

                        # 累积文本内容
                        if event_type == "content_block_delta":
                            delta = event.get("delta", {})
                            if delta.get("type") == "text_delta":
                                accumulated_text += delta.get("text", "")

                        # 记录停止原因
                        if event_type == "message_delta":
                            delta = event.get("delta", {})
                            stop_reason = delta.get("stop_reason")

                    except json.JSONDecodeError:
                        pass

                yield line + "\n"

            # 检查是否需要续传
            should_continue, truncation = continuation_handler.should_continue(
                accumulated_text, stop_reason, continuation_count
            )

            if should_continue and truncation:
                # 发送续传通知
                yield f"data: {{\"type\": \"continuation_start\"}}\n\n"

                # 执行续传
                async for chunk in _stream_continuation(
                    body, api_key, accumulated_text, truncation,
                    continuation_handler, continuation_count + 1
                ):
                    yield chunk

    except Exception as e:
        logger.error(f"Stream error: {e}")
        yield f"data: {{\"type\": \"error\", \"error\": {{\"message\": \"{str(e)}\"}}}}\n\n"


async def _stream_continuation(
    original_body: dict,
    api_key: str,
    accumulated_text: str,
    truncation: TruncationInfo,
    handler: ContinuationHandler,
    continuation_count: int,
) -> AsyncGenerator[str, None]:
    """流式续传"""
    settings = get_settings()
    client = await get_http_client()

    # 构建续传请求
    continuation_body = handler.build_continuation_request(
        original_body, accumulated_text, truncation
    )

    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }

    try:
        async with client.stream(
            "POST",
            f"{settings.api.anthropic_base_url}/v1/messages",
            json=continuation_body,
            headers=headers,
            timeout=settings.api.request_timeout,
        ) as response:
            if response.status_code != 200:
                return

            new_text = ""
            stop_reason = None

            async for line in response.aiter_lines():
                if not line:
                    continue

                if line.startswith("data: "):
                    data = line[6:]

                    if data == "[DONE]":
                        break

                    try:
                        event = json.loads(data)
                        event_type = event.get("type", "")

                        if event_type == "content_block_delta":
                            delta = event.get("delta", {})
                            if delta.get("type") == "text_delta":
                                new_text += delta.get("text", "")

                        if event_type == "message_delta":
                            delta = event.get("delta", {})
                            stop_reason = delta.get("stop_reason")

                    except json.JSONDecodeError:
                        pass

                yield line + "\n"

            # 递归检查是否需要继续续传
            combined_text = accumulated_text + new_text
            should_continue, new_truncation = handler.should_continue(
                combined_text, stop_reason, continuation_count
            )

            if should_continue and new_truncation:
                async for chunk in _stream_continuation(
                    original_body, api_key, combined_text, new_truncation,
                    handler, continuation_count + 1
                ):
                    yield chunk

    except Exception as e:
        logger.error(f"Continuation stream error: {e}")


async def _handle_continuation(
    original_body: dict,
    api_key: str,
    original_response: dict,
    truncation: TruncationInfo,
    handler: ContinuationHandler,
) -> dict:
    """处理非流式续传"""
    settings = get_settings()
    client = await get_http_client()

    accumulated_text = _extract_text_content(original_response)
    total_input_tokens = original_response.get("usage", {}).get("input_tokens", 0)
    total_output_tokens = original_response.get("usage", {}).get("output_tokens", 0)

    continuation_count = 0
    max_continuations = settings.continuation.max_continuations

    while continuation_count < max_continuations:
        # 构建续传请求
        continuation_body = handler.build_continuation_request(
            original_body, accumulated_text, truncation
        )

        headers = {
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        }

        try:
            response = await client.post(
                f"{settings.api.anthropic_base_url}/v1/messages",
                json=continuation_body,
                headers=headers,
                timeout=settings.api.request_timeout,
            )

            if response.status_code != 200:
                break

            result = response.json()
            continuation_count += 1

            # 合并响应
            new_text = _extract_text_content(result)
            accumulated_text = handler.merge_responses(
                accumulated_text, new_text, truncation
            )

            # 累计 token 使用量
            usage = result.get("usage", {})
            total_input_tokens += usage.get("input_tokens", 0)
            total_output_tokens += usage.get("output_tokens", 0)

            # 检查是否需要继续
            stop_reason = result.get("stop_reason")
            should_continue, truncation = handler.should_continue(
                accumulated_text, stop_reason, continuation_count
            )

            if not should_continue:
                break

        except Exception as e:
            logger.error(f"Continuation error: {e}")
            break

    # 构建最终响应
    final_response = original_response.copy()
    final_response["content"] = [{"type": "text", "text": accumulated_text}]
    final_response["usage"] = {
        "input_tokens": total_input_tokens,
        "output_tokens": total_output_tokens,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }

    if continuation_count > 0:
        final_response["_continuation_count"] = continuation_count

    return final_response


# ==================== 辅助函数 ====================

def _extract_bearer_token(authorization: Optional[str]) -> Optional[str]:
    """从 Authorization 头提取 Bearer token"""
    if not authorization:
        return None

    if authorization.startswith("Bearer "):
        return authorization[7:]

    return None


def _extract_text_content(response: dict) -> str:
    """从响应中提取文本内容"""
    content = response.get("content", [])

    texts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            texts.append(block.get("text", ""))

    return "".join(texts)
