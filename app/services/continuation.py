"""响应续传服务

处理被截断的响应，自动检测并续传。
"""

import re
import json
from typing import Optional, Tuple
from dataclasses import dataclass, field

from ..config import get_settings, ContinuationConfig
from ..utils.logging import get_logger
from ..utils.json_parser import find_json_end
from ..utils.tool_parser import has_incomplete_tool_call

logger = get_logger(__name__)


@dataclass
class TruncationInfo:
    """截断信息"""
    is_truncated: bool = False
    reason: Optional[str] = None
    position: int = 0
    truncated_ending: str = ""
    confidence: float = 0.0


@dataclass
class ContinuationResult:
    """续传结果"""
    success: bool = False
    combined_text: str = ""
    continuation_count: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    final_stop_reason: str = "end_turn"
    errors: list[str] = field(default_factory=list)


class TruncationDetector:
    """截断检测器

    检测响应是否被截断，支持多种检测策略：
    1. stop_reason 检测
    2. 未闭合的代码块
    3. 未完成的工具调用
    4. 未闭合的括号/引号
    5. 句子中断检测
    """

    def __init__(self, config: Optional[ContinuationConfig] = None):
        self.config = config or get_settings().continuation

        # 代码块模式
        self._code_block_pattern = re.compile(r'```(\w*)\n?')

        # 句子结束模式
        self._sentence_end_pattern = re.compile(r'[.!?。！？]\s*$')

        # 列表项模式
        self._list_item_pattern = re.compile(r'^\s*[-*\d]+[.)\s]', re.MULTILINE)

    def detect(self, text: str, stop_reason: Optional[str] = None) -> TruncationInfo:
        """检测文本是否被截断

        Args:
            text: 响应文本
            stop_reason: API 返回的停止原因

        Returns:
            TruncationInfo
        """
        if not text:
            return TruncationInfo()

        # 检查各种截断情况
        checks = [
            self._check_stop_reason(stop_reason),
            self._check_code_blocks(text),
            self._check_tool_calls(text),
            self._check_brackets(text),
            self._check_sentence_completion(text),
        ]

        # 返回置信度最高的检测结果
        truncated_checks = [c for c in checks if c.is_truncated]

        if not truncated_checks:
            return TruncationInfo()

        # 按置信度排序
        truncated_checks.sort(key=lambda x: x.confidence, reverse=True)
        result = truncated_checks[0]

        # 提取截断结尾
        result.truncated_ending = self._extract_truncated_ending(text)

        logger.debug(
            f"Truncation detected: reason={result.reason}, "
            f"confidence={result.confidence:.2f}"
        )

        return result

    def _check_stop_reason(self, stop_reason: Optional[str]) -> TruncationInfo:
        """检查停止原因"""
        if stop_reason == "max_tokens":
            return TruncationInfo(
                is_truncated=True,
                reason="max_tokens",
                confidence=1.0,
            )
        return TruncationInfo()

    def _check_code_blocks(self, text: str) -> TruncationInfo:
        """检查未闭合的代码块"""
        # 计算代码块开始和结束标记
        opens = len(self._code_block_pattern.findall(text))
        closes = text.count('```') - opens

        # 简化：计算 ``` 总数
        total_markers = text.count('```')

        if total_markers % 2 == 1:
            return TruncationInfo(
                is_truncated=True,
                reason="unclosed_code_block",
                confidence=0.95,
            )

        return TruncationInfo()

    def _check_tool_calls(self, text: str) -> TruncationInfo:
        """检查未完成的工具调用"""
        if has_incomplete_tool_call(text):
            return TruncationInfo(
                is_truncated=True,
                reason="incomplete_tool_call",
                confidence=0.9,
            )
        return TruncationInfo()

    def _check_brackets(self, text: str) -> TruncationInfo:
        """检查未闭合的括号"""
        # 只检查最后 1000 个字符
        check_text = text[-1000:] if len(text) > 1000 else text

        stack = []
        in_string = False
        escape_next = False
        bracket_pairs = {'{': '}', '[': ']', '(': ')'}

        for char in check_text:
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

            if char in bracket_pairs:
                stack.append(bracket_pairs[char])
            elif char in bracket_pairs.values():
                if stack and stack[-1] == char:
                    stack.pop()

        if stack:
            return TruncationInfo(
                is_truncated=True,
                reason="unclosed_brackets",
                confidence=0.7,
            )

        return TruncationInfo()

    def _check_sentence_completion(self, text: str) -> TruncationInfo:
        """检查句子是否完整"""
        # 获取最后一行
        lines = text.strip().split('\n')
        if not lines:
            return TruncationInfo()

        last_line = lines[-1].strip()

        # 如果最后一行是代码或列表项，不检查句子完整性
        if last_line.startswith('```') or self._list_item_pattern.match(last_line):
            return TruncationInfo()

        # 检查是否以句子结束符结尾
        if last_line and not self._sentence_end_pattern.search(last_line):
            # 检查是否在单词中间截断
            if last_line and last_line[-1].isalnum():
                return TruncationInfo(
                    is_truncated=True,
                    reason="incomplete_sentence",
                    confidence=0.5,
                )

        return TruncationInfo()

    def _extract_truncated_ending(
        self,
        text: str,
        max_chars: Optional[int] = None
    ) -> str:
        """提取截断结尾

        用于续传时提供上下文。
        """
        max_chars = max_chars or self.config.truncated_ending_chars

        if len(text) <= max_chars:
            return text

        # 尝试在合适的位置截断
        ending = text[-max_chars:]

        # 尝试从行首开始
        newline_pos = ending.find('\n')
        if newline_pos > 0 and newline_pos < max_chars // 2:
            ending = ending[newline_pos + 1:]

        return ending


class ContinuationHandler:
    """续传处理器

    处理被截断的响应，自动发起续传请求。
    """

    def __init__(self, config: Optional[ContinuationConfig] = None):
        self.config = config or get_settings().continuation
        self.detector = TruncationDetector(config)

    def should_continue(
        self,
        text: str,
        stop_reason: Optional[str],
        continuation_count: int
    ) -> Tuple[bool, Optional[TruncationInfo]]:
        """判断是否需要续传

        Args:
            text: 当前响应文本
            stop_reason: 停止原因
            continuation_count: 已续传次数

        Returns:
            (是否续传, 截断信息)
        """
        if not self.config.enabled:
            return False, None

        if continuation_count >= self.config.max_continuations:
            logger.warning(
                f"Max continuations reached: {continuation_count}"
            )
            return False, None

        # 检测截断
        truncation = self.detector.detect(text, stop_reason)

        if not truncation.is_truncated:
            return False, None

        # 检查触发条件
        triggers = self.config.triggers

        if truncation.reason == "max_tokens" and not triggers.max_tokens_reached:
            return False, None

        if truncation.reason == "incomplete_tool_call" and not triggers.incomplete_tool_json:
            return False, None

        return True, truncation

    def build_continuation_request(
        self,
        original_request: dict,
        accumulated_text: str,
        truncation: TruncationInfo
    ) -> dict:
        """构建续传请求

        Args:
            original_request: 原始请求
            accumulated_text: 已累积的响应文本
            truncation: 截断信息

        Returns:
            续传请求
        """
        # 复制原始请求
        request = original_request.copy()
        messages = list(request.get("messages", []))

        # 添加助手的部分响应
        messages.append({
            "role": "assistant",
            "content": accumulated_text
        })

        # 添加续传提示
        continuation_prompt = self.config.continuation_prompt.format(
            truncated_ending=truncation.truncated_ending
        )

        messages.append({
            "role": "user",
            "content": continuation_prompt
        })

        request["messages"] = messages

        # 调整 max_tokens
        request["max_tokens"] = self.config.continuation_max_tokens

        return request

    def merge_responses(
        self,
        original_text: str,
        continuation_text: str,
        truncation: TruncationInfo
    ) -> str:
        """合并原始响应和续传响应

        Args:
            original_text: 原始响应文本
            continuation_text: 续传响应文本
            truncation: 截断信息

        Returns:
            合并后的文本
        """
        if not continuation_text:
            return original_text

        # 清理续传文本开头可能的重复内容
        cleaned_continuation = self._remove_overlap(
            original_text,
            continuation_text
        )

        # 根据截断类型决定合并方式
        if truncation.reason == "unclosed_code_block":
            # 代码块续传，直接拼接
            return original_text + cleaned_continuation

        if truncation.reason == "incomplete_tool_call":
            # 工具调用续传，需要特殊处理
            return self._merge_tool_call(original_text, cleaned_continuation)

        # 默认：直接拼接
        return original_text + cleaned_continuation

    def _remove_overlap(self, original: str, continuation: str) -> str:
        """移除续传文本开头与原始文本结尾的重叠部分"""
        if not original or not continuation:
            return continuation

        # 获取原始文本结尾
        ending = original[-200:] if len(original) > 200 else original

        # 查找重叠
        for i in range(min(len(ending), len(continuation)), 0, -1):
            if ending.endswith(continuation[:i]):
                return continuation[i:]

        # 检查续传是否以原始结尾的部分内容开始
        for i in range(min(50, len(continuation)), 0, -1):
            if continuation[:i] in ending:
                # 找到重叠位置
                pos = ending.find(continuation[:i])
                overlap_len = len(ending) - pos
                if overlap_len <= len(continuation):
                    return continuation[overlap_len:]

        return continuation

    def _merge_tool_call(self, original: str, continuation: str) -> str:
        """合并工具调用

        处理 JSON 参数的续传。
        """
        # 查找原始文本中未完成的 JSON
        last_brace = original.rfind('{')
        if last_brace == -1:
            return original + continuation

        # 检查 JSON 是否完整
        json_part = original[last_brace:]
        end_pos = find_json_end(json_part, 0)

        if end_pos > 0:
            # JSON 已完整，直接拼接
            return original + continuation

        # JSON 不完整，尝试合并
        # 假设续传从 JSON 中断处继续
        return original + continuation


# 便捷函数
def detect_truncation(text: str, stop_reason: Optional[str] = None) -> TruncationInfo:
    """检测截断"""
    return TruncationDetector().detect(text, stop_reason)


def should_continue_response(
    text: str,
    stop_reason: Optional[str],
    continuation_count: int = 0
) -> Tuple[bool, Optional[TruncationInfo]]:
    """判断是否需要续传"""
    return ContinuationHandler().should_continue(text, stop_reason, continuation_count)
