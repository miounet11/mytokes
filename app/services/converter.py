"""协议转换服务

在 Anthropic Messages API 和 OpenAI Chat Completions API 之间进行转换。
"""

import json
import time
import uuid
from typing import Any, Optional, Union

from ..config import get_settings
from ..utils.logging import get_logger
from ..utils.json_parser import safe_json_loads

logger = get_logger(__name__)


class AnthropicToOpenAIConverter:
    """Anthropic 请求转换为 OpenAI 格式

    将 Anthropic Messages API 请求转换为 OpenAI Chat Completions API 格式。
    """

    def __init__(self):
        self.settings = get_settings()

    def convert_request(self, anthropic_request: dict) -> dict:
        """转换请求

        Args:
            anthropic_request: Anthropic 格式的请求

        Returns:
            OpenAI 格式的请求
        """
        openai_request = {
            "model": anthropic_request.get("model", ""),
            "messages": self._convert_messages(
                anthropic_request.get("messages", []),
                anthropic_request.get("system")
            ),
            "stream": anthropic_request.get("stream", False),
        }

        # 转换可选参数
        if "max_tokens" in anthropic_request:
            openai_request["max_tokens"] = anthropic_request["max_tokens"]

        if "temperature" in anthropic_request:
            openai_request["temperature"] = anthropic_request["temperature"]

        if "top_p" in anthropic_request:
            openai_request["top_p"] = anthropic_request["top_p"]

        if "stop_sequences" in anthropic_request:
            openai_request["stop"] = anthropic_request["stop_sequences"]

        # 转换工具
        if "tools" in anthropic_request:
            openai_request["tools"] = self._convert_tools(anthropic_request["tools"])

        if "tool_choice" in anthropic_request:
            openai_request["tool_choice"] = self._convert_tool_choice(
                anthropic_request["tool_choice"]
            )

        return openai_request

    def _convert_messages(
        self,
        messages: list[dict],
        system: Optional[Union[str, list]] = None
    ) -> list[dict]:
        """转换消息列表"""
        openai_messages = []

        # 添加系统消息
        if system:
            system_content = self._extract_system_content(system)
            if system_content:
                openai_messages.append({
                    "role": "system",
                    "content": system_content
                })

        # 转换每条消息
        for msg in messages:
            converted = self._convert_message(msg)
            if converted:
                if isinstance(converted, list):
                    openai_messages.extend(converted)
                else:
                    openai_messages.append(converted)

        return openai_messages

    def _extract_system_content(self, system: Union[str, list]) -> str:
        """提取系统消息内容"""
        if isinstance(system, str):
            return system

        if isinstance(system, list):
            texts = []
            for block in system:
                if isinstance(block, dict) and block.get("type") == "text":
                    texts.append(block.get("text", ""))
                elif isinstance(block, str):
                    texts.append(block)
            return "\n".join(texts)

        return ""

    def _convert_message(self, message: dict) -> Optional[Union[dict, list[dict]]]:
        """转换单条消息"""
        role = message.get("role", "")
        content = message.get("content", "")

        if role == "user":
            return self._convert_user_message(content)
        elif role == "assistant":
            return self._convert_assistant_message(content)

        return None

    def _convert_user_message(self, content: Union[str, list]) -> dict:
        """转换用户消息"""
        if isinstance(content, str):
            return {"role": "user", "content": content}

        if isinstance(content, list):
            # 检查是否包含工具结果
            tool_results = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"]

            if tool_results:
                # 返回工具消息列表
                messages = []
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "tool_result":
                            messages.append(self._convert_tool_result(block))
                        elif block.get("type") == "text":
                            messages.append({
                                "role": "user",
                                "content": block.get("text", "")
                            })
                return messages if len(messages) > 1 else messages[0] if messages else {"role": "user", "content": ""}

            # 转换多模态内容
            openai_content = []
            for block in content:
                converted = self._convert_content_block(block)
                if converted:
                    openai_content.append(converted)

            return {"role": "user", "content": openai_content if openai_content else ""}

        return {"role": "user", "content": str(content)}

    def _convert_assistant_message(self, content: Union[str, list]) -> dict:
        """转换助手消息"""
        if isinstance(content, str):
            return {"role": "assistant", "content": content}

        if isinstance(content, list):
            text_parts = []
            tool_calls = []

            for block in content:
                if isinstance(block, dict):
                    block_type = block.get("type", "")

                    if block_type == "text":
                        text_parts.append(block.get("text", ""))
                    elif block_type == "tool_use":
                        tool_calls.append(self._convert_tool_use(block))
                    elif block_type == "thinking":
                        # 思考内容可以作为注释添加
                        thinking = block.get("thinking", "")
                        if thinking:
                            text_parts.append(f"<thinking>\n{thinking}\n</thinking>")

            result = {"role": "assistant"}

            if text_parts:
                result["content"] = "\n".join(text_parts)
            else:
                result["content"] = None

            if tool_calls:
                result["tool_calls"] = tool_calls

            return result

        return {"role": "assistant", "content": str(content)}

    def _convert_content_block(self, block: Union[dict, str]) -> Optional[dict]:
        """转换内容块"""
        if isinstance(block, str):
            return {"type": "text", "text": block}

        if not isinstance(block, dict):
            return None

        block_type = block.get("type", "")

        if block_type == "text":
            return {"type": "text", "text": block.get("text", "")}

        elif block_type == "image":
            source = block.get("source", {})
            if source.get("type") == "base64":
                media_type = source.get("media_type", "image/png")
                data = source.get("data", "")
                return {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{media_type};base64,{data}"
                    }
                }
            elif source.get("type") == "url":
                return {
                    "type": "image_url",
                    "image_url": {
                        "url": source.get("url", "")
                    }
                }

        return None

    def _convert_tool_use(self, block: dict) -> dict:
        """转换工具调用"""
        return {
            "id": block.get("id", f"call_{uuid.uuid4().hex[:24]}"),
            "type": "function",
            "function": {
                "name": block.get("name", ""),
                "arguments": json.dumps(block.get("input", {}), ensure_ascii=False)
            }
        }

    def _convert_tool_result(self, block: dict) -> dict:
        """转换工具结果"""
        content = block.get("content", "")

        if isinstance(content, list):
            # 提取文本内容
            texts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    texts.append(item.get("text", ""))
                elif isinstance(item, str):
                    texts.append(item)
            content = "\n".join(texts)

        return {
            "role": "tool",
            "tool_call_id": block.get("tool_use_id", ""),
            "content": str(content)
        }

    def _convert_tools(self, tools: list[dict]) -> list[dict]:
        """转换工具定义"""
        openai_tools = []

        for tool in tools:
            openai_tool = {
                "type": "function",
                "function": {
                    "name": tool.get("name", ""),
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema", {})
                }
            }
            openai_tools.append(openai_tool)

        return openai_tools

    def _convert_tool_choice(self, tool_choice: Union[str, dict]) -> Union[str, dict]:
        """转换工具选择"""
        if isinstance(tool_choice, str):
            mapping = {
                "auto": "auto",
                "any": "required",
                "none": "none",
            }
            return mapping.get(tool_choice, "auto")

        if isinstance(tool_choice, dict):
            if tool_choice.get("type") == "tool":
                return {
                    "type": "function",
                    "function": {
                        "name": tool_choice.get("name", "")
                    }
                }

        return "auto"


class OpenAIToAnthropicConverter:
    """OpenAI 响应转换为 Anthropic 格式

    将 OpenAI Chat Completions API 响应转换为 Anthropic Messages API 格式。
    """

    def __init__(self):
        self.settings = get_settings()

    def convert_response(self, openai_response: dict) -> dict:
        """转换非流式响应

        Args:
            openai_response: OpenAI 格式的响应

        Returns:
            Anthropic 格式的响应
        """
        choices = openai_response.get("choices", [])
        if not choices:
            return self._create_empty_response(openai_response)

        choice = choices[0]
        message = choice.get("message", {})

        # 构建内容块
        content = self._convert_message_content(message)

        # 转换停止原因
        stop_reason = self._convert_finish_reason(choice.get("finish_reason"))

        # 转换使用量
        usage = self._convert_usage(openai_response.get("usage", {}))

        return {
            "id": openai_response.get("id", f"msg_{uuid.uuid4().hex}"),
            "type": "message",
            "role": "assistant",
            "content": content,
            "model": openai_response.get("model", ""),
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": usage,
        }

    def _convert_message_content(self, message: dict) -> list[dict]:
        """转换消息内容为内容块列表"""
        content_blocks = []

        # 处理文本内容
        text_content = message.get("content")
        if text_content:
            content_blocks.append({
                "type": "text",
                "text": text_content
            })

        # 处理工具调用
        tool_calls = message.get("tool_calls", [])
        for tool_call in tool_calls:
            content_blocks.append(self._convert_tool_call(tool_call))

        return content_blocks if content_blocks else [{"type": "text", "text": ""}]

    def _convert_tool_call(self, tool_call: dict) -> dict:
        """转换工具调用"""
        function = tool_call.get("function", {})
        arguments = function.get("arguments", "{}")

        # 解析参数
        parsed_args = safe_json_loads(arguments, {})

        return {
            "type": "tool_use",
            "id": tool_call.get("id", f"toolu_{uuid.uuid4().hex[:24]}"),
            "name": function.get("name", ""),
            "input": parsed_args,
        }

    def _convert_finish_reason(self, finish_reason: Optional[str]) -> Optional[str]:
        """转换完成原因"""
        mapping = {
            "stop": "end_turn",
            "length": "max_tokens",
            "tool_calls": "tool_use",
            "content_filter": "end_turn",
            "function_call": "tool_use",
        }
        return mapping.get(finish_reason, "end_turn")

    def _convert_usage(self, usage: dict) -> dict:
        """转换使用量信息"""
        return {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        }

    def _create_empty_response(self, openai_response: dict) -> dict:
        """创建空响应"""
        return {
            "id": openai_response.get("id", f"msg_{uuid.uuid4().hex}"),
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": ""}],
            "model": openai_response.get("model", ""),
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        }

    def convert_stream_event(self, event_type: str, data: dict) -> Optional[dict]:
        """转换流式事件

        Args:
            event_type: 事件类型
            data: 事件数据

        Returns:
            Anthropic 格式的事件，或 None 如果应跳过
        """
        # OpenAI 流式响应通常是 delta 格式
        choices = data.get("choices", [])
        if not choices:
            return None

        choice = choices[0]
        delta = choice.get("delta", {})
        finish_reason = choice.get("finish_reason")

        # 处理内容增量
        if "content" in delta:
            return {
                "type": "content_block_delta",
                "index": 0,
                "delta": {
                    "type": "text_delta",
                    "text": delta["content"]
                }
            }

        # 处理工具调用增量
        if "tool_calls" in delta:
            tool_calls = delta["tool_calls"]
            if tool_calls:
                tool_call = tool_calls[0]
                return self._convert_tool_call_delta(tool_call)

        # 处理完成
        if finish_reason:
            return {
                "type": "message_delta",
                "delta": {
                    "stop_reason": self._convert_finish_reason(finish_reason),
                    "stop_sequence": None,
                },
                "usage": {
                    "output_tokens": data.get("usage", {}).get("completion_tokens", 0)
                }
            }

        return None

    def _convert_tool_call_delta(self, tool_call: dict) -> Optional[dict]:
        """转换工具调用增量"""
        function = tool_call.get("function", {})

        # 工具调用开始
        if "name" in function:
            return {
                "type": "content_block_start",
                "index": tool_call.get("index", 0),
                "content_block": {
                    "type": "tool_use",
                    "id": tool_call.get("id", f"toolu_{uuid.uuid4().hex[:24]}"),
                    "name": function["name"],
                    "input": {},
                }
            }

        # 工具调用参数增量
        if "arguments" in function:
            return {
                "type": "content_block_delta",
                "index": tool_call.get("index", 0),
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": function["arguments"]
                }
            }

        return None


# 便捷函数
def anthropic_to_openai(request: dict) -> dict:
    """转换 Anthropic 请求为 OpenAI 格式"""
    return AnthropicToOpenAIConverter().convert_request(request)


def openai_to_anthropic(response: dict) -> dict:
    """转换 OpenAI 响应为 Anthropic 格式"""
    return OpenAIToAnthropicConverter().convert_response(response)
