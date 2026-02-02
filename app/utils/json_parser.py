"""JSON 解析与修复模块

提供健壮的 JSON 解析功能，支持修复常见的 JSON 格式错误。
"""

import json
import re
from typing import Any, Optional, Tuple
from .logging import get_logger

logger = get_logger(__name__)

# 预编译正则表达式
TRAILING_COMMA_PATTERN = re.compile(r',\s*([}\]])')
UNCLOSED_STRING_PATTERN = re.compile(r'"[^"]*$')
CONTROL_CHAR_PATTERN = re.compile(r'[\x00-\x1f]')


def safe_json_loads(json_str: str, default: Any = None) -> Any:
    """安全的 JSON 解析

    尝试解析 JSON，失败时返回默认值。

    Args:
        json_str: JSON 字符串
        default: 解析失败时的默认值

    Returns:
        解析结果或默认值
    """
    try:
        return json.loads(json_str)
    except (json.JSONDecodeError, TypeError):
        return default


def try_parse_json(json_str: str) -> Tuple[Optional[dict], Optional[str]]:
    """尝试解析 JSON，失败时尝试修复

    Args:
        json_str: JSON 字符串

    Returns:
        (解析结果, 错误信息) - 成功时错误信息为 None
    """
    if not json_str or not json_str.strip():
        return None, "Empty JSON string"

    # 第一次尝试：直接解析
    try:
        return json.loads(json_str), None
    except json.JSONDecodeError as e:
        original_error = str(e)

    # 第二次尝试：修复后解析
    repaired = repair_json(json_str)
    try:
        return json.loads(repaired), None
    except json.JSONDecodeError:
        pass

    # 第三次尝试：提取有效 JSON 部分
    extracted = extract_json_object(json_str)
    if extracted:
        try:
            return json.loads(extracted), None
        except json.JSONDecodeError:
            pass

    return None, original_error


def repair_json(json_str: str) -> str:
    """修复常见的 JSON 格式错误

    修复策略：
    1. 移除尾随逗号
    2. 补全未闭合的字符串
    3. 补全未闭合的括号
    4. 转义控制字符
    5. 修复单引号

    Args:
        json_str: 可能有错误的 JSON 字符串

    Returns:
        修复后的 JSON 字符串
    """
    if not json_str:
        return json_str

    result = json_str.strip()

    # 1. 移除尾随逗号
    result = TRAILING_COMMA_PATTERN.sub(r'\1', result)

    # 2. 转义未转义的控制字符
    def escape_control_char(match):
        char = match.group(0)
        if char == '\n':
            return '\\n'
        elif char == '\r':
            return '\\r'
        elif char == '\t':
            return '\\t'
        else:
            return f'\\u{ord(char):04x}'

    # 只在字符串值内部转义控制字符
    result = escape_control_chars_in_strings(result)

    # 3. 补全未闭合的字符串
    result = close_unclosed_strings(result)

    # 4. 补全未闭合的括号
    result = close_unclosed_brackets(result)

    # 5. 修复单引号（转为双引号）
    result = fix_single_quotes(result)

    return result


def escape_control_chars_in_strings(json_str: str) -> str:
    """转义字符串值中的控制字符"""
    result = []
    in_string = False
    escape_next = False

    for char in json_str:
        if escape_next:
            result.append(char)
            escape_next = False
            continue

        if char == '\\':
            result.append(char)
            escape_next = True
            continue

        if char == '"':
            in_string = not in_string
            result.append(char)
            continue

        if in_string and ord(char) < 32:
            # 转义控制字符
            if char == '\n':
                result.append('\\n')
            elif char == '\r':
                result.append('\\r')
            elif char == '\t':
                result.append('\\t')
            else:
                result.append(f'\\u{ord(char):04x}')
        else:
            result.append(char)

    return ''.join(result)


def close_unclosed_strings(json_str: str) -> str:
    """补全未闭合的字符串"""
    quote_count = 0
    escape_next = False

    for char in json_str:
        if escape_next:
            escape_next = False
            continue
        if char == '\\':
            escape_next = True
            continue
        if char == '"':
            quote_count += 1

    # 如果引号数量为奇数，添加闭合引号
    if quote_count % 2 == 1:
        json_str = json_str.rstrip() + '"'

    return json_str


def close_unclosed_brackets(json_str: str) -> str:
    """补全未闭合的括号"""
    stack = []
    in_string = False
    escape_next = False

    for char in json_str:
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
            stack.append('}')
        elif char == '[':
            stack.append(']')
        elif char in '}]':
            if stack and stack[-1] == char:
                stack.pop()

    # 补全未闭合的括号
    while stack:
        json_str += stack.pop()

    return json_str


def fix_single_quotes(json_str: str) -> str:
    """将单引号转换为双引号（仅在 JSON 键值对中）"""
    # 简单实现：只处理明显的单引号键
    # 完整实现需要更复杂的状态机
    result = []
    i = 0
    n = len(json_str)

    while i < n:
        char = json_str[i]

        # 检查是否是单引号开始的键或值
        if char == "'":
            # 查找配对的单引号
            j = i + 1
            while j < n and json_str[j] != "'":
                if json_str[j] == '\\':
                    j += 1
                j += 1

            if j < n:
                # 找到配对，转换为双引号
                result.append('"')
                result.append(json_str[i+1:j])
                result.append('"')
                i = j + 1
                continue

        result.append(char)
        i += 1

    return ''.join(result)


def extract_json_object(text: str) -> Optional[str]:
    """从文本中提取第一个完整的 JSON 对象

    Args:
        text: 可能包含 JSON 的文本

    Returns:
        提取的 JSON 字符串，未找到返回 None
    """
    # 查找第一个 { 或 [
    start = -1
    start_char = None

    for i, char in enumerate(text):
        if char == '{':
            start = i
            start_char = '{'
            break
        elif char == '[':
            start = i
            start_char = '['
            break

    if start == -1:
        return None

    end_char = '}' if start_char == '{' else ']'

    # 查找匹配的结束括号
    depth = 0
    in_string = False
    escape_next = False

    for i in range(start, len(text)):
        char = text[i]

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

        if char == start_char:
            depth += 1
        elif char == end_char:
            depth -= 1
            if depth == 0:
                return text[start:i+1]

    # 未找到完整对象，返回从开始到结尾
    return text[start:]


def find_json_end(text: str, start: int = 0) -> int:
    """查找 JSON 对象的结束位置

    Args:
        text: 文本
        start: 起始位置

    Returns:
        结束位置（不包含），未找到返回 -1
    """
    if start >= len(text):
        return -1

    # 确定开始字符
    start_char = text[start]
    if start_char == '{':
        end_char = '}'
    elif start_char == '[':
        end_char = ']'
    else:
        return -1

    depth = 0
    in_string = False
    escape_next = False

    for i in range(start, len(text)):
        char = text[i]

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

        if char == start_char:
            depth += 1
        elif char == end_char:
            depth -= 1
            if depth == 0:
                return i + 1

    return -1


def merge_json_objects(obj1: dict, obj2: dict, deep: bool = True) -> dict:
    """合并两个 JSON 对象

    Args:
        obj1: 第一个对象
        obj2: 第二个对象
        deep: 是否深度合并

    Returns:
        合并后的对象
    """
    result = obj1.copy()

    for key, value in obj2.items():
        if key in result and deep:
            if isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = merge_json_objects(result[key], value, deep)
            elif isinstance(result[key], list) and isinstance(value, list):
                result[key] = result[key] + value
            else:
                result[key] = value
        else:
            result[key] = value

    return result


def truncate_json_string(json_str: str, max_length: int, suffix: str = "...") -> str:
    """截断 JSON 字符串，保持有效性

    Args:
        json_str: JSON 字符串
        max_length: 最大长度
        suffix: 截断后缀

    Returns:
        截断后的字符串
    """
    if len(json_str) <= max_length:
        return json_str

    # 尝试解析并重新序列化
    try:
        obj = json.loads(json_str)
        if isinstance(obj, dict):
            # 逐步移除键直到满足长度要求
            keys = list(obj.keys())
            while keys and len(json.dumps(obj, ensure_ascii=False)) > max_length:
                del obj[keys.pop()]
            return json.dumps(obj, ensure_ascii=False)
        elif isinstance(obj, list):
            # 逐步移除元素
            while obj and len(json.dumps(obj, ensure_ascii=False)) > max_length:
                obj.pop()
            return json.dumps(obj, ensure_ascii=False)
    except json.JSONDecodeError:
        pass

    # 简单截断
    return json_str[:max_length - len(suffix)] + suffix
