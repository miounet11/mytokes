"""OpenAI Chat Completions API 兼容路由

实现 OpenAI Chat Completions API 兼容的端点，
内部转换为 Anthropic Messages API 调用。
"""

import json
import time
import uuid
from typing import Optional, AsyncGenerator, List, Dict, Any

from fastapi import APIRouter, Request, Header
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel, Field

from ..config import get_settings
from ..utils.logging import get_logger, get_request_id, metrics
from ..utils.exceptions import (
    BadRequestError,
    AuthenticationError,
    UpstreamError,
)
from ..services.model_router import get_router
from ..services.http_client import get_http_client

logger = get_logger(__name__)

router = APIRouter()


# ==================== 请求/响应模型 ====================

class ChatMessage(BaseModel):
    """聊天消息"""
    role: str
    content: str
    name: Optional[str] = None


class ChatCompletionRequest(BaseModel):
    """OpenAI Chat Completion 请求模型"""
    model: str = Field(..., description="Model ID")
    messages: List[Dict[str, Any]] = Field(..., description="Conversation messages")
    max_tokens: Optional[int] = Field(4096, description="Maximum tokens to generate")
    temperature: Optional[float] = Field(None, ge=0, le=2)
    top_p: Optional[float] = Field(None, ge=0, le=1)
    n: Optional[int] = Field(1, description="Number of completions")
    stream: bool = Field(False, description="Enable streaming")
    stop: Optional[List[str]] = Field(None, description="Stop sequences")
    presence_penalty: Optional[float] = Field(None)
    frequency_penalty: Optional[float] = Field(None)
    user: Optional[str] = Field(None)

    class Config:
        extra = "allow"


# ==================== 格式转换 ====================

def openai_to_anthropic_messages(messages: List[Dict[str, Any]]) -> tuple[Optional[str], List[Dict[str, Any]]]:
    """将 OpenAI 消息格式转换为 Anthropic 格式

    Returns:
        tuple: (system_prompt, messages)
    """
    system_prompt = None
    anthropic_messages = []

    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")

        if role == "system":
            # Anthropic 使用单独的 system 参数
            if system_prompt:
                system_prompt += "\n\n" + content
            else:
                system_prompt = content
        elif role == "user":
            anthropic_messages.append({
                "role": "user",
                "content": content
            })
        elif role == "assistant":
            anthropic_messages.append({
                "role": "assistant",
                "content": content
            })
        elif role == "function" or role == "tool":
            # 将工具响应作为用户消息处理
            anthropic_messages.append({
                "role": "user",
                "content": f"[Tool Response]: {content}"
            })

    return system_prompt, anthropic_messages


def anthropic_to_openai_response(anthropic_response: dict, model: str) -> dict:
    """将 Anthropic 响应转换为 OpenAI 格式"""
    content = ""
    for block in anthropic_response.get("content", []):
        if isinstance(block, dict) and block.get("type") == "text":
            content += block.get("text", "")

    # 映射 stop_reason
    stop_reason = anthropic_response.get("stop_reason")
    finish_reason = "stop"
    if stop_reason == "max_tokens":
        finish_reason = "length"
    elif stop_reason == "stop_sequence":
        finish_reason = "stop"
    elif stop_reason == "tool_use":
        finish_reason = "tool_calls"

    usage = anthropic_response.get("usage", {})

    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": content
            },
            "finish_reason": finish_reason
        }],
        "usage": {
            "prompt_tokens": usage.get("input_tokens", 0),
            "completion_tokens": usage.get("output_tokens", 0),
            "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
        }
    }


def anthropic_stream_to_openai_stream(event: dict, model: str, response_id: str) -> Optional[str]:
    """将 Anthropic 流式事件转换为 OpenAI 格式"""
    event_type = event.get("type", "")

    if event_type == "content_block_delta":
        delta = event.get("delta", {})
        if delta.get("type") == "text_delta":
            text = delta.get("text", "")
            chunk = {
                "id": response_id,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [{
                    "index": 0,
                    "delta": {
                        "content": text
                    },
                    "finish_reason": None
                }]
            }
            return f"data: {json.dumps(chunk)}\n\n"

    elif event_type == "message_delta":
        delta = event.get("delta", {})
        stop_reason = delta.get("stop_reason")
        if stop_reason:
            finish_reason = "stop"
            if stop_reason == "max_tokens":
                finish_reason = "length"

            chunk = {
                "id": response_id,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [{
                    "index": 0,
                    "delta": {},
                    "finish_reason": finish_reason
                }]
            }
            return f"data: {json.dumps(chunk)}\n\n"

    elif event_type == "message_start":
        # 发送初始 chunk
        chunk = {
            "id": response_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "delta": {
                    "role": "assistant",
                    "content": ""
                },
                "finish_reason": None
            }]
        }
        return f"data: {json.dumps(chunk)}\n\n"

    return None


# ==================== 主要端点 ====================

@router.post("/chat/completions")
async def create_chat_completion(
    request: Request,
    authorization: Optional[str] = Header(None),
):
    """创建聊天完成

    OpenAI Chat Completions API 兼容端点。
    内部转换为 Anthropic Messages API 调用。
    """
    settings = get_settings()
    request_id = get_request_id()

    # 获取 API Key
    api_key = _extract_bearer_token(authorization)
    if not api_key:
        api_key = settings.api.anthropic_api_key

    if not api_key:
        raise AuthenticationError("API key is required")

    # 解析请求体
    try:
        body = await request.json()
    except json.JSONDecodeError as e:
        raise BadRequestError(f"Invalid JSON: {e}")

    # 转换消息格式
    system_prompt, anthropic_messages = openai_to_anthropic_messages(
        body.get("messages", [])
    )

    # 构建 Anthropic 请求
    anthropic_body = {
        "model": body.get("model", "claude-sonnet-4-20250514"),
        "messages": anthropic_messages,
        "max_tokens": body.get("max_tokens", 4096),
        "stream": body.get("stream", False),
    }

    if system_prompt:
        anthropic_body["system"] = system_prompt

    if body.get("temperature") is not None:
        # OpenAI 温度范围 0-2，Anthropic 0-1
        anthropic_body["temperature"] = min(body["temperature"], 1.0)

    if body.get("top_p") is not None:
        anthropic_body["top_p"] = body["top_p"]

    if body.get("stop"):
        anthropic_body["stop_sequences"] = body["stop"]

    # 执行模型路由
    router_instance = get_router()
    routing = router_instance.route(anthropic_body, request_id)

    logger.info(
        f"Chat completion request: model={routing.routed_model}, "
        f"stream={anthropic_body.get('stream', False)}, "
        f"routing_reason={routing.reason}"
    )

    # 更新请求中的模型
    anthropic_body["model"] = routing.routed_model
    original_model = body.get("model", routing.routed_model)

    # 记录指标
    metrics.increment("chat_completions_requests")
    metrics.increment(f"model_{routing.reason}")

    # 根据是否流式选择处理方式
    if anthropic_body.get("stream", False):
        return await _handle_stream_request(anthropic_body, api_key, original_model)
    else:
        return await _handle_non_stream_request(anthropic_body, api_key, original_model)


# ==================== 请求处理 ====================

async def _handle_non_stream_request(
    body: dict,
    api_key: str,
    original_model: str,
) -> JSONResponse:
    """处理非流式请求"""
    settings = get_settings()
    client = await get_http_client()

    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }

    start_time = time.time()

    url = f"{settings.api.anthropic_base_url}/v1/messages"
    logger.info(f"Sending request to: {url}")
    logger.debug(f"Request body: {json.dumps(body, ensure_ascii=False)[:500]}")

    try:
        response = await client.post(
            url,
            json=body,
            headers=headers,
            timeout=settings.api.request_timeout,
        )
        logger.info(f"Response status: {response.status_code}")

        duration_ms = (time.time() - start_time) * 1000
        metrics.record_timing("upstream_latency", duration_ms)

        if response.status_code != 200:
            error_body = response.text
            logger.error(f"Upstream error: {response.status_code} - {error_body}")

            # 转换为 OpenAI 错误格式
            return JSONResponse(
                status_code=response.status_code,
                content={
                    "error": {
                        "message": f"Upstream API error: {error_body}",
                        "type": "api_error",
                        "code": str(response.status_code)
                    }
                }
            )

        # 检查响应内容
        response_text = response.text
        if not response_text:
            logger.error("Empty response from upstream")
            return JSONResponse(
                status_code=502,
                content={
                    "error": {
                        "message": "Empty response from upstream API",
                        "type": "api_error",
                        "code": "502"
                    }
                }
            )

        try:
            result = json.loads(response_text)
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON from upstream: {response_text[:200]}")
            return JSONResponse(
                status_code=502,
                content={
                    "error": {
                        "message": f"Invalid JSON from upstream: {e}",
                        "type": "api_error",
                        "code": "502"
                    }
                }
            )

        # 转换为 OpenAI 格式
        openai_response = anthropic_to_openai_response(result, original_model)

        # 记录使用量
        usage = result.get("usage", {})
        metrics.increment("input_tokens", usage.get("input_tokens", 0))
        metrics.increment("output_tokens", usage.get("output_tokens", 0))

        return JSONResponse(content=openai_response)

    except Exception as e:
        logger.error(f"Request failed: {e}")
        metrics.increment("request_errors")
        raise


async def _handle_stream_request(
    body: dict,
    api_key: str,
    original_model: str,
) -> StreamingResponse:
    """处理流式请求"""
    return StreamingResponse(
        _stream_response(body, api_key, original_model),
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
    original_model: str,
) -> AsyncGenerator[str, None]:
    """生成流式响应（OpenAI 格式）"""
    settings = get_settings()
    client = await get_http_client()

    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }

    response_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"

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
                error_chunk = {
                    "error": {
                        "message": f"Upstream error: {response.status_code}",
                        "type": "api_error"
                    }
                }
                yield f"data: {json.dumps(error_chunk)}\n\n"
                return

            async for line in response.aiter_lines():
                if not line:
                    continue

                if line.startswith("data: "):
                    data = line[6:]

                    if data == "[DONE]":
                        yield "data: [DONE]\n\n"
                        break

                    try:
                        event = json.loads(data)
                        openai_chunk = anthropic_stream_to_openai_stream(
                            event, original_model, response_id
                        )
                        if openai_chunk:
                            yield openai_chunk
                    except json.JSONDecodeError:
                        pass

            # 发送结束标记
            yield "data: [DONE]\n\n"

    except Exception as e:
        logger.error(f"Stream error: {e}")
        error_chunk = {
            "error": {
                "message": str(e),
                "type": "api_error"
            }
        }
        yield f"data: {json.dumps(error_chunk)}\n\n"


# ==================== 辅助函数 ====================

def _extract_bearer_token(authorization: Optional[str]) -> Optional[str]:
    """从 Authorization 头提取 Bearer token"""
    if not authorization:
        return None

    if authorization.startswith("Bearer "):
        return authorization[7:]

    return None
