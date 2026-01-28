"""AI History Manager API 服务

提供 OpenAI 兼容的 API 接口，集成历史消息管理功能。
可接入 NewAPI 作为自定义渠道使用。

启动方式:
    uvicorn api_server:app --host 0.0.0.0 --port 8100

NewAPI 配置:
    - 类型: 自定义渠道
    - Base URL: http://your-server:8100
    - 模型: 按需配置
"""

import json
import time
import uuid
import asyncio
import logging
from typing import Optional, AsyncIterator

import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel

from ai_history_manager import HistoryManager, HistoryConfig, TruncateStrategy
from ai_history_manager.utils import is_content_length_error

# ==================== 配置 ====================

# Kiro 代理地址 (tokens 网关, 使用内网地址)
KIRO_PROXY_BASE = "http://127.0.0.1:8000"
# OpenAI 兼容端点 (Kiro 渠道)
KIRO_PROXY_URL = f"{KIRO_PROXY_BASE}/kiro/v1/chat/completions"
KIRO_MODELS_URL = f"{KIRO_PROXY_BASE}/kiro/v1/models"
KIRO_API_KEY = "dba22273-65d3-4dc1-8ce9-182f680b2bf5"

# 历史消息管理配置
# 调整阈值，更早触发截断以避免 "Input is too long" 错误
HISTORY_CONFIG = HistoryConfig(
    strategies=[
        TruncateStrategy.PRE_ESTIMATE,      # 优先预估，提前截断
        TruncateStrategy.AUTO_TRUNCATE,     # 自动截断
        TruncateStrategy.SMART_SUMMARY,     # 智能摘要
        TruncateStrategy.ERROR_RETRY,       # 错误重试
    ],
    max_messages=25,           # 30 → 25，减少最大消息数
    max_chars=100000,          # 150000 → 100000，降低字符上限
    summary_keep_recent=8,     # 10 → 8，保留更少的最近消息
    summary_threshold=80000,   # 100000 → 80000，更早触发摘要
    retry_max_messages=15,     # 20 → 15，重试时保留更少消息
    max_retries=3,             # 2 → 3，增加重试次数
    estimate_threshold=100000, # 180000 → 100000，更早预估截断
    summary_cache_enabled=True,
    add_warning_header=True,
)

# 服务配置
SERVICE_PORT = 8100
REQUEST_TIMEOUT = 300

# ==================== 日志 ====================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("ai_history_manager_api")

# ==================== FastAPI App ====================

app = FastAPI(
    title="AI History Manager API",
    description="OpenAI 兼容 API，集成智能历史消息管理",
    version="1.0.0",
)


# ==================== 数据模型 ====================

class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[dict]
    stream: Optional[bool] = False
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    top_p: Optional[float] = None
    frequency_penalty: Optional[float] = None
    presence_penalty: Optional[float] = None
    stop: Optional[list[str]] = None


# ==================== 辅助函数 ====================

def generate_session_id(messages: list[dict]) -> str:
    """基于消息内容生成会话 ID"""
    if not messages:
        return "default"

    content_parts = []
    for msg in messages[:3]:
        content = msg.get("content", "")
        if isinstance(content, str):
            content_parts.append(content[:100])

    if content_parts:
        import hashlib
        return hashlib.md5("".join(content_parts).encode()).hexdigest()[:16]

    return "default"


def extract_user_content(messages: list[dict]) -> str:
    """提取最后一条用户消息"""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                return content
    return ""


async def call_kiro_for_summary(prompt: str) -> str:
    """调用 Kiro API 生成摘要"""
    request_body = {
        "model": "claude-haiku-4",  # 使用快速模型
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "max_tokens": 2000,
    }

    headers = {
        "Authorization": f"Bearer {KIRO_API_KEY}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                KIRO_PROXY_URL,
                json=request_body,
                headers=headers,
            )

            if response.status_code == 200:
                data = response.json()
                return data.get("choices", [{}])[0].get("message", {}).get("content", "")
    except Exception as e:
        logger.warning(f"摘要生成失败: {e}")

    return ""


# ==================== API 端点 ====================

@app.get("/")
async def root():
    """健康检查"""
    return {
        "status": "ok",
        "service": "AI History Manager API",
        "version": "1.0.0",
    }


@app.get("/v1/models")
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


@app.post("/v1/messages/count_tokens")
async def count_tokens(request: Request):
    """Token 计数端点 (简化实现)"""
    try:
        body = await request.json()
        # 简单估算: 约 4 字符 = 1 token
        total_chars = 0

        # 计算 system
        system = body.get("system", "")
        if isinstance(system, str):
            total_chars += len(system)
        elif isinstance(system, list):
            for item in system:
                if isinstance(item, dict) and "text" in item:
                    total_chars += len(item["text"])

        # 计算 messages
        for msg in body.get("messages", []):
            content = msg.get("content", "")
            if isinstance(content, str):
                total_chars += len(content)
            elif isinstance(content, list):
                for item in content:
                    if isinstance(item, dict):
                        if item.get("type") == "text":
                            total_chars += len(item.get("text", ""))

        # 计算 tools
        tools = body.get("tools", [])
        for tool in tools:
            total_chars += len(json.dumps(tool))

        estimated_tokens = total_chars // 4

        return {"input_tokens": estimated_tokens}
    except Exception as e:
        return JSONResponse(
            status_code=400,
            content={"error": {"type": "invalid_request_error", "message": str(e)}}
        )


# ==================== Anthropic -> OpenAI 转换 ====================

def extract_content_item(item: dict) -> str:
    """提取单个 content item 的文本表示

    支持的类型：
    - text: 纯文本
    - image: 图像（base64/URL）
    - document: 文档（PDF等）
    - file: 文件
    - tool_use: 工具调用
    - tool_result: 工具结果
    - thinking: 思考内容
    - code_execution_result: 代码执行结果
    - citation: 引用
    - redacted_thinking: 隐藏的思考
    """
    item_type = item.get("type", "")

    if item_type == "text":
        return item.get("text", "")

    elif item_type == "image":
        # 图像内容 - 提取描述或标记
        source = item.get("source", {})
        if source.get("type") == "base64":
            media_type = source.get("media_type", "image")
            return f"[Image: {media_type}]"
        elif source.get("type") == "url":
            url = source.get("url", "")
            return f"[Image: {url[:50]}...]" if len(url) > 50 else f"[Image: {url}]"
        return "[Image]"

    elif item_type == "document":
        # 文档内容（PDF等）
        source = item.get("source", {})
        doc_type = source.get("media_type", "document")
        doc_name = item.get("name", "document")

        # 提取文档文本内容（如果有）
        if "text" in item:
            return f"[Document: {doc_name}]\n{item.get('text', '')}"

        # 如果有 content 字段（某些 API 版本）
        if "content" in item:
            doc_content = item.get("content", "")
            if isinstance(doc_content, str):
                return f"[Document: {doc_name}]\n{doc_content}"

        return f"[Document: {doc_name} ({doc_type})]"

    elif item_type == "file":
        # 文件内容
        file_name = item.get("name", item.get("filename", "file"))
        file_type = item.get("media_type", "")
        file_content = item.get("content", "")

        if file_content:
            if isinstance(file_content, str):
                return f"[File: {file_name}]\n{file_content}"
            elif isinstance(file_content, list):
                content_text = "\n".join(
                    extract_content_item(c) if isinstance(c, dict) else str(c)
                    for c in file_content
                )
                return f"[File: {file_name}]\n{content_text}"

        return f"[File: {file_name}]" + (f" ({file_type})" if file_type else "")

    elif item_type == "tool_result":
        # 工具结果
        tool_id = item.get("tool_use_id", "")
        tool_content = item.get("content", "")
        is_error = item.get("is_error", False)

        # 处理 content 可能是列表的情况
        if isinstance(tool_content, list):
            tool_content = "\n".join(
                extract_content_item(c) if isinstance(c, dict) else str(c)
                for c in tool_content
            )
        elif isinstance(tool_content, dict):
            tool_content = extract_content_item(tool_content)

        prefix = "[Tool Error]" if is_error else "[Tool Result]"
        return f"{prefix}\n{tool_content}" if tool_content else prefix

    elif item_type == "thinking":
        # 思考内容 - 不使用 <thinking> 标签（Kiro API 不支持）
        # 直接返回思考内容，或者完全跳过
        thinking_text = item.get("thinking", "")
        # 跳过思考内容，避免 Kiro API 报错
        return ""

    elif item_type == "redacted_thinking":
        # 隐藏的思考内容（跳过）
        return ""

    elif item_type == "signature":
        # 签名（用于扩展思考，跳过）
        return ""

    elif item_type == "code_execution_result":
        # 代码执行结果
        output = item.get("output", "")
        return_code = item.get("return_code", 0)
        if return_code != 0:
            return f"[Code Execution Error (exit={return_code})]\n{output}"
        return f"[Code Execution Result]\n{output}" if output else ""

    elif item_type == "citation":
        # 引用
        cited_text = item.get("cited_text", "")
        source_name = item.get("source", {}).get("name", "source")
        return f"[Citation from {source_name}]: {cited_text}" if cited_text else ""

    elif item_type == "video":
        # 视频
        source = item.get("source", {})
        return f"[Video: {source.get('url', 'embedded')}]"

    elif item_type == "audio":
        # 音频
        source = item.get("source", {})
        return f"[Audio: {source.get('url', 'embedded')}]"

    else:
        # 未知类型 - 尝试提取文本或返回类型标记
        if "text" in item:
            return item.get("text", "")
        if "content" in item:
            content = item.get("content", "")
            if isinstance(content, str):
                return content
        # 返回类型标记而非空
        return f"[{item_type}]" if item_type else ""


def clean_system_content(content: str) -> str:
    """清理 system 消息内容

    移除不应该出现在 system prompt 中的内容：
    - HTTP header 格式的内容（如 x-anthropic-billing-header）
    - 其他元数据
    """
    if not content:
        return content

    lines = content.split('\n')
    cleaned_lines = []

    for line in lines:
        # 跳过 HTTP header 格式的行 (key: value)
        if ':' in line:
            key = line.split(':')[0].strip().lower()
            # 跳过已知的 header 类型
            if key.startswith('x-') or key in [
                'content-type', 'authorization', 'user-agent',
                'accept', 'cache-control', 'cookie'
            ]:
                continue
        cleaned_lines.append(line)

    return '\n'.join(cleaned_lines).strip()


def clean_assistant_content(content: str) -> str:
    """清理 assistant 消息内容

    移除格式化标记：
    - (no content)
    - [Calling tool: xxx]
    - <thinking>...</thinking> 标签（Kiro API 不支持）
    """
    if not content:
        return content

    import re

    # 移除 (no content) 标记
    content = content.replace("(no content)", "").strip()

    # 不再移除 [Calling tool: xxx] 标记，因为我们使用这个格式来内联工具调用

    # 移除 <thinking>...</thinking> 标签（Kiro API 不支持）
    # 保留标签内的内容，但移除标签本身
    content = re.sub(r'<thinking>(.*?)</thinking>', r'\1', content, flags=re.DOTALL)

    # 移除未闭合的 <thinking> 标签
    content = re.sub(r'<thinking>.*$', '', content, flags=re.DOTALL)
    content = re.sub(r'^.*</thinking>', '', content, flags=re.DOTALL)

    # 移除 <redacted_thinking> 相关标签
    content = re.sub(r'<redacted_thinking>.*?</redacted_thinking>', '', content, flags=re.DOTALL)

    # 移除其他可能的 Claude 特有标签
    content = re.sub(r'<signature>.*?</signature>', '', content, flags=re.DOTALL)

    return content.strip() if content.strip() else " "


def convert_anthropic_to_openai(anthropic_body: dict) -> dict:
    """将 Anthropic 请求转换为 OpenAI 格式

    处理 Claude Code 发送的完整 Anthropic 格式请求，包括：
    - system 消息（字符串或列表，支持缓存控制）
    - messages 消息列表（支持多模态内容）
    - tools 工具定义
    - tool_choice 工具选择
    - thinking/extended thinking 相关字段
    - 图像、文档、文件等多媒体内容

    同时包含截断保护和空消息过滤
    """
    # 截断配置
    MAX_MESSAGES = 30           # 最大消息数（不含 system）
    MAX_TOTAL_CHARS = 100000    # 最大总字符数
    MAX_SINGLE_CONTENT = 40000  # 单条消息最大字符数

    messages = []

    # 处理 system 消息
    system = anthropic_body.get("system", "")
    if system:
        if isinstance(system, str):
            system_content = clean_system_content(system)
        elif isinstance(system, list):
            # Anthropic 允许 system 为列表格式（支持缓存控制等）
            system_parts = []
            for item in system:
                if isinstance(item, dict):
                    extracted = extract_content_item(item)
                    if extracted:
                        system_parts.append(extracted)
                else:
                    system_parts.append(str(item))
            system_content = clean_system_content("\n".join(filter(None, system_parts)))
        else:
            system_content = clean_system_content(str(system))

        if system_content.strip():
            # 截断过长的 system 消息
            if len(system_content) > MAX_SINGLE_CONTENT:
                system_content = system_content[:MAX_SINGLE_CONTENT] + "\n...[truncated]"
            messages.append({"role": "system", "content": system_content})

    # 获取原始消息并截断
    raw_messages = anthropic_body.get("messages", [])
    if len(raw_messages) > MAX_MESSAGES:
        raw_messages = raw_messages[-MAX_MESSAGES:]

    # 转换 messages
    converted_messages = []
    for msg in raw_messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")

        # 处理 content 为列表的情况 (多模态/工具调用)
        if isinstance(content, list):
            text_parts = []

            for item in content:
                if isinstance(item, dict):
                    item_type = item.get("type", "")

                    if item_type == "tool_use":
                        # 将 tool_use 转为文本描述
                        # tokens 网关不完整支持 OpenAI tool_calls → Kiro toolUses
                        tool_name = item.get("name", "unknown")
                        tool_input = item.get("input", {})
                        input_str = json.dumps(tool_input, ensure_ascii=False)
                        if len(input_str) > 5000:
                            input_str = input_str[:5000] + "...[truncated]"
                        text_parts.append(f"[Calling tool: {tool_name}]\nInput: {input_str}")
                    elif item_type == "tool_result":
                        # 将 tool_result 转为文本描述
                        tool_content = item.get("content", "")
                        is_error = item.get("is_error", False)

                        # 处理 content 可能是列表的情况
                        if isinstance(tool_content, list):
                            parts = []
                            for c in tool_content:
                                if isinstance(c, dict):
                                    if c.get("type") == "text":
                                        parts.append(c.get("text", ""))
                                    else:
                                        extracted = extract_content_item(c)
                                        if extracted:
                                            parts.append(extracted)
                                else:
                                    parts.append(str(c))
                            tool_content = "\n".join(filter(None, parts))
                        elif isinstance(tool_content, dict):
                            tool_content = extract_content_item(tool_content)

                        if not tool_content:
                            tool_content = "Error" if is_error else "OK"

                        prefix = "[Tool Error]" if is_error else "[Tool Result]"
                        # 截断过长工具结果
                        if len(tool_content) > MAX_SINGLE_CONTENT:
                            tool_content = tool_content[:MAX_SINGLE_CONTENT] + "\n...[truncated]"
                        text_parts.append(f"{prefix}\n{tool_content}")
                    else:
                        # 使用通用提取函数处理其他类型
                        extracted = extract_content_item(item)
                        if extracted:
                            text_parts.append(extracted)
                else:
                    text_parts.append(str(item))

            content = "\n".join(filter(None, text_parts))

            # 清理 assistant 消息中的格式化标记
            if role == "assistant":
                content = clean_assistant_content(content)

            # 截断过长内容
            if len(content) > MAX_SINGLE_CONTENT:
                content = content[:MAX_SINGLE_CONTENT] + "\n...[truncated]"

            # 添加消息（简单的 role + content，不再使用 tool_calls/role="tool"）
            if content.strip():
                converted_messages.append({
                    "role": role,
                    "content": content
                })
            elif role == "assistant":
                # assistant 消息即使没有文本也需要占位
                converted_messages.append({
                    "role": "assistant",
                    "content": "I understand."
                })
        else:
            # content 是字符串
            if role == "assistant":
                content = clean_assistant_content(content)

            # 截断过长内容
            if len(content) > MAX_SINGLE_CONTENT:
                content = content[:MAX_SINGLE_CONTENT] + "\n...[truncated]"

            # 跳过空消息
            if content.strip():
                converted_messages.append({
                    "role": role,
                    "content": content
                })

    # 合并连续同角色消息
    merged_messages = []
    for msg in converted_messages:
        role = msg.get("role")
        if merged_messages and merged_messages[-1].get("role") == role:
            # 合并内容
            merged_messages[-1]["content"] += "\n" + msg.get("content", "")
        else:
            merged_messages.append(msg.copy())

    # 确保消息顺序正确
    final_messages = merged_messages

    # 添加到主消息列表
    messages.extend(final_messages)

    # 确保至少有一条消息
    if not messages:
        messages.append({"role": "user", "content": "Hello"})

    # 确保最后一条不是 system
    if len(messages) == 1 and messages[0]["role"] == "system":
        messages.append({"role": "user", "content": "Hello"})

    # 关键修复：确保最后一条消息不是 role="tool"
    # Kiro API 需要最后一条是 user 消息
    if messages and messages[-1].get("role") == "tool":
        messages.append({"role": "user", "content": "Please continue based on the tool results above."})

    # 确保消息不以 assistant 结尾（Kiro 需要 user 结尾）
    if messages and messages[-1].get("role") == "assistant":
        messages.append({"role": "user", "content": "Please continue."})

    # 检查总字符数，如果超过则进一步截断
    total_chars = sum(len(m.get("content", "")) for m in messages)
    while total_chars > MAX_TOTAL_CHARS and len(messages) > 2:
        if messages[0].get("role") == "system":
            if len(messages) > 2:
                messages.pop(1)
        else:
            messages.pop(0)
        total_chars = sum(len(m.get("content", "")) for m in messages)

    # 构建 OpenAI 请求
    openai_body = {
        "model": anthropic_body.get("model", "claude-sonnet-4"),
        "messages": messages,
        "stream": anthropic_body.get("stream", False),
    }

    # 转换参数
    if "max_tokens" in anthropic_body:
        openai_body["max_tokens"] = anthropic_body["max_tokens"]
    if "temperature" in anthropic_body:
        openai_body["temperature"] = anthropic_body["temperature"]
    if "top_p" in anthropic_body:
        openai_body["top_p"] = anthropic_body["top_p"]
    if "stop_sequences" in anthropic_body:
        openai_body["stop"] = anthropic_body["stop_sequences"]

    # 注意：不传递 tools/tool_choice 参数
    # tokens 网关 Kiro 渠道不完整支持 OpenAI tool_calls → Kiro toolUses
    # 工具调用和结果已被内联为文本内容

    return openai_body


def escape_json_string_newlines(json_str: str) -> str:
    """转义 JSON 字符串值中的原始换行符和控制字符

    当模型输出的 JSON 在字符串值中包含未转义的换行符时，
    标准 JSON 解析会失败。此函数将这些控制字符正确转义。
    """
    result = []
    in_string = False
    escape = False
    i = 0

    while i < len(json_str):
        c = json_str[i]

        if escape:
            # 正常的转义序列，保持原样
            result.append(c)
            escape = False
            i += 1
            continue

        if c == '\\':
            result.append(c)
            escape = True
            i += 1
            continue

        if c == '"':
            in_string = not in_string
            result.append(c)
            i += 1
            continue

        if in_string:
            # 在字符串内部，转义控制字符
            if c == '\n':
                result.append('\\n')
            elif c == '\r':
                result.append('\\r')
            elif c == '\t':
                result.append('\\t')
            elif ord(c) < 32:
                # 其他控制字符
                result.append(f'\\u{ord(c):04x}')
            else:
                result.append(c)
        else:
            result.append(c)

        i += 1

    return ''.join(result)


def extract_json_from_position(text: str, start: int) -> tuple[dict, int]:
    """从指定位置提取 JSON 对象，支持任意嵌套深度

    Args:
        text: 源文本
        start: 开始搜索的位置

    Returns:
        (parsed_json, end_position) 或抛出异常
    """
    # 跳过空白找到 '{'
    pos = start
    while pos < len(text) and text[pos] in ' \t\n\r':
        pos += 1

    if pos >= len(text) or text[pos] != '{':
        raise ValueError(f"No JSON object found at position {start}")

    # 使用括号计数来找到匹配的 '}'
    depth = 0
    in_string = False
    escape = False
    json_start = pos

    while pos < len(text):
        c = text[pos]

        if escape:
            escape = False
            pos += 1
            continue

        if c == '\\' and in_string:
            escape = True
            pos += 1
            continue

        if c == '"' and not escape:
            in_string = not in_string
            pos += 1
            continue

        if in_string:
            pos += 1
            continue

        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                json_str = text[json_start:pos + 1]
                try:
                    return json.loads(json_str), pos + 1
                except json.JSONDecodeError as e:
                    # 尝试修复常见问题
                    try:
                        # 1. 移除尾随逗号
                        import re
                        fixed = re.sub(r',\s*}', '}', json_str)
                        fixed = re.sub(r',\s*]', ']', fixed)
                        return json.loads(fixed), pos + 1
                    except json.JSONDecodeError:
                        pass

                    try:
                        # 2. 转义字符串内的控制字符（处理未转义的换行符）
                        fixed = escape_json_string_newlines(json_str)
                        return json.loads(fixed), pos + 1
                    except json.JSONDecodeError:
                        pass

                    try:
                        # 3. 组合修复：先转义控制字符，再移除尾随逗号
                        fixed = escape_json_string_newlines(json_str)
                        fixed = re.sub(r',\s*}', '}', fixed)
                        fixed = re.sub(r',\s*]', ']', fixed)
                        return json.loads(fixed), pos + 1
                    except json.JSONDecodeError:
                        raise e

        pos += 1

    raise ValueError("Incomplete JSON object - no matching '}'")


def parse_inline_tool_calls(text: str) -> tuple[list, str]:
    """解析内联的工具调用文本，转换为 Anthropic tool_use content blocks

    检测格式 (支持多种变体):
    [Calling tool: tool_name]
    Input: {"arg": "value"}

    或者带缩进:
    [Calling tool: tool_name]
      Input: {"arg": "value"}

    Returns:
        (tool_use_blocks, remaining_text)
    """
    import re

    tool_uses = []
    remaining_parts = []

    # 匹配 [Calling tool: xxx]，捕获工具名和位置
    # 然后手动解析后面的 Input: {json}
    tool_pattern = r'\[Calling tool:\s*([^\]]+)\]'

    last_end = 0
    pos = 0

    while pos < len(text):
        match = re.search(tool_pattern, text[pos:])
        if not match:
            break

        match_start = pos + match.start()
        match_end = pos + match.end()

        # 添加匹配前的文本
        before_text = text[last_end:match_start].strip()
        if before_text:
            remaining_parts.append(before_text)

        tool_name = match.group(1).strip()

        # 在工具名后面查找 "Input:" 和 JSON
        # 允许任意空白（空格、换行、制表符）
        after_match = text[match_end:]

        # 匹配 Input: 前的空白和 Input: 本身
        input_pattern = r'^[\s]*Input:\s*'
        input_match = re.match(input_pattern, after_match)

        if input_match:
            json_start_pos = match_end + input_match.end()
            try:
                input_json, json_end_pos = extract_json_from_position(text, json_start_pos)

                # 生成唯一 ID
                tool_id = f"toolu_{uuid.uuid4().hex[:12]}"

                tool_uses.append({
                    "type": "tool_use",
                    "id": tool_id,
                    "name": tool_name,
                    "input": input_json
                })

                last_end = json_end_pos
                pos = json_end_pos
                continue

            except (ValueError, json.JSONDecodeError) as e:
                # JSON 解析失败，尝试更简单的提取
                logger.warning(f"JSON parse failed for tool {tool_name}: {e}")

                # 尝试提取到下一个 [Calling tool: 或文本结尾
                next_tool = re.search(r'\[Calling tool:', after_match[input_match.end():])
                if next_tool:
                    json_text = after_match[input_match.end():input_match.end() + next_tool.start()].strip()
                else:
                    json_text = after_match[input_match.end():].strip()

                # 尝试解析
                if json_text.startswith('{'):
                    try:
                        # 找到最后一个 }
                        brace_count = 0
                        end_pos = 0
                        for i, c in enumerate(json_text):
                            if c == '{':
                                brace_count += 1
                            elif c == '}':
                                brace_count -= 1
                                if brace_count == 0:
                                    end_pos = i + 1
                                    break

                        if end_pos > 0:
                            input_json = json.loads(json_text[:end_pos])
                            tool_id = f"toolu_{uuid.uuid4().hex[:12]}"
                            tool_uses.append({
                                "type": "tool_use",
                                "id": tool_id,
                                "name": tool_name,
                                "input": input_json
                            })
                            last_end = match_end + input_match.end() + end_pos
                            pos = last_end
                            continue
                    except:
                        pass

                # 如果还是失败，作为 raw_input 处理
                tool_id = f"toolu_{uuid.uuid4().hex[:12]}"
                tool_uses.append({
                    "type": "tool_use",
                    "id": tool_id,
                    "name": tool_name,
                    "input": {"_raw": json_text[:500] if json_text else ""}
                })
                last_end = match_end + (input_match.end() if input_match else 0)
                pos = last_end
                continue
        else:
            # 没有 Input:，可能是格式不完整，跳过这个匹配
            pos = match_end
            continue

        pos = match_end

    # 添加最后剩余的文本
    if last_end < len(text):
        after_text = text[last_end:].strip()
        if after_text:
            remaining_parts.append(after_text)

    remaining_text = "\n".join(remaining_parts)
    return tool_uses, remaining_text


def convert_openai_to_anthropic(openai_response: dict, model: str, request_id: str) -> dict:
    """将 OpenAI 响应转换为 Anthropic 格式

    关键增强：检测并转换内联的工具调用为标准 tool_use content blocks
    """
    choice = openai_response.get("choices", [{}])[0]
    message = choice.get("message", {})
    content = message.get("content", "")
    finish_reason = choice.get("finish_reason", "stop")

    # 构建 content blocks
    content_blocks = []
    stop_reason = "end_turn"

    if content:
        # 检测并解析内联的工具调用
        tool_uses, remaining_text = parse_inline_tool_calls(content)

        # 添加文本 content block（如果有剩余文本）
        if remaining_text:
            content_blocks.append({"type": "text", "text": remaining_text})

        # 添加 tool_use content blocks
        if tool_uses:
            content_blocks.extend(tool_uses)
            stop_reason = "tool_use"

    # 如果 OpenAI 返回了 tool_calls（tokens 网关可能在某些情况下返回）
    tool_calls = message.get("tool_calls", [])
    if tool_calls:
        for tc in tool_calls:
            func = tc.get("function", {})
            args_str = func.get("arguments", "{}")
            try:
                args = json.loads(args_str)
            except:
                args = {"raw": args_str}

            content_blocks.append({
                "type": "tool_use",
                "id": tc.get("id", f"toolu_{uuid.uuid4().hex[:12]}"),
                "name": func.get("name", "unknown"),
                "input": args
            })
        stop_reason = "tool_use"

    # 如果没有任何内容，添加空文本
    if not content_blocks:
        content_blocks = [{"type": "text", "text": ""}]

    # 根据 finish_reason 调整 stop_reason
    if finish_reason == "tool_calls":
        stop_reason = "tool_use"
    elif finish_reason == "length":
        stop_reason = "max_tokens"
    elif finish_reason == "stop" and stop_reason != "tool_use":
        stop_reason = "end_turn"

    return {
        "id": f"msg_{request_id}",
        "type": "message",
        "role": "assistant",
        "content": content_blocks,
        "model": model,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": openai_response.get("usage", {}).get("prompt_tokens", 0),
            "output_tokens": openai_response.get("usage", {}).get("completion_tokens", 0),
        }
    }


def convert_anthropic_to_openai_simple(anthropic_body: dict) -> dict:
    """最简单的 Anthropic -> OpenAI 转换，带截断保护"""

    # 截断配置
    MAX_MESSAGES = 20          # 最大消息数（不含 system）
    MAX_TOTAL_CHARS = 80000    # 最大总字符数
    MAX_SINGLE_CONTENT = 30000 # 单条消息最大字符数

    messages = []

    # 处理 system 消息
    system = anthropic_body.get("system", "")
    if system:
        if isinstance(system, str):
            system_content = system
        elif isinstance(system, list):
            parts = []
            for item in system:
                if isinstance(item, dict) and "text" in item:
                    parts.append(item["text"])
                elif isinstance(item, str):
                    parts.append(item)
            system_content = "\n".join(parts)
        else:
            system_content = str(system)

        if system_content.strip():
            # 截断过长的 system 消息
            if len(system_content) > MAX_SINGLE_CONTENT:
                system_content = system_content[:MAX_SINGLE_CONTENT] + "\n...[truncated]"
            messages.append({"role": "system", "content": system_content})

    # 转换 messages
    raw_messages = anthropic_body.get("messages", [])

    # 如果消息太多，只保留最近的
    if len(raw_messages) > MAX_MESSAGES:
        raw_messages = raw_messages[-MAX_MESSAGES:]

    for msg in raw_messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")

        # 处理 content
        if isinstance(content, list):
            text_parts = []
            for item in content:
                if isinstance(item, dict):
                    if item.get("type") == "text":
                        text_parts.append(item.get("text", ""))
                    elif item.get("type") == "tool_result":
                        result_content = item.get("content", "")
                        if isinstance(result_content, str):
                            text_parts.append(result_content)
                        elif isinstance(result_content, list):
                            for rc in result_content:
                                if isinstance(rc, dict) and rc.get("type") == "text":
                                    text_parts.append(rc.get("text", ""))
                elif isinstance(item, str):
                    text_parts.append(item)
            content = "\n".join(filter(None, text_parts))

        # 确保 content 非空
        if not content or not content.strip():
            content = " "

        # 截断过长的单条消息
        if len(content) > MAX_SINGLE_CONTENT:
            content = content[:MAX_SINGLE_CONTENT] + "\n...[truncated]"

        messages.append({"role": role, "content": content})

    # 确保至少有一条消息
    if not messages:
        messages.append({"role": "user", "content": "Hello"})

    # 检查总字符数，如果超过则进一步截断
    total_chars = sum(len(m.get("content", "")) for m in messages)
    while total_chars > MAX_TOTAL_CHARS and len(messages) > 2:
        # 保留 system（如果有）和最后一条消息，删除最早的非 system 消息
        if messages[0].get("role") == "system":
            if len(messages) > 2:
                messages.pop(1)
        else:
            messages.pop(0)
        total_chars = sum(len(m.get("content", "")) for m in messages)

    # 构建 OpenAI 请求
    openai_body = {
        "model": anthropic_body.get("model", "claude-sonnet-4"),
        "messages": messages,
        "stream": anthropic_body.get("stream", False),
    }

    if "max_tokens" in anthropic_body:
        openai_body["max_tokens"] = anthropic_body["max_tokens"]
    if "temperature" in anthropic_body:
        openai_body["temperature"] = anthropic_body["temperature"]

    return openai_body


@app.post("/v1/messages")
async def anthropic_messages(request: Request):
    """Anthropic /v1/messages 端点 - 通过 OpenAI 格式发送到 tokens 网关"""
    request_id = uuid.uuid4().hex[:8]

    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(400, "Invalid JSON")

    model = body.get("model", "claude-sonnet-4")
    stream = body.get("stream", False)
    orig_msg_count = len(body.get("messages", []))

    # 使用完整转换（包含截断和空消息过滤）
    openai_body = convert_anthropic_to_openai(body)

    final_msg_count = len(openai_body.get("messages", []))
    total_chars = sum(len(str(m.get("content", ""))) for m in openai_body.get("messages", []))

    logger.info(f"[{request_id}] Anthropic -> OpenAI: model={model}, stream={stream}, "
                f"msgs={orig_msg_count}->{final_msg_count}, chars={total_chars}")

    # 保存调试文件（仅保留最近几个）
    debug_dir = "/tmp/ai-history-debug"
    import os
    os.makedirs(debug_dir, exist_ok=True)
    try:
        with open(f"{debug_dir}/{request_id}_converted.json", "w") as f:
            json.dump(openai_body, f, indent=2, ensure_ascii=False)
        # 清理旧文件（保留最近 10 个）
        debug_files = sorted(
            [f for f in os.listdir(debug_dir) if f.endswith('.json')],
            key=lambda x: os.path.getmtime(os.path.join(debug_dir, x)),
            reverse=True
        )
        for old_file in debug_files[10:]:
            os.remove(os.path.join(debug_dir, old_file))
    except:
        pass

    headers = {
        "Authorization": f"Bearer {KIRO_API_KEY}",
        "Content-Type": "application/json",
    }

    if stream:
        return await handle_anthropic_stream_via_openai(openai_body, headers, request_id, model)
    else:
        return await handle_anthropic_non_stream_via_openai(openai_body, headers, request_id, model)


async def handle_anthropic_stream_via_openai(
    openai_body: dict,
    headers: dict,
    request_id: str,
    model: str,
) -> StreamingResponse:
    """处理 Anthropic 流式请求 - 通过 OpenAI 格式

    关键增强：检测内联工具调用并转换为标准 tool_use content blocks
    策略：累积完整响应后解析，然后正确发送 content blocks
    """

    async def generate() -> AsyncIterator[bytes]:
        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                async with client.stream(
                    "POST",
                    KIRO_PROXY_URL,
                    json=openai_body,
                    headers=headers,
                ) as response:
                    if response.status_code != 200:
                        error_text = await response.aread()
                        error_str = error_text.decode()
                        logger.error(f"[{request_id}] OpenAI API Error {response.status_code}: {error_str[:200]}")

                        error_response = {
                            "type": "error",
                            "error": {
                                "type": "api_error",
                                "message": error_str[:500],
                            }
                        }
                        yield f"data: {json.dumps(error_response)}\n\n".encode()
                        return

                    # 发送 Anthropic 流式头
                    msg_start = {
                        "type": "message_start",
                        "message": {
                            "id": f"msg_{request_id}",
                            "type": "message",
                            "role": "assistant",
                            "content": [],
                            "model": model,
                            "stop_reason": None,
                            "stop_sequence": None,
                            "usage": {"input_tokens": 0, "output_tokens": 0}
                        }
                    }
                    yield f"data: {json.dumps(msg_start)}\n\n".encode()

                    # 累积完整响应文本，然后解析
                    full_text = ""
                    buffer = ""
                    finish_reason = "end_turn"
                    has_openai_tool_calls = False
                    openai_tool_calls = {}

                    async for chunk in response.aiter_text():
                        buffer += chunk
                        while "\n" in buffer:
                            line, buffer = buffer.split("\n", 1)
                            line = line.strip()
                            if not line or not line.startswith("data:"):
                                continue

                            data_str = line[5:].strip()
                            if data_str == "[DONE]":
                                continue

                            try:
                                data = json.loads(data_str)
                                choice = data.get("choices", [{}])[0]
                                delta = choice.get("delta", {})
                                fr = choice.get("finish_reason")
                                if fr:
                                    if fr == "tool_calls":
                                        finish_reason = "tool_use"
                                    elif fr == "length":
                                        finish_reason = "max_tokens"

                                # 累积文本
                                content = delta.get("content", "")
                                if content:
                                    full_text += content

                                # 处理 OpenAI 原生 tool_calls（如果有）
                                tool_calls = delta.get("tool_calls", [])
                                for tc in tool_calls:
                                    tc_index = tc.get("index", 0)
                                    tc_id = tc.get("id")
                                    tc_func = tc.get("function", {})

                                    if tc_id:
                                        has_openai_tool_calls = True
                                        openai_tool_calls[tc_index] = {
                                            "id": tc_id,
                                            "name": tc_func.get("name", ""),
                                            "arguments": ""
                                        }

                                    if tc_index in openai_tool_calls and tc_func.get("arguments"):
                                        openai_tool_calls[tc_index]["arguments"] += tc_func["arguments"]

                            except json.JSONDecodeError:
                                pass

                    # 解析内联工具调用
                    tool_uses, remaining_text = parse_inline_tool_calls(full_text)

                    # 添加 OpenAI 原生工具调用
                    for tc_data in openai_tool_calls.values():
                        try:
                            args = json.loads(tc_data["arguments"])
                        except:
                            args = {"raw": tc_data["arguments"]}
                        tool_uses.append({
                            "type": "tool_use",
                            "id": tc_data["id"],
                            "name": tc_data["name"],
                            "input": args
                        })

                    # 发送 content blocks
                    block_index = 0

                    # 1. 发送文本 content block（如果有）
                    if remaining_text:
                        yield f'data: {{"type":"content_block_start","index":{block_index},"content_block":{{"type":"text","text":""}}}}\n\n'.encode()
                        yield f'data: {{"type":"content_block_delta","index":{block_index},"delta":{{"type":"text_delta","text":{json.dumps(remaining_text)}}}}}\n\n'.encode()
                        yield f'data: {{"type":"content_block_stop","index":{block_index}}}\n\n'.encode()
                        block_index += 1
                    elif not tool_uses:
                        # 没有文本也没有工具，发送空文本
                        yield f'data: {{"type":"content_block_start","index":0,"content_block":{{"type":"text","text":""}}}}\n\n'.encode()
                        yield f'data: {{"type":"content_block_stop","index":0}}\n\n'.encode()
                        block_index = 1

                    # 2. 发送 tool_use content blocks
                    if tool_uses:
                        finish_reason = "tool_use"
                        for tool_use in tool_uses:
                            # content_block_start
                            tool_start = {
                                "type": "content_block_start",
                                "index": block_index,
                                "content_block": {
                                    "type": "tool_use",
                                    "id": tool_use["id"],
                                    "name": tool_use["name"],
                                    "input": {}
                                }
                            }
                            yield f"data: {json.dumps(tool_start)}\n\n".encode()

                            # input_json_delta
                            input_json = json.dumps(tool_use["input"])
                            yield f'data: {{"type":"content_block_delta","index":{block_index},"delta":{{"type":"input_json_delta","partial_json":{json.dumps(input_json)}}}}}\n\n'.encode()

                            # content_block_stop
                            yield f'data: {{"type":"content_block_stop","index":{block_index}}}\n\n'.encode()
                            block_index += 1

                    # message delta
                    yield f'data: {{"type":"message_delta","delta":{{"stop_reason":"{finish_reason}","stop_sequence":null}},"usage":{{"output_tokens":0}}}}\n\n'.encode()

                    # message stop
                    yield f'data: {{"type":"message_stop"}}\n\n'.encode()

        except httpx.TimeoutException:
            logger.error(f"[{request_id}] 请求超时")
            error_response = {
                "type": "error",
                "error": {"type": "timeout_error", "message": "Request timeout"}
            }
            yield f"data: {json.dumps(error_response)}\n\n".encode()
        except Exception as e:
            logger.error(f"[{request_id}] 请求异常: {e}")
            error_response = {
                "type": "error",
                "error": {"type": "api_error", "message": str(e)}
            }
            yield f"data: {json.dumps(error_response)}\n\n".encode()

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
    )


async def handle_anthropic_non_stream_via_openai(
    openai_body: dict,
    headers: dict,
    request_id: str,
    model: str,
) -> JSONResponse:
    """处理 Anthropic 非流式请求 - 通过 OpenAI 格式"""
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            response = await client.post(
                KIRO_PROXY_URL,
                json=openai_body,
                headers=headers,
            )

            if response.status_code != 200:
                error_str = response.text
                logger.error(f"[{request_id}] OpenAI API Error {response.status_code}: {error_str[:200]}")

                return JSONResponse(
                    status_code=response.status_code,
                    content={
                        "type": "error",
                        "error": {
                            "type": "api_error",
                            "message": error_str[:500],
                        }
                    }
                )

            # 转换 OpenAI 响应为 Anthropic 格式
            openai_response = response.json()
            anthropic_response = convert_openai_to_anthropic(openai_response, model, request_id)
            return JSONResponse(content=anthropic_response)

    except httpx.TimeoutException:
        logger.error(f"[{request_id}] 请求超时")
        return JSONResponse(
            status_code=408,
            content={
                "type": "error",
                "error": {"type": "timeout_error", "message": "Request timeout"}
            }
        )
    except Exception as e:
        logger.error(f"[{request_id}] 请求异常: {e}")
        return JSONResponse(
            status_code=500,
            content={
                "type": "error",
                "error": {"type": "api_error", "message": str(e)}
            }
        )


# ==================== OpenAI API ====================

@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """聊天完成接口 - OpenAI 兼容"""
    start_time = time.time()
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

    # 创建历史管理器
    session_id = generate_session_id(messages)
    manager = HistoryManager(HISTORY_CONFIG, cache_key=session_id)

    # 预处理消息
    user_content = extract_user_content(messages)

    if manager.should_summarize(messages):
        processed_messages = await manager.pre_process_async(
            messages, user_content, call_kiro_for_summary
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

    # 传递其他参数
    for key in ["temperature", "max_tokens", "top_p", "frequency_penalty", "presence_penalty", "stop"]:
        if key in body and body[key] is not None:
            kiro_request[key] = body[key]

    headers = {
        "Authorization": f"Bearer {KIRO_API_KEY}",
        "Content-Type": "application/json",
    }

    if stream:
        return await handle_stream(kiro_request, headers, manager, request_id, messages)
    else:
        return await handle_non_stream(kiro_request, headers, manager, request_id, messages)


async def handle_stream(
    kiro_request: dict,
    headers: dict,
    manager: HistoryManager,
    request_id: str,
    original_messages: list,
) -> StreamingResponse:
    """处理流式响应"""

    async def generate() -> AsyncIterator[bytes]:
        nonlocal kiro_request
        retry_count = 0
        max_retries = HISTORY_CONFIG.max_retries

        while retry_count <= max_retries:
            try:
                async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
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

                            # 检查是否为长度错误
                            if is_content_length_error(response.status_code, error_str):
                                logger.info(f"[{request_id}] 检测到长度错误，尝试截断重试")

                                truncated, should_retry = await manager.handle_length_error_async(
                                    kiro_request["messages"],
                                    retry_count,
                                    call_kiro_for_summary,
                                )

                                if should_retry:
                                    kiro_request["messages"] = truncated
                                    retry_count += 1
                                    logger.info(f"[{request_id}] {manager.truncate_info}")
                                    continue

                            # 返回错误
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

                        # 正常流式响应
                        async for chunk in response.aiter_bytes():
                            yield chunk

                        return

            except httpx.TimeoutException:
                logger.error(f"[{request_id}] 请求超时")
                if retry_count < max_retries:
                    retry_count += 1
                    await asyncio.sleep(1)
                    continue

                error_response = {"error": {"message": "Request timeout", "type": "timeout_error"}}
                yield f"data: {json.dumps(error_response)}\n\n".encode()
                yield b"data: [DONE]\n\n"
                return

            except Exception as e:
                logger.error(f"[{request_id}] 请求异常: {e}")
                error_response = {"error": {"message": str(e), "type": "api_error"}}
                yield f"data: {json.dumps(error_response)}\n\n".encode()
                yield b"data: [DONE]\n\n"
                return

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
    )


async def handle_non_stream(
    kiro_request: dict,
    headers: dict,
    manager: HistoryManager,
    request_id: str,
    original_messages: list,
) -> JSONResponse:
    """处理非流式响应"""
    retry_count = 0
    max_retries = HISTORY_CONFIG.max_retries

    while retry_count <= max_retries:
        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                response = await client.post(
                    KIRO_PROXY_URL,
                    json=kiro_request,
                    headers=headers,
                )

                if response.status_code != 200:
                    error_str = response.text
                    logger.error(f"[{request_id}] Kiro API Error {response.status_code}: {error_str[:200]}")

                    # 检查是否为长度错误
                    if is_content_length_error(response.status_code, error_str):
                        logger.info(f"[{request_id}] 检测到长度错误，尝试截断重试")

                        truncated, should_retry = await manager.handle_length_error_async(
                            kiro_request["messages"],
                            retry_count,
                            call_kiro_for_summary,
                        )

                        if should_retry:
                            kiro_request["messages"] = truncated
                            retry_count += 1
                            logger.info(f"[{request_id}] {manager.truncate_info}")
                            continue

                    raise HTTPException(response.status_code, error_str[:500])

                return JSONResponse(content=response.json())

        except HTTPException:
            raise
        except httpx.TimeoutException:
            logger.error(f"[{request_id}] 请求超时")
            if retry_count < max_retries:
                retry_count += 1
                await asyncio.sleep(1)
                continue
            raise HTTPException(408, "Request timeout")
        except Exception as e:
            logger.error(f"[{request_id}] 请求异常: {e}")
            raise HTTPException(500, str(e))

    raise HTTPException(503, "All retries exhausted")


# ==================== 配置接口 ====================

@app.get("/admin/config")
async def get_config():
    """获取当前配置"""
    return {
        "kiro_proxy_url": KIRO_PROXY_URL,
        "history_config": HISTORY_CONFIG.to_dict(),
    }


@app.post("/admin/config/history")
async def update_history_config(request: Request):
    """更新历史管理配置"""
    global HISTORY_CONFIG

    try:
        data = await request.json()
        HISTORY_CONFIG = HistoryConfig.from_dict(data)
        return {"status": "ok", "config": HISTORY_CONFIG.to_dict()}
    except Exception as e:
        raise HTTPException(400, str(e))


# ==================== 启动入口 ====================

if __name__ == "__main__":
    import uvicorn

    print(f"""
╔══════════════════════════════════════════════════════════════╗
║           AI History Manager API Server                      ║
╠══════════════════════════════════════════════════════════════╣
║  服务地址: http://0.0.0.0:{SERVICE_PORT}                          ║
║  API 端点: /v1/chat/completions                              ║
║  健康检查: /                                                 ║
║  模型列表: /v1/models                                        ║
║  配置查看: /admin/config                                     ║
╠══════════════════════════════════════════════════════════════╣
║  NewAPI 配置:                                                ║
║  - 类型: 自定义渠道 (OpenAI)                                 ║
║  - Base URL: http://your-server:{SERVICE_PORT}/v1                 ║
║  - API Key: 任意值                                           ║
╚══════════════════════════════════════════════════════════════╝
""")

    uvicorn.run(app, host="0.0.0.0", port=SERVICE_PORT)
