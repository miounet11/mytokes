import re
import uuid
import json
import os
import logging
from fastapi import APIRouter, Request, HTTPException
from app.core.config import (
    KIRO_API_KEY, HISTORY_CONFIG, ASYNC_SUMMARY_CONFIG,
    CONTEXT_ENHANCEMENT_CONFIG, NATIVE_TOOLS_ENABLED, logger
)
from app.core.router import model_router
from app.services.context import generate_session_id, enhance_user_message, extract_user_content, extract_project_context, count_user_messages
from app.services.managers import async_summary_manager, async_context_manager
from app.services.converter import convert_anthropic_to_openai
from app.services.streaming import handle_anthropic_stream_via_openai, handle_anthropic_non_stream_via_openai
from ai_history_manager import HistoryManager

router = APIRouter()

@router.post("/messages")
async def anthropic_messages(request: Request):
    """Anthropic /v1/messages 端点 - 通过 OpenAI 格式发送到 tokens 网关"""
    request_id = uuid.uuid4().hex[:8]

    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(400, "Invalid JSON")

    original_model = body.get("model", "claude-sonnet-4")
    stream = body.get("stream", False)
    orig_msg_count = len(body.get("messages", []))

    # ==================== max_tokens 处理 ====================
    DEFAULT_MAX_TOKENS = 16384
    original_max_tokens = body.get("max_tokens")
    if original_max_tokens is None:
        body["max_tokens"] = DEFAULT_MAX_TOKENS
        logger.info(f"[{request_id}] 设置默认 max_tokens: {DEFAULT_MAX_TOKENS}")
    elif original_max_tokens < 1000:
        logger.warning(f"[{request_id}] max_tokens 较小 ({original_max_tokens})，可能导致响应截断")

    final_max_tokens = body.get("max_tokens")

    # ==================== 智能模型路由 ====================
    routed_model, route_reason = await model_router.route(body)

    if routed_model != original_model:
        logger.info(f"[{request_id}] 🔀 模型路由: {original_model} -> {routed_model} ({route_reason})")
        body["model"] = routed_model
        model = routed_model
    else:
        model = original_model
        if "opus" in original_model.lower():
            logger.info(f"[{request_id}] ✅ 保留 Opus: {route_reason}")

    # ==================== Session ID 生成（防止串会话）====================
    messages = body.get("messages", [])

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

    # ==================== 历史消息管理 ====================
    manager = HistoryManager(HISTORY_CONFIG, cache_key=session_id)
    user_content = extract_user_content(messages)

    original_chars = len(json.dumps(messages, ensure_ascii=False))
    logger.info(f"[{request_id}] 原始消息: {len(messages)} 条, {original_chars} 字符")

    should_summarize = manager.should_summarize(messages)
    logger.info(f"[{request_id}] 需要摘要: {should_summarize}, 阈值: {HISTORY_CONFIG.summary_threshold}")

    # ==================== 异步摘要优化 ====================
    cache_info = {"hit": False, "original_tokens": 0, "cached_tokens": 0, "saved_tokens": 0}

    # 为了异步摘要能调用 API，我们需要一个 lambda
    # 这里定义一个调用 kiro 生成摘要的函数
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
            cache_info = async_summary_manager.get_cache_info(session_id)
            cached_processed = async_summary_manager.get_cached_processed_messages(session_id)
            if cached_processed:
                logger.info(f"[{request_id}] ⚡ 使用缓存摘要 (节省 {cache_info['saved_tokens']} tokens)")
                processed_messages = cached_processed
                if async_summary_manager.should_update_summary(session_id, len(messages)):
                    await async_summary_manager.schedule_summary_task(
                        session_id, messages, manager, user_content, call_kiro_for_summary
                    )
            else:
                processed_messages = manager.pre_process(messages, user_content)
                cache_info = {"hit": False, "original_tokens": 0, "cached_tokens": 0, "saved_tokens": 0}
        elif ASYNC_SUMMARY_CONFIG.get("fast_first_request", True):
            logger.info(f"[{request_id}] ⚡ 首次请求，使用简单截断")
            processed_messages = manager.pre_process(messages, user_content)
            await async_summary_manager.schedule_summary_task(
                session_id, messages, manager, user_content, call_kiro_for_summary
            )
        else:
            logger.info(f"[{request_id}] 触发同步智能摘要...")
            processed_messages = await manager.pre_process_async(messages, user_content, call_kiro_for_summary)
    elif should_summarize:
        logger.info(f"[{request_id}] 触发同步智能摘要...")
        processed_messages = await manager.pre_process_async(messages, user_content, call_kiro_for_summary)
        if CONTEXT_ENHANCEMENT_CONFIG["integrate_with_summary"]:
            logger.info(f"[{request_id}] 🔄 摘要触发，同步更新项目上下文...")
            context = await extract_project_context(messages, session_id, http_client_getter)
            if context:
                user_message_count = count_user_messages(messages)
                from app.services.context import update_session_context
                update_session_context(session_id, context, user_message_count)
    else:
        processed_messages = manager.pre_process(messages, user_content)

    if manager.was_truncated:
        logger.info(f"[{request_id}] ✂️ {manager.truncate_info}")
    else:
        logger.info(f"[{request_id}] 无需截断")

    body["messages"] = processed_messages
    openai_body = convert_anthropic_to_openai(body)

    final_msg_count = len(openai_body.get("messages", []))
    total_chars = sum(len(str(m.get("content", ""))) for m in openai_body.get("messages", []))
    tools_count = len(openai_body.get("tools", []))
    tools_mode = "原生" if tools_count > 0 and NATIVE_TOOLS_ENABLED else ("文本注入" if body.get("tools") else "无")

    logger.info(f"[{request_id}] Anthropic -> OpenAI: model={model}, stream={stream}, "
                f"msgs={orig_msg_count}->{final_msg_count}, chars={total_chars}, max_tokens={final_max_tokens}, "
                f"tools={tools_count}({tools_mode})")

    # 构建请求头
    headers = {
        "Authorization": f"Bearer {KIRO_API_KEY}",
        "Content-Type": "application/json",
        "X-Request-ID": f"req_{request_id}_{uuid.uuid4().hex[:8]}",
        "X-Trace-ID": f"trace_{uuid.uuid4().hex}",
        "X-Client-ID": f"client_{uuid.uuid4().hex[:12]}",
    }

    if stream:
        return await handle_anthropic_stream_via_openai(openai_body, headers, request_id, model, cache_info)
    else:
        return await handle_anthropic_non_stream_via_openai(openai_body, headers, request_id, model, cache_info)
