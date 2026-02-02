"""数据模型定义

使用 Pydantic 定义 API 请求/响应的数据结构。
"""

from typing import Any, Optional, Union
from pydantic import BaseModel, Field
from enum import Enum


# ==================== 枚举类型 ====================

class MessageRole(str, Enum):
    """消息角色"""
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"


class ContentBlockType(str, Enum):
    """内容块类型"""
    TEXT = "text"
    TOOL_USE = "tool_use"
    TOOL_RESULT = "tool_result"
    THINKING = "thinking"
    IMAGE = "image"


class StopReason(str, Enum):
    """停止原因"""
    END_TURN = "end_turn"
    TOOL_USE = "tool_use"
    MAX_TOKENS = "max_tokens"
    STOP_SEQUENCE = "stop_sequence"


class FinishReason(str, Enum):
    """OpenAI 完成原因"""
    STOP = "stop"
    LENGTH = "length"
    TOOL_CALLS = "tool_calls"
    CONTENT_FILTER = "content_filter"


# ==================== 内容块模型 ====================

class TextContent(BaseModel):
    """文本内容块"""
    type: str = "text"
    text: str


class ImageSource(BaseModel):
    """图片来源"""
    type: str = "base64"
    media_type: str
    data: str


class ImageContent(BaseModel):
    """图片内容块"""
    type: str = "image"
    source: ImageSource


class ToolUseContent(BaseModel):
    """工具调用内容块"""
    type: str = "tool_use"
    id: str
    name: str
    input: dict[str, Any] = Field(default_factory=dict)


class ToolResultContent(BaseModel):
    """工具结果内容块"""
    type: str = "tool_result"
    tool_use_id: str
    content: Union[str, list[dict]] = ""
    is_error: bool = False


class ThinkingContent(BaseModel):
    """思考内容块"""
    type: str = "thinking"
    thinking: str


ContentBlock = Union[TextContent, ImageContent, ToolUseContent, ToolResultContent, ThinkingContent]


# ==================== Anthropic 消息模型 ====================

class AnthropicMessage(BaseModel):
    """Anthropic 消息格式"""
    role: MessageRole
    content: Union[str, list[ContentBlock]]


class ToolDefinition(BaseModel):
    """工具定义"""
    name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)


class AnthropicRequest(BaseModel):
    """Anthropic Messages API 请求"""
    model: str
    messages: list[AnthropicMessage]
    max_tokens: int = 16384
    system: Optional[Union[str, list[dict]]] = None
    tools: Optional[list[ToolDefinition]] = None
    tool_choice: Optional[dict] = None
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    top_k: Optional[int] = None
    stop_sequences: Optional[list[str]] = None
    stream: bool = False
    metadata: Optional[dict] = None


class UsageInfo(BaseModel):
    """Token 使用信息"""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


class AnthropicResponse(BaseModel):
    """Anthropic Messages API 响应"""
    id: str
    type: str = "message"
    role: str = "assistant"
    content: list[ContentBlock]
    model: str
    stop_reason: Optional[StopReason] = None
    stop_sequence: Optional[str] = None
    usage: UsageInfo = Field(default_factory=UsageInfo)


# ==================== OpenAI 消息模型 ====================

class OpenAIFunctionCall(BaseModel):
    """OpenAI 函数调用"""
    name: str
    arguments: str


class OpenAIToolCall(BaseModel):
    """OpenAI 工具调用"""
    id: str
    type: str = "function"
    function: OpenAIFunctionCall


class OpenAIMessage(BaseModel):
    """OpenAI 消息格式"""
    role: str
    content: Optional[Union[str, list[dict]]] = None
    name: Optional[str] = None
    tool_calls: Optional[list[OpenAIToolCall]] = None
    tool_call_id: Optional[str] = None


class OpenAIRequest(BaseModel):
    """OpenAI Chat Completions API 请求"""
    model: str
    messages: list[OpenAIMessage]
    max_tokens: Optional[int] = None
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    frequency_penalty: Optional[float] = None
    presence_penalty: Optional[float] = None
    stop: Optional[Union[str, list[str]]] = None
    stream: bool = False
    tools: Optional[list[dict]] = None
    tool_choice: Optional[Union[str, dict]] = None


class OpenAIChoice(BaseModel):
    """OpenAI 响应选项"""
    index: int = 0
    message: OpenAIMessage
    finish_reason: Optional[str] = None


class OpenAIUsage(BaseModel):
    """OpenAI Token 使用信息"""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class OpenAIResponse(BaseModel):
    """OpenAI Chat Completions API 响应"""
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: list[OpenAIChoice]
    usage: OpenAIUsage = Field(default_factory=OpenAIUsage)


# ==================== 流式事件模型 ====================

class StreamEvent(BaseModel):
    """流式事件基类"""
    type: str


class MessageStartEvent(StreamEvent):
    """消息开始事件"""
    type: str = "message_start"
    message: dict


class ContentBlockStartEvent(StreamEvent):
    """内容块开始事件"""
    type: str = "content_block_start"
    index: int
    content_block: dict


class ContentBlockDeltaEvent(StreamEvent):
    """内容块增量事件"""
    type: str = "content_block_delta"
    index: int
    delta: dict


class ContentBlockStopEvent(StreamEvent):
    """内容块结束事件"""
    type: str = "content_block_stop"
    index: int


class MessageDeltaEvent(StreamEvent):
    """消息增量事件"""
    type: str = "message_delta"
    delta: dict
    usage: dict


class MessageStopEvent(StreamEvent):
    """消息结束事件"""
    type: str = "message_stop"


# ==================== 内部模型 ====================

class TruncationInfo(BaseModel):
    """截断信息"""
    is_truncated: bool = False
    reason: Optional[str] = None
    position: Optional[int] = None
    details: Optional[str] = None


class ParsedToolCall(BaseModel):
    """解析后的工具调用"""
    id: str
    name: str
    input: dict[str, Any] = Field(default_factory=dict)
    raw_json: Optional[str] = None
    parse_error: Optional[str] = None


class ContinuationResult(BaseModel):
    """续传结果"""
    text: str
    finish_reason: str
    stream_completed: bool
    input_tokens: int = 0
    output_tokens: int = 0
    continuation_count: int = 0
    tool_calls: list[ParsedToolCall] = Field(default_factory=list)


class RoutingDecision(BaseModel):
    """路由决策"""
    original_model: str
    routed_model: str
    reason: str
    priority: int = 0


# ==================== 错误模型 ====================

class ErrorDetail(BaseModel):
    """错误详情"""
    type: str
    message: str
    code: Optional[str] = None
    param: Optional[str] = None


class ErrorResponse(BaseModel):
    """错误响应"""
    type: str = "error"
    error: ErrorDetail
