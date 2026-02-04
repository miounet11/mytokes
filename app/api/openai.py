import re
import uuid
import time
import json
from typing import AsyncIterator
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse
from ai_history_manager import HistoryManager
from ai_history_manager.utils import is_content_length_error

from app.core.config import (
    KIRO_API_KEY, KIRO_PROXY_URL, HISTORY_CONFIG, ASYNC_SUMMARY_CONFIG,
    NATIVE_TOOLS_ENABLED, logger
)
from app.services.context import generate_session_id, enhance_user_message, extract_user_content
from app.services.managers import async_summary_manager

router = APIRouter()

@router.post("/chat/completions")
async def chat_completions(request: Request):
    """聊天完成接口 - OpenAI 兼容"""
    request_id = uuid.uuid4().hex[:8]

    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(400, "Invalid JSON")

    model = body.get("model", "claude-sonnet-4")
    messages = body.get("messages", [])
    stream = body.get("stream", False)

    if not messages:
        raise HTTPException(400, "messages is required")

    logger.info(f"[{request_id}] Request: model={model}, messages={len(messages)}, stream={stream}")

    # ==================== Session ID 生成（防止串会话）====================
    # 从请求头提取客户端标识
    client_id = (
        request.headers.get("X-Client-ID") or
        request.headers.get("X-API-Key") or
        request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or
        request.client.host if request.client else None
    )
    conversation_id = (
        request.headers.get("X-Conversation-ID") or
        request.headers.get("X-Session-ID") or
        body.get("metadata", {}).get("conversation_id") or
        body.get("conversation_id")
    )

    session_id = generate_session_id(messages, client_id, conversation_id)
    logger.info(f"[{request_id}] Session: {session_id[:12]}... (client={client_id[:8] if client_id else 'none'})")

    # ==================== 上下文增强 ====================
    http_client_getter = lambda: request.app.state.http_client
    messages = await enhance_user_message(messages, session_id, http_client_getter)
    body["messages"] = messages

    # 创建历史管理器
    manager = HistoryManager(HISTORY_CONFIG, cache_key=session_id)

    # 预处理消息
    user_content = extract_user_content(messages)
    should_summarize = manager.should_summarize(messages)

    # ==================== 异步摘要优化 ====================
    async def call_kiro_for_summary(prompt: str) -> str:
        summary_id = uuid.uuid4().hex[:8]
        from app.core.config import SUMMARY_MODEL
        request_body = {
            "model": SUMMARY_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "max_tokens": 2000,
        }
        headers = {
            "Authorization": f"Bearer {KIRO_API_KEY}",
            "Content-Type": "application/json",
            "X-Request-ID": f"summary_{summary_id}",
            "X-Trace-ID": f"trace_{uuid.uuid4().hex}",
        }
        try:
            client = http_client_getter()
            response = await client.post(
                request.app.state.kiro_proxy_url,
                json=request_body,
                headers=headers,
                timeout=60,
            )
            if response.status_code == 200:
                data = response.json()
                return data.get("choices", [{}])[0].get("message", {}).get("content", "")
        except Exception as e:
            logger.warning(f"摘要生成失败: {e}")
        return ""

    if should_summarize and ASYNC_SUMMARY_CONFIG.get("enabled", True):
        cached_summary, has_cache, original_tokens = async_summary_manager.get_cached_summary(session_id)

        if has_cache:
            cached_processed = async_summary_manager.get_cached_processed_messages(session_id)
            if cached_processed:
                logger.info(f"[{request_id}] ⚡ 使用缓存摘要")
                processed_messages = cached_processed
                if async_summary_manager.should_update_summary(session_id, len(messages)):
                    await async_summary_manager.schedule_summary_task(
                        session_id, messages, manager, user_content, call_kiro_for_summary
                    )
            else:
                processed_messages = manager.pre_process(messages, user_content)
        else:
            logger.info(f"[{request_id}] ⚡ 首次请求，使用简单截断")
            processed_messages = manager.pre_process(messages, user_content)
            await async_summary_manager.schedule_summary_task(
                session_id, messages, manager, user_content, call_kiro_for_summary
            )
    else:
        processed_messages = manager.pre_process(messages, user_content)

    if manager.was_truncated:
        logger.info(f"[{request_id}] {manager.truncate_info}")

    # 构建请求
    kiro_request = {
        "model": model,
        "messages": processed_messages,
        "stream": stream,
    }

    for key in ["temperature", "max_tokens", "top_p", "frequency_penalty", "presence_penalty", "stop"]:
        if key in body and body[key] is not None:
            kiro_request[key] = body[key]

    if NATIVE_TOOLS_ENABLED:
        if "tools" in body and body["tools"]:
            kiro_request["tools"] = body["tools"]
        if "tool_choice" in body and body["tool_choice"]:
            kiro_request["tool_choice"] = body["tool_choice"]

    headers = {
        "Authorization": f"Bearer {KIRO_API_KEY}",
        "Content-Type": "application/json",
        "X-Request-ID": f"chat_{request_id}_{uuid.uuid4().hex[:8]}",
        "X-Trace-ID": f"trace_{uuid.uuid4().hex}",
        "X-Client-ID": f"client_{uuid.uuid4().hex[:12]}",
    }

    if stream:
        return await handle_stream(kiro_request, headers, manager, request_id, call_kiro_for_summary, http_client_getter)
    else:
        return await handle_non_stream(kiro_request, headers, manager, request_id, call_kiro_for_summary, http_client_getter)

async def handle_stream(
    kiro_request: dict,
    headers: dict,
    manager: HistoryManager,
    request_id: str,
    call_kiro_for_summary,
    http_client_getter
) -> StreamingResponse:
    """处理流式响应"""

    async def generate() -> AsyncIterator[bytes]:
        nonlocal kiro_request
        retry_count = 0
        max_retries = HISTORY_CONFIG.max_retries

        while retry_count <= max_retries:
            try:
                client = http_client_getter()
                async with client.stream(
                    "POST",
                    KIRO_PROXY_URL,
                    json=kiro_request,
                    headers=headers,
                ) as response:

                    if response.status_code != 200:
                        error_text = await response.aread()
                        error_str = error_text.decode()
                        logger.error(f"[{request_id}] Kiro API Error {response.status_code}: {error_str[:200]}")

                        if is_content_length_error(response.status_code, error_str):
                            truncated, should_retry = await manager.handle_length_error_async(
                                kiro_request["messages"],
                                retry_count,
                                call_kiro_for_summary,
                            )
                            if should_retry:
                                kiro_request["messages"] = truncated
                                retry_count += 1
                                continue

                        error_response = {
                            "error": {
                                "message": error_str[:500],
                                "type": "api_error",
                                "code": response.status_code,
                            }
                        }
                        yield f"data: {json.dumps(error_response)}\n\n".encode()
                        yield b"data: [DONE]\n\n"
                        return

                    async for chunk in response.aiter_bytes():
                        yield chunk
                    return

            except Exception as e:
                logger.error(f"[{request_id}] 请求异常: {e}")
                if retry_count < max_retries:
                    retry_count += 1
                    await asyncio.sleep(1)
                    continue
                error_response = {"error": {"message": str(e), "type": "api_error"}}
                yield f"data: {json.dumps(error_response)}\n\n".encode()
                yield b"data: [DONE]\n\n"
                return

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"}
    )

async def handle_non_stream(
    kiro_request: dict,
    headers: dict,
    manager: HistoryManager,
    request_id: str,
    call_kiro_for_summary,
    http_client_getter
) -> JSONResponse:
    """处理非流式响应"""
    retry_count = 0
    max_retries = HISTORY_CONFIG.max_retries

    while retry_count <= max_retries:
        try:
            client = http_client_getter()
            response = await client.post(
                KIRO_PROXY_URL,
                json=kiro_request,
                headers=headers,
            )

            if response.status_code != 200:
                error_str = response.text
                if is_content_length_error(response.status_code, error_str):
                    truncated, should_retry = await manager.handle_length_error_async(
                        kiro_request["messages"],
                        retry_count,
                        call_kiro_for_summary,
                    )
                    if should_retry:
                        kiro_request["messages"] = truncated
                        retry_count += 1
                        continue
                raise HTTPException(response.status_code, error_str[:500])

            return JSONResponse(content=response.json())

        except Exception as e:
            if retry_count < max_retries:
                retry_count += 1
                await asyncio.sleep(1)
                continue
            if isinstance(e, HTTPException): raise
            raise HTTPException(500, str(e))

    raise HTTPException(503, "All retries exhausted")
