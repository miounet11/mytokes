"""Kiro API 格式转换器

将 Anthropic 格式转换为 Kiro 原生格式，确保工具调用正确处理。
基于 KiroProxy 的转换逻辑实现。
"""

import json
import uuid
from typing import Optional, Tuple, List, Dict, Any


def map_model_name(model: str) -> str:
    """映射模型名称到 Kiro 格式"""
    model_lower = model.lower()

    # Opus 系列
    if "opus" in model_lower:
        if "4.5" in model or "4-5" in model:
            return "claude-opus-4.5"
        return "claude-opus-4"

    # Sonnet 系列
    if "sonnet" in model_lower:
        if "4.5" in model or "4-5" in model:
            return "claude-sonnet-4.5"
        if "3.7" in model or "3-7" in model:
            return "claude-sonnet-3.7"
        return "claude-sonnet-4"

    # Haiku 系列
    if "haiku" in model_lower:
        if "4.5" in model or "4-5" in model:
            return "claude-haiku-4.5"
        return "claude-haiku-4"

    # 默认
    return "claude-sonnet-4"


def extract_text_content(content) -> str:
    """从 content 中提取纯文本

    Args:
        content: 可以是字符串、列表或字典

    Returns:
        提取的文本内容
    """
    if isinstance(content, str):
        return content

    if isinstance(content, list):
        text_parts = []
        for item in content:
            if isinstance(item, dict):
                item_type = item.get("type", "")

                if item_type == "text":
                    text_parts.append(item.get("text", ""))
                elif item_type == "tool_result":
                    # 提取工具结果内容
                    result_content = item.get("content", "")
                    if isinstance(result_content, str):
                        text_parts.append(result_content)
                    elif isinstance(result_content, list):
                        for rc in result_content:
                            if isinstance(rc, dict) and rc.get("type") == "text":
                                text_parts.append(rc.get("text", ""))
                # 跳过 tool_use, thinking 等类型
            elif isinstance(item, str):
                text_parts.append(item)

        return "\n".join(filter(None, text_parts))

    if isinstance(content, dict):
        if "text" in content:
            return content["text"]
        if "content" in content:
            return extract_text_content(content["content"])

    return str(content)


def parse_assistant_content(content) -> Tuple[str, List[Dict]]:
    """解析 assistant 消息，提取文本和工具调用

    Args:
        content: assistant 消息的 content 字段

    Returns:
        (text_content, tool_uses)
    """
    if isinstance(content, str):
        return content, []

    if isinstance(content, list):
        text_parts = []
        tool_uses = []

        for item in content:
            if isinstance(item, dict):
                item_type = item.get("type", "")

                if item_type == "text":
                    text_parts.append(item.get("text", ""))
                elif item_type == "tool_use":
                    tool_uses.append({
                        "toolUseId": item.get("id", f"toolu_{uuid.uuid4().hex[:12]}"),
                        "name": item.get("name"),
                        "input": item.get("input", {})
                    })
                elif item_type == "thinking":
                    # 跳过 thinking blocks（Kiro 不支持）
                    pass
            elif isinstance(item, str):
                text_parts.append(item)

        return "\n".join(filter(None, text_parts)), tool_uses

    return str(content), []


def parse_user_tool_results(content) -> Optional[List[Dict]]:
    """解析 user 消息中的工具结果

    Args:
        content: user 消息的 content 字段

    Returns:
        tool_results 列表或 None
    """
    if not isinstance(content, list):
        return None

    tool_results = []

    for item in content:
        if isinstance(item, dict) and item.get("type") == "tool_result":
            tool_use_id = item.get("tool_use_id", "")
            result_content = item.get("content", "")
            is_error = item.get("is_error", False)

            # 提取结果文本
            if isinstance(result_content, str):
                result_text = result_content
            elif isinstance(result_content, list):
                parts = []
                for rc in result_content:
                    if isinstance(rc, dict) and rc.get("type") == "text":
                        parts.append(rc.get("text", ""))
                    elif isinstance(rc, str):
                        parts.append(rc)
                result_text = "\n".join(parts)
            else:
                result_text = str(result_content)

            tool_results.append({
                "toolUseId": tool_use_id,
                "content": result_text,
                "status": "error" if is_error else "success"
            })

    return tool_results if tool_results else None


def fix_history_alternation(history: List[Dict], model_id: str = "claude-sonnet-4") -> List[Dict]:
    """修复历史消息交替和工具调用配对

    关键规则（基于 KiroProxy 实现）：
    1. 消息必须 user → assistant → user → assistant 交替
    2. 如果 assistant 有 toolUses，下一条 user 必须有 toolResults
    3. 如果 assistant 没有 toolUses，下一条 user 不能有 toolResults

    Args:
        history: 历史消息列表
        model_id: 模型 ID（用于生成占位消息）

    Returns:
        修复后的历史消息列表
    """
    if not history:
        return []

    fixed = []

    # 第一步：修复交替
    for i, msg in enumerate(history):
        is_user = "userInputMessage" in msg
        is_assistant = "assistantResponseMessage" in msg

        # 检查交替
        if fixed:
            last_is_user = "userInputMessage" in fixed[-1]

            if is_user and last_is_user:
                # 连续两条 user，插入占位 assistant
                fixed.append({
                    "assistantResponseMessage": {
                        "content": "I understand."
                    }
                })
            elif is_assistant and not last_is_user:
                # 连续两条 assistant，插入占位 user
                fixed.append({
                    "userInputMessage": {
                        "content": "Please continue."
                    }
                })

        fixed.append(msg)

    # 确保不以 user 结尾（Kiro 需要 assistant 结尾）
    if fixed and "userInputMessage" in fixed[-1]:
        fixed.append({
            "assistantResponseMessage": {
                "content": "I understand."
            }
        })

    # 第二步：验证和修复 toolUses/toolResults 配对
    for i in range(len(fixed) - 1):
        current = fixed[i]
        next_msg = fixed[i + 1]

        if "assistantResponseMessage" in current:
            assistant = current["assistantResponseMessage"]
            has_tool_uses = "toolUses" in assistant and assistant["toolUses"]

            if "userInputMessage" in next_msg:
                user = next_msg["userInputMessage"]
                has_tool_results = (
                    "userInputMessageContext" in user and
                    "toolResults" in user["userInputMessageContext"]
                )

                # 修复不配对的情况
                if has_tool_uses and not has_tool_results:
                    # assistant 有 toolUses 但 user 没有 toolResults
                    # 清除 toolUses（避免 Kiro API 报错）
                    print(f"[Kiro] 警告: assistant 有 toolUses 但下一条 user 没有 toolResults，清除 toolUses")
                    assistant.pop("toolUses", None)
                elif not has_tool_uses and has_tool_results:
                    # assistant 没有 toolUses 但 user 有 toolResults
                    # 清除 toolResults
                    print(f"[Kiro] 警告: assistant 没有 toolUses 但下一条 user 有 toolResults，清除 toolResults")
                    user.pop("userInputMessageContext", None)

    return fixed


def convert_anthropic_tools_to_kiro(tools: List[Dict]) -> List[Dict]:
    """将 Anthropic 工具定义转换为 Kiro 格式

    Args:
        tools: Anthropic 工具定义列表

    Returns:
        Kiro 格式的工具定义列表
    """
    kiro_tools = []

    for tool in tools:
        name = tool.get("name", "unknown")
        description = tool.get("description", "")
        input_schema = tool.get("input_schema", {})

        # Kiro 限制描述长度为 500 字符
        if len(description) > 500:
            description = description[:497] + "..."

        kiro_tools.append({
            "toolSpecification": {
                "name": name,
                "description": description,
                "inputSchema": {
                    "json": input_schema
                }
            }
        })

    return kiro_tools


def convert_anthropic_to_kiro(anthropic_body: Dict[str, Any]) -> Dict[str, Any]:
    """将 Anthropic 请求转换为 Kiro 原生格式

    Args:
        anthropic_body: Anthropic 格式的请求体

    Returns:
        Kiro 格式的请求体
    """
    messages = anthropic_body.get("messages", [])
    system = anthropic_body.get("system", "")
    tools = anthropic_body.get("tools", [])
    model = anthropic_body.get("model", "claude-sonnet-4")

    # 提取最后一条用户消息和历史
    user_content = ""
    history = []
    tool_results = None

    for i, msg in enumerate(messages):
        role = msg.get("role")
        content = msg.get("content", "")

        if i == len(messages) - 1 and role == "user":
            # 最后一条用户消息
            user_content = extract_text_content(content)
            # 检查是否有工具结果
            tool_results = parse_user_tool_results(content)
        else:
            # 历史消息
            if role == "user":
                user_msg = {
                    "userInputMessage": {
                        "content": extract_text_content(content)
                    }
                }

                # 检查是否有工具结果
                results = parse_user_tool_results(content)
                if results:
                    user_msg["userInputMessage"]["userInputMessageContext"] = {
                        "toolResults": results
                    }

                history.append(user_msg)

            elif role == "assistant":
                # 解析工具调用
                text_content, tool_uses = parse_assistant_content(content)

                assistant_msg = {
                    "assistantResponseMessage": {
                        "content": text_content or " "  # 确保不为空
                    }
                }

                if tool_uses:
                    assistant_msg["assistantResponseMessage"]["toolUses"] = tool_uses

                history.append(assistant_msg)

    # 修复历史消息交替
    history = fix_history_alternation(history, map_model_name(model))

    # 转换工具定义
    kiro_tools = None
    if tools:
        kiro_tools = convert_anthropic_tools_to_kiro(tools)

    # 构建 Kiro 请求
    kiro_request = {
        "conversationState": {
            "currentMessage": {
                "userInputMessage": {
                    "content": user_content or "Hello"  # 确保不为空
                }
            },
            "history": history
        },
        "modelId": map_model_name(model),
        "inferenceConfig": {
            "maxTokens": anthropic_body.get("max_tokens", 8192),
            "temperature": anthropic_body.get("temperature", 1.0),
            "topP": anthropic_body.get("top_p", 1.0)
        }
    }

    # 添加 system prompt（如果有）
    if system:
        if isinstance(system, str):
            system_text = system
        elif isinstance(system, list):
            system_parts = []
            for item in system:
                if isinstance(item, dict) and "text" in item:
                    system_parts.append(item["text"])
                elif isinstance(item, str):
                    system_parts.append(item)
            system_text = "\n".join(system_parts)
        else:
            system_text = str(system)

        if system_text.strip():
            kiro_request["conversationState"]["systemPrompt"] = system_text

    # 添加工具配置（如果有）
    if kiro_tools:
        kiro_request["conversationState"]["currentMessage"]["userInputMessage"]["toolConfig"] = {
            "tools": kiro_tools
        }

    # 添加工具结果（如果有）
    if tool_results:
        kiro_request["conversationState"]["currentMessage"]["userInputMessage"]["userInputMessageContext"] = {
            "toolResults": tool_results
        }

    return kiro_request


def convert_kiro_response_to_anthropic(kiro_response: Dict[str, Any], model: str, message_id: str) -> Dict[str, Any]:
    """将 Kiro 响应转换为 Anthropic 格式

    Args:
        kiro_response: Kiro 格式的响应
        model: 模型名称
        message_id: 消息 ID

    Returns:
        Anthropic 格式的响应
    """
    # 提取内容
    text = kiro_response.get("text", "")
    tool_uses = kiro_response.get("tool_uses", [])
    stop_reason = kiro_response.get("stop_reason", "end_turn")
    input_tokens = kiro_response.get("input_tokens", 0)
    output_tokens = kiro_response.get("output_tokens", 0)

    # 构建 content
    content = []

    if text:
        content.append({
            "type": "text",
            "text": text
        })

    for tool_use in tool_uses:
        content.append({
            "type": "tool_use",
            "id": tool_use.get("id", f"toolu_{uuid.uuid4().hex[:12]}"),
            "name": tool_use.get("name"),
            "input": tool_use.get("input", {})
        })

    # 构建 Anthropic 响应
    return {
        "id": message_id,
        "type": "message",
        "role": "assistant",
        "content": content,
        "model": model,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens
        }
    }
