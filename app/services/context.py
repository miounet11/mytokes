import re
import uuid
import time
import logging
import json
import hashlib
from typing import List, Dict, Any, Optional
from app.core.config import (
    CONTEXT_ENHANCEMENT_CONFIG, KIRO_API_KEY, KIRO_PROXY_URL, logger
)
from app.services.managers import async_context_manager

# Session 上下文存储（内存）
_session_contexts = {}

def get_session_context(session_id: str) -> dict:
    """获取 session 的项目上下文"""
    return _session_contexts.get(session_id, {
        "content": "",
        "last_updated_at": 0,
        "message_count_at_update": 0,
        "version": 0,
    })

def update_session_context(session_id: str, context: str, message_count: int):
    """更新 session 的项目上下文"""
    _session_contexts[session_id] = {
        "content": context,
        "last_updated_at": time.time(),
        "message_count_at_update": message_count,
        "version": _session_contexts.get(session_id, {}).get("version", 0) + 1,
    }

def generate_session_id(
    messages: List[dict],
    client_id: str = None,
    conversation_id: str = None
) -> str:
    """生成会话 ID - 优先使用客户端标识，避免 session 串"""

    # 优先级 1: 使用客户端传递的 conversation_id（最可靠）
    if conversation_id:
        return f"conv_{hashlib.md5(conversation_id.encode()).hexdigest()[:16]}"

    # 优先级 2: 使用 client_id + 消息内容哈希
    content_parts = []

    # 加入 client_id 作为隔离因子
    if client_id:
        content_parts.append(f"client:{client_id}")

    # 使用更多消息内容（前5条，每条前200字符）
    for msg in messages[:5]:
        content = msg.get("content", "")
        if isinstance(content, str):
            content_parts.append(content[:200])
        elif isinstance(content, list):
            # 处理复杂内容结构
            for item in content[:3]:
                if isinstance(item, dict):
                    text = item.get("text", "") or item.get("content", "")
                    if isinstance(text, str):
                        content_parts.append(text[:100])

    if content_parts:
        # 使用 SHA256 更安全，取前20位减少碰撞
        hash_input = "|".join(content_parts)
        return hashlib.sha256(hash_input.encode()).hexdigest()[:20]

    # 兜底：使用随机 ID（每次请求独立，不共享缓存）
    return f"rand_{uuid.uuid4().hex[:16]}"

def extract_user_content(messages: List[dict]) -> str:
    """提取最后一条用户消息"""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                return content
    return ""

async def extract_project_context(messages: List[dict], session_id: str, http_client_getter) -> str:
    """从对话历史中提取项目上下文"""
    if not CONTEXT_ENHANCEMENT_CONFIG["enabled"]:
        return ""

    if not messages:
        return ""

    conversation_history = []
    for msg in messages[-20:]:
        role = msg.get("role", "")
        content = msg.get("content", "")

        if isinstance(content, list):
            content_str = ""
            for item in content:
                if isinstance(item, dict):
                    if item.get("type") == "text":
                        content_str += item.get("text", "")
                    elif item.get("type") == "tool_use":
                        content_str += f"[Tool: {item.get('name', 'unknown')}]"
                    elif item.get("type") == "tool_result":
                        content_str += "[Tool Result]"
            content = content_str

        if isinstance(content, str) and content.strip():
            if len(content) > 500:
                content = content[:500] + "..."
            conversation_history.append(f"{role}: {content}")

    if not conversation_history:
        return ""

    prompt = CONTEXT_ENHANCEMENT_CONFIG["extraction_prompt"].format(
        conversation_history="\n".join(conversation_history)
    )

    context_id = uuid.uuid4().hex[:8]
    request_body = {
        "model": CONTEXT_ENHANCEMENT_CONFIG["model"],
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "max_tokens": CONTEXT_ENHANCEMENT_CONFIG["max_tokens"] + 50,
    }

    headers = {
        "Authorization": f"Bearer {KIRO_API_KEY}",
        "Content-Type": "application/json",
        "X-Request-ID": f"context_{context_id}",
        "X-Trace-ID": f"trace_{uuid.uuid4().hex}",
    }

    try:
        client = http_client_getter()
        response = await client.post(
            KIRO_PROXY_URL,
            json=request_body,
            headers=headers,
            timeout=30.0,
        )

        if response.status_code == 200:
            result = response.json()
            context = result.get("choices", [{}])[0].get("message", {}).get("content", "").strip()

            if len(context) > CONTEXT_ENHANCEMENT_CONFIG["max_tokens"] * 4:
                context = context[:CONTEXT_ENHANCEMENT_CONFIG["max_tokens"] * 4]

            logger.info(f"[{context_id}] ✅ 上下文提取成功: {len(context)} chars")
            return context
        else:
            logger.error(f"[{context_id}] 上下文提取失败: {response.status_code}")
            return ""

    except Exception as e:
        logger.error(f"[{context_id}] 上下文提取异常: {e}")
        return ""

def count_user_messages(messages: List[dict]) -> int:
    """统计用户消息数量"""
    return sum(1 for msg in messages if msg.get("role") == "user")

async def enhance_user_message(messages: List[dict], session_id: str, http_client_getter) -> List[dict]:
    """增强用户消息（在最后一条用户消息中注入项目上下文）"""
    if not CONTEXT_ENHANCEMENT_CONFIG["enabled"]:
        return messages

    if not messages or messages[-1].get("role") != "user":
        return messages

    user_message_count = count_user_messages(messages)
    context, has_cache = async_context_manager.get_cached_context(session_id)
    
    should_update = async_context_manager.should_update_context(session_id, user_message_count)

    if should_update:
        logger.info(f"[{session_id[:8]}] 🔄 调度后台上下文提取")
        # 注意：这里需要传入一个能够提取上下文的 lambda
        async def extract_func(msgs, sid):
            return await extract_project_context(msgs, sid, http_client_getter)
        await async_context_manager.schedule_context_task(session_id, messages, user_message_count, extract_func)

    if not context:
        return messages

    enhanced_messages = messages.copy()
    last_message = enhanced_messages[-1].copy()
    original_content = last_message.get("content", "")

    if isinstance(original_content, list):
        enhanced_content = []
        text_enhanced = False
        for item in original_content:
            if isinstance(item, dict) and item.get("type") == "text" and not text_enhanced:
                enhanced_text = CONTEXT_ENHANCEMENT_CONFIG["enhancement_template"].format(
                    context=context,
                    user_input=item.get("text", "")
                )
                enhanced_content.append({"type": "text", "text": enhanced_text})
                text_enhanced = True
            else:
                enhanced_content.append(item)
        last_message["content"] = enhanced_content
    elif isinstance(original_content, str):
        enhanced_text = CONTEXT_ENHANCEMENT_CONFIG["enhancement_template"].format(
            context=context,
            user_input=original_content
        )
        last_message["content"] = enhanced_text

    enhanced_messages[-1] = last_message
    return enhanced_messages
