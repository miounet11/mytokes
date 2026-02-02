"""工具调用解析模块

解析 AI 响应中的工具调用，支持多种格式：
- 内联格式: [Calling tool: name]\nInput: {...}
- XML 格式: <tool_call>...</tool_call>
- JSON 格式: {"tool": "name", "input": {...}}
"""

import re
import json
import uuid
from typing import Optional
from dataclasses import dataclass, field
from .json_parser import try_parse_json, repair_json, find_json_end
from .logging import get_logger

logger = get_logger(__name__)


@dataclass
class ParsedToolCall:
    """解析后的工具调用"""
    id: str
    name: str
    input: dict = field(default_factory=dict)
    raw_text: str = ""
    start_pos: int = 0
    end_pos: int = 0
    parse_error: Optional[str] = None

    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            "id": self.id,
            "name": self.name,
            "input": self.input,
        }

    def to_anthropic_format(self) -> dict:
        """转换为 Anthropic tool_use 格式"""
        return {
            "type": "tool_use",
            "id": self.id,
            "name": self.name,
            "input": self.input,
        }


@dataclass
class ToolParseResult:
    """工具解析结果"""
    tool_calls: list[ParsedToolCall] = field(default_factory=list)
    remaining_text: str = ""
    has_incomplete: bool = False
    incomplete_text: str = ""


# ==================== 预编译正则表达式 ====================

# 内联格式: [Calling tool: name]
INLINE_TOOL_PATTERN = re.compile(
    r'\[Calling tool:\s*([^\]]+)\]',
    re.IGNORECASE
)

# Input 行
INPUT_PATTERN = re.compile(
    r'^\s*Input:\s*',
    re.MULTILINE | re.IGNORECASE
)

# XML 格式
XML_TOOL_PATTERN = re.compile(
    r'<tool_call>\s*([\s\S]*?)\s*</tool_call>',
    re.IGNORECASE
)

# XML 工具名称
XML_TOOL_NAME_PATTERN = re.compile(
    r'<tool_name>\s*([^<]+)\s*</tool_name>',
    re.IGNORECASE
)

# XML 参数
XML_PARAMETERS_PATTERN = re.compile(
    r'<parameters>\s*([\s\S]*?)\s*</parameters>',
    re.IGNORECASE
)

# 未闭合的工具调用开始
INCOMPLETE_TOOL_PATTERN = re.compile(
    r'\[Calling tool:\s*([^\]]+)\]\s*(?:Input:\s*)?({[^}]*)?$',
    re.IGNORECASE | re.DOTALL
)


def generate_tool_id() -> str:
    """生成工具调用 ID"""
    return f"toolu_{uuid.uuid4().hex[:24]}"


def parse_tool_calls(text: str) -> ToolParseResult:
    """解析文本中的所有工具调用

    自动检测格式并解析。

    Args:
        text: 包含工具调用的文本

    Returns:
        ToolParseResult 包含解析结果
    """
    result = ToolParseResult(remaining_text=text)

    if not text:
        return result

    # 尝试内联格式
    inline_result = parse_inline_tool_calls(text)
    if inline_result.tool_calls:
        return inline_result

    # 尝试 XML 格式
    xml_result = parse_xml_tool_calls(text)
    if xml_result.tool_calls:
        return xml_result

    return result


def parse_inline_tool_calls(text: str) -> ToolParseResult:
    """解析内联格式的工具调用

    格式:
    [Calling tool: tool_name]
    Input: {"param": "value"}

    Args:
        text: 文本内容

    Returns:
        ToolParseResult
    """
    result = ToolParseResult()
    tool_calls = []
    remaining_parts = []
    last_end = 0

    # 查找所有工具调用
    for match in INLINE_TOOL_PATTERN.finditer(text):
        tool_name = match.group(1).strip()
        start_pos = match.start()
        header_end = match.end()

        # 保存工具调用之前的文本
        if start_pos > last_end:
            remaining_parts.append(text[last_end:start_pos])

        # 查找 Input: 行
        after_header = text[header_end:]
        input_match = INPUT_PATTERN.match(after_header)

        if input_match:
            json_start = header_end + input_match.end()
            json_text = text[json_start:]

            # 查找 JSON 结束位置
            json_str, end_offset, parse_error = extract_tool_json(json_text)

            tool_call = ParsedToolCall(
                id=generate_tool_id(),
                name=tool_name,
                start_pos=start_pos,
                end_pos=json_start + end_offset,
                raw_text=text[start_pos:json_start + end_offset],
            )

            if json_str:
                parsed, error = try_parse_json(json_str)
                if parsed is not None:
                    tool_call.input = parsed
                else:
                    tool_call.parse_error = error
            elif parse_error:
                tool_call.parse_error = parse_error

            tool_calls.append(tool_call)
            last_end = tool_call.end_pos
        else:
            # 没有 Input 行，可能是不完整的调用
            tool_call = ParsedToolCall(
                id=generate_tool_id(),
                name=tool_name,
                start_pos=start_pos,
                end_pos=header_end,
                raw_text=text[start_pos:header_end],
                parse_error="Missing Input line",
            )
            tool_calls.append(tool_call)
            last_end = header_end

    # 保存剩余文本
    if last_end < len(text):
        remaining = text[last_end:].strip()
        if remaining:
            remaining_parts.append(remaining)

    # 检查是否有未完成的工具调用
    if remaining_parts:
        last_part = remaining_parts[-1] if remaining_parts else ""
        incomplete_match = INCOMPLETE_TOOL_PATTERN.search(last_part)
        if incomplete_match:
            result.has_incomplete = True
            result.incomplete_text = incomplete_match.group(0)

    result.tool_calls = tool_calls
    result.remaining_text = ''.join(remaining_parts).strip()

    return result


def extract_tool_json(text: str) -> tuple[Optional[str], int, Optional[str]]:
    """从文本开头提取 JSON 对象

    Args:
        text: 以 JSON 开头的文本

    Returns:
        (json_str, end_offset, error)
    """
    text = text.lstrip()
    if not text or text[0] != '{':
        return None, 0, "JSON not found"

    # 使用括号匹配查找结束位置
    end_pos = find_json_end(text, 0)

    if end_pos > 0:
        return text[:end_pos], end_pos, None

    # 未找到完整 JSON，尝试修复
    # 查找可能的结束位置（下一个工具调用或文件结尾）
    next_tool = INLINE_TOOL_PATTERN.search(text[1:])
    if next_tool:
        candidate = text[:next_tool.start() + 1].rstrip()
    else:
        candidate = text.rstrip()

    # 尝试修复并解析
    repaired = repair_json(candidate)
    parsed, error = try_parse_json(repaired)

    if parsed is not None:
        return repaired, len(candidate), None

    return candidate, len(candidate), f"Incomplete JSON: {error}"


def parse_xml_tool_calls(text: str) -> ToolParseResult:
    """解析 XML 格式的工具调用

    格式:
    <tool_call>
        <tool_name>name</tool_name>
        <parameters>{"param": "value"}</parameters>
    </tool_call>

    Args:
        text: 文本内容

    Returns:
        ToolParseResult
    """
    result = ToolParseResult()
    tool_calls = []
    remaining_text = text

    for match in XML_TOOL_PATTERN.finditer(text):
        content = match.group(1)

        # 提取工具名称
        name_match = XML_TOOL_NAME_PATTERN.search(content)
        if not name_match:
            continue

        tool_name = name_match.group(1).strip()

        # 提取参数
        params = {}
        params_match = XML_PARAMETERS_PATTERN.search(content)
        if params_match:
            params_str = params_match.group(1).strip()
            parsed, _ = try_parse_json(params_str)
            if parsed:
                params = parsed

        tool_call = ParsedToolCall(
            id=generate_tool_id(),
            name=tool_name,
            input=params,
            start_pos=match.start(),
            end_pos=match.end(),
            raw_text=match.group(0),
        )
        tool_calls.append(tool_call)

        # 从剩余文本中移除
        remaining_text = remaining_text.replace(match.group(0), '', 1)

    result.tool_calls = tool_calls
    result.remaining_text = remaining_text.strip()

    return result


def format_tool_call_inline(name: str, input_data: dict) -> str:
    """格式化为内联工具调用格式

    Args:
        name: 工具名称
        input_data: 输入参数

    Returns:
        格式化的字符串
    """
    json_str = json.dumps(input_data, ensure_ascii=False)
    return f"[Calling tool: {name}]\nInput: {json_str}"


def format_tool_call_xml(name: str, input_data: dict) -> str:
    """格式化为 XML 工具调用格式

    Args:
        name: 工具名称
        input_data: 输入参数

    Returns:
        格式化的字符串
    """
    json_str = json.dumps(input_data, ensure_ascii=False, indent=2)
    return f"""<tool_call>
    <tool_name>{name}</tool_name>
    <parameters>{json_str}</parameters>
</tool_call>"""


def has_tool_call_marker(text: str) -> bool:
    """检查文本是否包含工具调用标记

    Args:
        text: 文本内容

    Returns:
        是否包含工具调用
    """
    if not text:
        return False

    # 检查内联格式
    if INLINE_TOOL_PATTERN.search(text):
        return True

    # 检查 XML 格式
    if '<tool_call>' in text.lower():
        return True

    return False


def has_incomplete_tool_call(text: str) -> bool:
    """检查文本是否以未完成的工具调用结尾

    Args:
        text: 文本内容

    Returns:
        是否有未完成的工具调用
    """
    if not text:
        return False

    # 检查内联格式的不完整调用
    # 1. 有 [Calling tool: 但没有闭合的 ]
    if '[Calling tool:' in text and not text.rstrip().endswith('}'):
        last_bracket = text.rfind('[Calling tool:')
        after = text[last_bracket:]
        if ']' not in after:
            return True

    # 2. 有 Input: 但 JSON 不完整
    if 'Input:' in text:
        last_input = text.rfind('Input:')
        after = text[last_input + 6:].strip()
        if after.startswith('{'):
            # 检查括号是否平衡
            depth = 0
            in_string = False
            escape_next = False

            for char in after:
                if escape_next:
                    escape_next = False
                    continue
                if char == '\\':
                    escape_next = True
                    continue
                if char == '"':
                    in_string = not in_string
                    continue
                if in_string:
                    continue
                if char == '{':
                    depth += 1
                elif char == '}':
                    depth -= 1

            if depth > 0:
                return True

    # 检查 XML 格式的不完整调用
    if '<tool_call>' in text.lower():
        open_count = text.lower().count('<tool_call>')
        close_count = text.lower().count('</tool_call>')
        if open_count > close_count:
            return True

    return False


def extract_text_before_tools(text: str) -> str:
    """提取工具调用之前的文本

    Args:
        text: 完整文本

    Returns:
        工具调用之前的文本
    """
    # 查找第一个工具调用
    inline_match = INLINE_TOOL_PATTERN.search(text)
    xml_match = re.search(r'<tool_call>', text, re.IGNORECASE)

    positions = []
    if inline_match:
        positions.append(inline_match.start())
    if xml_match:
        positions.append(xml_match.start())

    if positions:
        return text[:min(positions)].strip()

    return text


def extract_text_after_tools(text: str) -> str:
    """提取工具调用之后的文本

    Args:
        text: 完整文本

    Returns:
        工具调用之后的文本
    """
    result = parse_tool_calls(text)
    return result.remaining_text
