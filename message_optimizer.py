"""
Message Optimizer - 消息优化器模块

功能：
1. 智能消息压缩 - 在保留关键上下文的同时减少 token 消耗
2. 续传优化 - 更智能的截断检测和续传请求构建
3. 工具调用优化 - 压缩大型工具输出
4. 上下文管理 - Token 估算和消息优先级排序

设计原则：
- 保留语义完整性
- 最小化信息损失
- 优化 Claude Code CLI 兼容性
"""

import re
import json
import logging
from typing import Optional, List, Dict, Tuple, Any
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger("message_optimizer")


# ==================== 配置 ====================

class CompressionLevel(Enum):
    """压缩级别"""
    NONE = 0        # 不压缩
    LIGHT = 1       # 轻度压缩：移除空白、格式化
    MEDIUM = 2      # 中度压缩：压缩工具输出、移除重复
    AGGRESSIVE = 3  # 激进压缩：摘要化、移除非关键内容


@dataclass
class OptimizerConfig:
    """优化器配置"""
    # 压缩配置
    compression_level: CompressionLevel = CompressionLevel.MEDIUM
    max_total_chars: int = 100000           # 最大总字符数
    max_single_message_chars: int = 30000   # 单条消息最大字符数
    max_tool_output_chars: int = 10000      # 工具输出最大字符数

    # 保留配置
    keep_recent_messages: int = 10          # 保留最近 N 条消息不压缩
    keep_system_message: bool = True        # 始终保留系统消息
    keep_tool_calls: bool = True            # 保留工具调用结构

    # 续传配置
    max_continuations: int = 5              # 最大续传次数
    continuation_max_tokens: int = 16384    # 续传请求的 max_tokens
    truncated_ending_chars: int = 500       # 截断结尾保留字符数

    # Token 估算配置
    chars_per_token: float = 4.0            # 字符/token 估算比例
    token_safety_margin: float = 0.9        # Token 安全边际


@dataclass
class TruncationInfo:
    """截断检测信息"""
    is_truncated: bool = False
    reason: str = ""
    truncated_text: str = ""
    stream_completed: bool = True
    finish_reason: str = "end_turn"
    confidence: float = 1.0  # 检测置信度 (0-1)
    valid_tool_uses: List[Dict] = field(default_factory=list)
    failed_tool_uses: List[Dict] = field(default_factory=list)


@dataclass
class OptimizationResult:
    """优化结果"""
    messages: List[Dict]
    original_count: int
    optimized_count: int
    original_chars: int
    optimized_chars: int
    compression_ratio: float
    actions_taken: List[str] = field(default_factory=list)


# ==================== 消息优化器 ====================

class MessageOptimizer:
    """消息优化器 - 智能压缩和优化消息"""

    def __init__(self, config: Optional[OptimizerConfig] = None):
        self.config = config or OptimizerConfig()
        self._compression_stats = {
            "total_optimizations": 0,
            "total_chars_saved": 0,
            "total_messages_compressed": 0,
        }

    def optimize(self, messages: List[Dict], target_chars: Optional[int] = None) -> OptimizationResult:
        """
        优化消息列表

        Args:
            messages: 原始消息列表
            target_chars: 目标字符数（可选，默认使用配置）

        Returns:
            OptimizationResult: 优化结果
        """
        target = target_chars or self.config.max_total_chars
        original_chars = self._count_chars(messages)
        original_count = len(messages)
        actions = []

        # 如果已经在限制内，不需要优化
        if original_chars <= target:
            return OptimizationResult(
                messages=messages,
                original_count=original_count,
                optimized_count=len(messages),
                original_chars=original_chars,
                optimized_chars=original_chars,
                compression_ratio=1.0,
                actions_taken=["no_optimization_needed"]
            )

        optimized = list(messages)

        # 阶段 1：压缩工具输出
        if self.config.compression_level.value >= CompressionLevel.LIGHT.value:
            optimized, tool_actions = self._compress_tool_outputs(optimized)
            actions.extend(tool_actions)

        # 阶段 2：移除冗余内容
        if self.config.compression_level.value >= CompressionLevel.MEDIUM.value:
            optimized, redundancy_actions = self._remove_redundancy(optimized)
            actions.extend(redundancy_actions)

        # 阶段 3：截断旧消息
        current_chars = self._count_chars(optimized)
        if current_chars > target:
            optimized, truncate_actions = self._truncate_old_messages(optimized, target)
            actions.extend(truncate_actions)

        # 阶段 4：激进压缩（如果仍然超限）
        current_chars = self._count_chars(optimized)
        if current_chars > target and self.config.compression_level == CompressionLevel.AGGRESSIVE:
            optimized, aggressive_actions = self._aggressive_compress(optimized, target)
            actions.extend(aggressive_actions)

        optimized_chars = self._count_chars(optimized)
        compression_ratio = optimized_chars / original_chars if original_chars > 0 else 1.0

        # 更新统计
        self._compression_stats["total_optimizations"] += 1
        self._compression_stats["total_chars_saved"] += (original_chars - optimized_chars)
        self._compression_stats["total_messages_compressed"] += (original_count - len(optimized))

        return OptimizationResult(
            messages=optimized,
            original_count=original_count,
            optimized_count=len(optimized),
            original_chars=original_chars,
            optimized_chars=optimized_chars,
            compression_ratio=compression_ratio,
            actions_taken=actions
        )

    def _compress_tool_outputs(self, messages: List[Dict]) -> Tuple[List[Dict], List[str]]:
        """压缩工具输出"""
        actions = []
        result = []

        for i, msg in enumerate(messages):
            # 跳过最近的消息
            if i >= len(messages) - self.config.keep_recent_messages:
                result.append(msg)
                continue

            content = msg.get("content", "")

            # 处理工具结果消息
            if msg.get("role") == "user" and isinstance(content, list):
                compressed_content = []
                for item in content:
                    if item.get("type") == "tool_result":
                        tool_content = item.get("content", "")
                        if isinstance(tool_content, str) and len(tool_content) > self.config.max_tool_output_chars:
                            # 压缩工具输出
                            compressed = self._compress_tool_content(tool_content)
                            item = dict(item)
                            item["content"] = compressed
                            actions.append(f"compressed_tool_output_{item.get('tool_use_id', 'unknown')[:8]}")
                    compressed_content.append(item)
                msg = dict(msg)
                msg["content"] = compressed_content

            # 处理普通字符串内容
            elif isinstance(content, str) and len(content) > self.config.max_single_message_chars:
                msg = dict(msg)
                msg["content"] = self._truncate_with_summary(content, self.config.max_single_message_chars)
                actions.append(f"truncated_message_{i}")

            result.append(msg)

        return result, actions

    def _compress_tool_content(self, content: str) -> str:
        """压缩工具内容"""
        max_chars = self.config.max_tool_output_chars

        if len(content) <= max_chars:
            return content

        # 尝试智能压缩
        # 1. 如果是 JSON，尝试压缩
        if content.strip().startswith('{') or content.strip().startswith('['):
            try:
                data = json.loads(content)
                # 压缩 JSON
                compressed = json.dumps(data, separators=(',', ':'), ensure_ascii=False)
                if len(compressed) <= max_chars:
                    return compressed
            except json.JSONDecodeError:
                pass

        # 2. 如果是代码输出，保留头尾
        head_size = max_chars // 3
        tail_size = max_chars // 3
        middle_marker = f"\n\n... [{len(content) - head_size - tail_size} chars truncated] ...\n\n"

        return content[:head_size] + middle_marker + content[-tail_size:]

    def _truncate_with_summary(self, content: str, max_chars: int) -> str:
        """截断内容并添加摘要"""
        if len(content) <= max_chars:
            return content

        # 保留开头和结尾
        head_size = int(max_chars * 0.6)
        tail_size = int(max_chars * 0.3)

        truncated_size = len(content) - head_size - tail_size
        marker = f"\n\n[... {truncated_size} characters truncated ...]\n\n"

        return content[:head_size] + marker + content[-tail_size:]

    def _remove_redundancy(self, messages: List[Dict]) -> Tuple[List[Dict], List[str]]:
        """移除冗余内容"""
        actions = []
        result = []
        seen_contents = set()

        for i, msg in enumerate(messages):
            # 始终保留系统消息和最近消息
            if msg.get("role") == "system":
                result.append(msg)
                continue

            if i >= len(messages) - self.config.keep_recent_messages:
                result.append(msg)
                continue

            # 检查重复内容
            content = msg.get("content", "")
            if isinstance(content, str):
                content_hash = hash(content[:500])  # 只比较前 500 字符
                if content_hash in seen_contents:
                    actions.append(f"removed_duplicate_{i}")
                    continue
                seen_contents.add(content_hash)

            result.append(msg)

        return result, actions

    def _truncate_old_messages(self, messages: List[Dict], target_chars: int) -> Tuple[List[Dict], List[str]]:
        """截断旧消息"""
        actions = []

        # 分离系统消息和普通消息
        system_msg = None
        regular_messages = []

        for msg in messages:
            if msg.get("role") == "system":
                system_msg = msg
            else:
                regular_messages.append(msg)

        # 从最旧的消息开始移除
        current_chars = self._count_chars(messages)
        removed_count = 0

        while current_chars > target_chars and len(regular_messages) > self.config.keep_recent_messages:
            removed_msg = regular_messages.pop(0)
            removed_count += 1
            current_chars = self._count_chars([system_msg] if system_msg else []) + self._count_chars(regular_messages)

        if removed_count > 0:
            actions.append(f"removed_{removed_count}_old_messages")

        # 重组消息
        result = []
        if system_msg and self.config.keep_system_message:
            result.append(system_msg)
        result.extend(regular_messages)

        return result, actions

    def _aggressive_compress(self, messages: List[Dict], target_chars: int) -> Tuple[List[Dict], List[str]]:
        """激进压缩"""
        actions = []
        result = []

        for msg in messages:
            content = msg.get("content", "")

            if isinstance(content, str):
                # 移除多余空白
                compressed = re.sub(r'\s+', ' ', content)
                # 移除代码注释
                compressed = re.sub(r'//.*$', '', compressed, flags=re.MULTILINE)
                compressed = re.sub(r'#.*$', '', compressed, flags=re.MULTILINE)

                if len(compressed) < len(content):
                    msg = dict(msg)
                    msg["content"] = compressed
                    actions.append("aggressive_whitespace_removal")

            result.append(msg)

        return result, actions

    def _count_chars(self, messages: List[Dict]) -> int:
        """计算消息总字符数"""
        total = 0
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                total += len(content)
            elif isinstance(content, list):
                for item in content:
                    if isinstance(item, dict):
                        total += len(str(item.get("content", "")))
                        total += len(str(item.get("text", "")))
        return total

    def estimate_tokens(self, text: str) -> int:
        """估算 token 数"""
        return int(len(text) / self.config.chars_per_token)

    def get_stats(self) -> Dict:
        """获取压缩统计"""
        return dict(self._compression_stats)


# ==================== 截断检测器 ====================

class TruncationDetector:
    """截断检测器 - 智能检测响应是否被截断"""

    def __init__(self):
        # 检测模式及其置信度
        self._patterns = {
            # 高置信度模式 (0.9+)
            "code_block_unclosed": (r'```[^`]*$', 0.95),
            "json_brace_unclosed": (r'\{[^}]*$', 0.85),
            "sql_insert_incomplete": (r'\bINSERT\s+INTO\s+\w+\s*\([^)]*$', 0.90),
            "sql_values_incomplete": (r'\bVALUES\s*\([^)]*$', 0.90),

            # 中置信度模式 (0.7-0.9)
            "function_def_incomplete": (r'function\s+\w+\s*\([^)]*$', 0.80),
            "arrow_function_incomplete": (r'=>\s*\{[^}]*$', 0.80),

            # 工具调用相关 (高置信度)
            "tool_call_json_incomplete": (r'\[Calling tool:.*\{[^}]*$', 0.95),
        }

    def detect(self,
               full_text: str,
               stream_completed: bool,
               finish_reason: str,
               request_id: str = "") -> TruncationInfo:
        """
        检测响应是否被截断

        Args:
            full_text: 完整响应文本
            stream_completed: 流是否正常完成
            finish_reason: 结束原因
            request_id: 请求 ID（用于日志）

        Returns:
            TruncationInfo: 截断检测信息
        """
        info = TruncationInfo()
        info.truncated_text = full_text
        info.stream_completed = stream_completed
        info.finish_reason = finish_reason

        # 检测 1: 流未正常完成（最高优先级）
        if not stream_completed:
            info.is_truncated = True
            info.reason = "stream_interrupted"
            info.confidence = 1.0
            logger.warning(f"[{request_id}] 截断检测: 流未正常完成")
            return info

        # 检测 2: finish_reason 表示达到上限
        if finish_reason in ("max_tokens", "length"):
            info.is_truncated = True
            info.reason = "max_tokens_reached"
            info.confidence = 1.0
            logger.warning(f"[{request_id}] 截断检测: finish_reason={finish_reason}")
            return info

        # 检测 3: 代码块未闭合
        code_fence_count = full_text.count("```")
        if code_fence_count % 2 != 0:
            info.is_truncated = True
            info.reason = f"incomplete_code_block (fence_count: {code_fence_count})"
            info.confidence = 0.95
            logger.warning(f"[{request_id}] 截断检测: 代码块未闭合")
            return info

        # 检测 4: 工具调用 JSON 括号不匹配
        if "[Calling tool:" in full_text:
            open_braces = full_text.count('{')
            close_braces = full_text.count('}')
            if open_braces > close_braces:
                info.is_truncated = True
                info.reason = f"incomplete_json (braces: {open_braces} open, {close_braces} close)"
                info.confidence = 0.90
                logger.warning(f"[{request_id}] 截断检测: JSON括号不匹配")
                return info

        # 检测 5: 模式匹配（仅在高置信度时触发）
        if len(full_text) > 100:
            last_200_chars = full_text[-200:]
            for pattern_name, (pattern, confidence) in self._patterns.items():
                if re.search(pattern, last_200_chars, re.IGNORECASE | re.DOTALL):
                    if confidence >= 0.85:  # 只触发高置信度模式
                        info.is_truncated = True
                        info.reason = f"pattern_match ({pattern_name})"
                        info.confidence = confidence
                        logger.warning(f"[{request_id}] 截断检测: 模式匹配 - {pattern_name} (置信度: {confidence})")
                        return info

        return info

    def should_continue(self, info: TruncationInfo, continuation_count: int, max_continuations: int) -> bool:
        """
        判断是否应该续传

        Args:
            info: 截断检测信息
            continuation_count: 当前续传次数
            max_continuations: 最大续传次数

        Returns:
            bool: 是否应该续传
        """
        if not info.is_truncated:
            return False

        if continuation_count >= max_continuations:
            return False

        # 高置信度截断必须续传
        if info.confidence >= 0.9:
            return True

        # 中置信度截断，根据续传次数决定
        if info.confidence >= 0.7 and continuation_count < max_continuations - 1:
            return True

        return False


# ==================== 续传请求构建器 ====================

class ContinuationBuilder:
    """续传请求构建器"""

    def __init__(self, config: Optional[OptimizerConfig] = None):
        self.config = config or OptimizerConfig()

        # 续传提示模板
        self._prompt_template = """Your response was truncated. Continue EXACTLY from where you stopped.

RULES:
- Do NOT repeat any content
- Do NOT add preambles
- Continue the exact code/JSON/text that was cut off
- Stay in the same context (code block, function, etc.)

Your response ended with:
```
{truncated_ending}
```

Continue directly:"""

    def build(self,
              original_messages: List[Dict],
              truncated_text: str,
              original_body: Dict,
              continuation_count: int,
              request_id: str = "") -> Dict:
        """
        构建续传请求

        Args:
            original_messages: 原始消息列表
            truncated_text: 截断的响应文本
            original_body: 原始请求体
            continuation_count: 续传计数
            request_id: 请求 ID

        Returns:
            Dict: 续传请求体
        """
        # 获取截断结尾
        ending_chars = self.config.truncated_ending_chars
        truncated_ending = truncated_text[-ending_chars:] if len(truncated_text) > ending_chars else truncated_text

        # 构建续传提示
        continuation_prompt = self._prompt_template.format(truncated_ending=truncated_ending)

        # 构建新的消息列表
        new_messages = list(original_messages)

        # 添加截断的 assistant 响应
        new_messages.append({
            "role": "assistant",
            "content": truncated_text
        })

        # 添加续传提示
        new_messages.append({
            "role": "user",
            "content": continuation_prompt
        })

        # 构建新的请求体
        new_body = dict(original_body)
        new_body["messages"] = new_messages
        new_body["max_tokens"] = self.config.continuation_max_tokens

        logger.info(f"[{request_id}] 构建续传请求 #{continuation_count + 1}: "
                    f"原始消息={len(original_messages)}, 新消息={len(new_messages)}, "
                    f"截断文本长度={len(truncated_text)}")

        return new_body

    def merge_responses(self, original_text: str, continuation_text: str, request_id: str = "") -> str:
        """
        合并原始响应和续传响应

        Args:
            original_text: 原始响应文本
            continuation_text: 续传响应文本
            request_id: 请求 ID

        Returns:
            str: 合并后的文本
        """
        if not continuation_text:
            return original_text

        continuation_clean = continuation_text.strip()

        # 检测重叠
        overlap_check_len = min(100, len(original_text))
        original_ending = original_text[-overlap_check_len:]

        for i in range(len(original_ending), 0, -1):
            if continuation_clean.startswith(original_ending[-i:]):
                continuation_clean = continuation_clean[i:]
                logger.info(f"[{request_id}] 合并响应: 检测到 {i} 字符重叠，已去除")
                break

        # 智能拼接
        merged = original_text + continuation_clean

        logger.info(f"[{request_id}] 合并响应: 原始={len(original_text)}, "
                    f"续传={len(continuation_text)}, 合并后={len(merged)}")

        return merged


# ==================== 工具输出压缩器 ====================

class ToolOutputCompressor:
    """工具输出压缩器 - 专门处理大型工具输出"""

    def __init__(self, max_chars: int = 10000):
        self.max_chars = max_chars

        # 不同类型输出的压缩策略
        self._strategies = {
            "json": self._compress_json,
            "code": self._compress_code,
            "log": self._compress_log,
            "text": self._compress_text,
        }

    def compress(self, content: str, content_type: str = "auto") -> str:
        """
        压缩工具输出

        Args:
            content: 工具输出内容
            content_type: 内容类型 (json/code/log/text/auto)

        Returns:
            str: 压缩后的内容
        """
        if len(content) <= self.max_chars:
            return content

        # 自动检测类型
        if content_type == "auto":
            content_type = self._detect_type(content)

        strategy = self._strategies.get(content_type, self._compress_text)
        return strategy(content)

    def _detect_type(self, content: str) -> str:
        """检测内容类型"""
        stripped = content.strip()

        if stripped.startswith('{') or stripped.startswith('['):
            return "json"

        if any(kw in content[:500] for kw in ['def ', 'function ', 'class ', 'import ', 'const ', 'let ', 'var ']):
            return "code"

        if re.search(r'\d{4}-\d{2}-\d{2}.*?(INFO|DEBUG|ERROR|WARN)', content[:500]):
            return "log"

        return "text"

    def _compress_json(self, content: str) -> str:
        """压缩 JSON"""
        try:
            data = json.loads(content)
            # 压缩 JSON 格式
            compressed = json.dumps(data, separators=(',', ':'), ensure_ascii=False)

            if len(compressed) <= self.max_chars:
                return compressed

            # 仍然太大，截断
            return self._truncate_with_marker(compressed)
        except json.JSONDecodeError:
            return self._truncate_with_marker(content)

    def _compress_code(self, content: str) -> str:
        """压缩代码"""
        # 移除空行和注释
        lines = content.split('\n')
        compressed_lines = []

        for line in lines:
            stripped = line.strip()
            # 跳过空行和纯注释行
            if not stripped or stripped.startswith('#') or stripped.startswith('//'):
                continue
            compressed_lines.append(line)

        compressed = '\n'.join(compressed_lines)

        if len(compressed) <= self.max_chars:
            return compressed

        return self._truncate_with_marker(compressed)

    def _compress_log(self, content: str) -> str:
        """压缩日志"""
        lines = content.split('\n')

        if len(lines) <= 50:
            return self._truncate_with_marker(content)

        # 保留开头和结尾的日志行
        head_lines = lines[:20]
        tail_lines = lines[-20:]

        marker = f"\n... [{len(lines) - 40} log lines truncated] ...\n"

        return '\n'.join(head_lines) + marker + '\n'.join(tail_lines)

    def _compress_text(self, content: str) -> str:
        """压缩普通文本"""
        return self._truncate_with_marker(content)

    def _truncate_with_marker(self, content: str) -> str:
        """截断并添加标记"""
        if len(content) <= self.max_chars:
            return content

        head_size = int(self.max_chars * 0.6)
        tail_size = int(self.max_chars * 0.35)

        truncated_size = len(content) - head_size - tail_size
        marker = f"\n\n[... {truncated_size} chars truncated ...]\n\n"

        return content[:head_size] + marker + content[-tail_size:]


# ==================== 导出 ====================

__all__ = [
    'MessageOptimizer',
    'TruncationDetector',
    'ContinuationBuilder',
    'ToolOutputCompressor',
    'OptimizerConfig',
    'TruncationInfo',
    'OptimizationResult',
    'CompressionLevel',
]
