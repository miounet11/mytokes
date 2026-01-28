"""历史消息管理器

核心模块，提供对话历史消息的智能管理功能，包括：
- 自动截断
- 智能摘要
- 错误重试处理
- Token 预估检测
"""

import json
import logging
from typing import Any, Callable, Optional

from .cache import SummaryCache
from .config import HistoryConfig, TruncateStrategy
from .utils import format_history_for_summary, summarize_history_structure

# 模块级日志器
logger = logging.getLogger("ai_history_manager")

# 全局摘要缓存
_summary_cache = SummaryCache()


def get_summary_cache() -> SummaryCache:
    """获取全局摘要缓存实例"""
    return _summary_cache


class HistoryManager:
    """历史消息管理器

    提供对话历史消息的智能管理功能，支持多种处理策略。

    使用示例:
    ```python
    from ai_history_manager import HistoryManager, HistoryConfig, TruncateStrategy

    config = HistoryConfig(
        strategies=[TruncateStrategy.ERROR_RETRY, TruncateStrategy.SMART_SUMMARY],
        max_messages=30
    )

    manager = HistoryManager(config, cache_key="session_123")

    # 同步预处理
    processed = manager.pre_process(history, user_content)

    # 异步预处理（支持摘要）
    processed = await manager.pre_process_async(history, user_content, summary_generator)

    # 处理长度错误
    truncated, should_retry = await manager.handle_length_error_async(history, retry_count)

    if manager.was_truncated:
        print(f"历史被截断: {manager.truncate_info}")
    ```
    """

    def __init__(
        self,
        config: Optional[HistoryConfig] = None,
        cache_key: Optional[str] = None,
    ):
        """初始化管理器

        Args:
            config: 历史消息配置，为 None 时使用默认配置
            cache_key: 摘要缓存键（通常是会话 ID）
        """
        self.config = config or HistoryConfig()
        self._truncated = False
        self._truncate_info = ""
        self.cache_key = cache_key

        # 配置日志
        if self.config.logging_enabled:
            level = getattr(logging, self.config.logging_level.upper(), logging.INFO)
            logger.setLevel(level)

    @property
    def was_truncated(self) -> bool:
        """是否发生了截断"""
        return self._truncated

    @property
    def truncate_info(self) -> str:
        """截断信息"""
        return self._truncate_info

    def reset(self) -> None:
        """重置状态"""
        self._truncated = False
        self._truncate_info = ""

    def set_cache_key(self, cache_key: Optional[str]) -> None:
        """设置摘要缓存键

        Args:
            cache_key: 缓存键（通常是会话 ID）
        """
        self.cache_key = cache_key

    def _summary_cache_key(self, target_count: int) -> Optional[str]:
        """生成摘要缓存键"""
        if not self.cache_key:
            return None
        return f"{self.cache_key}:{target_count}"

    # ==================== 估算方法 ====================

    def estimate_tokens(self, text: str) -> int:
        """估算 token 数量

        Args:
            text: 文本内容

        Returns:
            估算的 token 数
        """
        return int(len(text) / self.config.chars_per_token)

    def estimate_history_size(self, history: list[dict]) -> tuple[int, int]:
        """估算历史消息大小

        Args:
            history: 历史消息列表

        Returns:
            (消息数, 字符数)
        """
        char_count = len(json.dumps(history, ensure_ascii=False))
        return len(history), char_count

    def estimate_request_chars(
        self, history: list[dict], user_content: str = ""
    ) -> tuple[int, int, int]:
        """估算请求字符数

        Args:
            history: 历史消息列表
            user_content: 用户当前消息

        Returns:
            (历史字符数, 用户消息字符数, 总字符数)
        """
        history_chars = len(json.dumps(history, ensure_ascii=False))
        user_chars = len(user_content or "")
        return history_chars, user_chars, history_chars + user_chars

    # ==================== 截断方法 ====================

    def truncate_by_count(self, history: list[dict], max_count: int) -> list[dict]:
        """按消息数量截断

        保留最近的 max_count 条消息。

        Args:
            history: 历史消息列表
            max_count: 最大消息数

        Returns:
            截断后的历史消息
        """
        if len(history) <= max_count:
            return history

        original_count = len(history)
        truncated = history[-max_count:]
        self._truncated = True
        self._truncate_info = f"按数量截断: {original_count} -> {len(truncated)} 条消息"

        if self.config.logging_enabled:
            logger.info(self._truncate_info)

        return truncated

    def truncate_by_chars(self, history: list[dict], max_chars: int) -> list[dict]:
        """按字符数截断

        从后往前累计字符数，保留不超过 max_chars 的消息。

        Args:
            history: 历史消息列表
            max_chars: 最大字符数

        Returns:
            截断后的历史消息
        """
        total_chars = len(json.dumps(history, ensure_ascii=False))
        if total_chars <= max_chars:
            return history

        original_count = len(history)
        result = []
        current_chars = 0

        for msg in reversed(history):
            msg_chars = len(json.dumps(msg, ensure_ascii=False))
            if current_chars + msg_chars > max_chars and result:
                break
            result.insert(0, msg)
            current_chars += msg_chars

        if len(result) < original_count:
            self._truncated = True
            self._truncate_info = (
                f"按字符数截断: {original_count} -> {len(result)} 条消息 "
                f"({total_chars} -> {current_chars} 字符)"
            )

            if self.config.logging_enabled:
                logger.info(self._truncate_info)

        return result

    # ==================== 摘要方法 ====================

    async def generate_summary(
        self,
        history: list[dict],
        summary_generator: Callable[[str], Any],
    ) -> Optional[str]:
        """生成历史消息摘要

        Args:
            history: 需要摘要的历史消息
            summary_generator: 摘要生成函数，签名为 async (prompt: str) -> str

        Returns:
            摘要文本，失败返回 None
        """
        if not history:
            return None

        formatted = format_history_for_summary(history)
        # 限制输入长度
        if len(formatted) > 10000:
            formatted = formatted[:10000] + "\n...(truncated)"

        prompt = f"""请简洁地总结以下对话历史的关键信息，包括：
1. 用户的主要目标和需求
2. 已完成的重要操作
3. 当前的工作状态和上下文

对话历史：
{formatted}

请用中文输出摘要，控制在 {self.config.summary_max_length} 字符以内："""

        try:
            summary = await summary_generator(prompt)
            if summary and len(summary) > self.config.summary_max_length:
                summary = summary[: self.config.summary_max_length] + "..."
            return summary
        except Exception as e:
            if self.config.logging_enabled:
                logger.warning(f"生成摘要失败: {e}")
            return None

    def _build_summary_history(
        self,
        summary: str,
        recent_history: list[dict],
        debug_label: Optional[str] = None,
    ) -> list[dict]:
        """用摘要替换旧历史，保留最近完整上下文

        关键规则：
        1. 历史必须以 user 消息开头
        2. user/assistant 必须严格交替
        3. 当 assistant 有 toolUses 时，下一条 user 必须有对应的 toolResults
        4. 当 assistant 没有 toolUses 时，下一条 user 不能有 toolResults

        Args:
            summary: 摘要文本
            recent_history: 最近的完整历史消息
            debug_label: 调试标签

        Returns:
            构建好的历史消息列表
        """
        # 检查是否为 Kiro 格式
        is_kiro_format = any(
            "userInputMessage" in h or "assistantResponseMessage" in h for h in recent_history
        )

        if is_kiro_format:
            return self._build_summary_history_kiro(summary, recent_history, debug_label)
        else:
            return self._build_summary_history_standard(summary, recent_history, debug_label)

    def _build_summary_history_kiro(
        self,
        summary: str,
        recent_history: list[dict],
        debug_label: Optional[str] = None,
    ) -> list[dict]:
        """构建 Kiro 格式的摘要历史"""
        # 如果 recent_history 以 assistant 开头，跳过它
        if recent_history and "assistantResponseMessage" in recent_history[0]:
            recent_history = recent_history[1:]

        # 收集所有 toolUse IDs
        tool_use_ids = set()
        for msg in recent_history:
            if "assistantResponseMessage" in msg:
                for tu in msg["assistantResponseMessage"].get("toolUses", []) or []:
                    tu_id = tu.get("toolUseId")
                    if tu_id:
                        tool_use_ids.add(tu_id)

        # 检查第一条 user 消息是否有 toolResults
        if recent_history and "userInputMessage" in recent_history[0]:
            ctx = recent_history[0].get("userInputMessage", {}).get("userInputMessageContext", {})
            if ctx.get("toolResults"):
                # 清除，因为摘要后的 assistant 占位消息没有 toolUses
                recent_history[0]["userInputMessage"].pop("userInputMessageContext", None)

        # 重新收集 tool_use_ids（因为可能已经修改了 recent_history）
        tool_use_ids = set()
        for msg in recent_history:
            if "assistantResponseMessage" in msg:
                for tu in msg["assistantResponseMessage"].get("toolUses", []) or []:
                    tu_id = tu.get("toolUseId")
                    if tu_id:
                        tool_use_ids.add(tu_id)

        # 过滤孤立的 toolResults
        if tool_use_ids:
            for msg in recent_history:
                if "userInputMessage" in msg:
                    ctx = msg.get("userInputMessage", {}).get("userInputMessageContext", {})
                    results = ctx.get("toolResults")
                    if results:
                        filtered = [r for r in results if r.get("toolUseId") in tool_use_ids]
                        if filtered:
                            ctx["toolResults"] = filtered
                        else:
                            ctx.pop("toolResults", None)
                        if not ctx:
                            msg["userInputMessage"].pop("userInputMessageContext", None)
        else:
            # 没有任何 toolUses，清除所有 toolResults
            for msg in recent_history:
                if "userInputMessage" in msg:
                    msg["userInputMessage"].pop("userInputMessageContext", None)

        # 获取模型 ID
        model_id = "claude-sonnet-4"
        for msg in reversed(recent_history):
            if "userInputMessage" in msg:
                model_id = msg["userInputMessage"].get("modelId", model_id)
                break
            if "assistantResponseMessage" in msg:
                model_id = msg["assistantResponseMessage"].get("modelId", model_id)
                break

        # 构建结果
        summary_msg = {
            "userInputMessage": {
                "content": f"[Earlier conversation summary]\n{summary}\n\n[Continuing from recent messages...]",
                "modelId": model_id,
                "origin": "AI_EDITOR",
            }
        }
        result = [summary_msg]
        # 占位 assistant 消息（没有 toolUses）
        result.append({"assistantResponseMessage": {"content": "I understand the context. Let's continue."}})
        result.extend(recent_history)

        if debug_label and self.config.logging_enabled:
            logger.debug(f"[{debug_label}]: {summarize_history_structure(result)}")

        return result

    def _build_summary_history_standard(
        self,
        summary: str,
        recent_history: list[dict],
        debug_label: Optional[str] = None,
    ) -> list[dict]:
        """构建标准格式（OpenAI/Anthropic）的摘要历史"""
        summary_msg = {
            "role": "user",
            "content": f"[Earlier conversation summary]\n{summary}\n\n[Continuing from recent messages...]",
        }
        result = [summary_msg]
        result.append({"role": "assistant", "content": "I understand the context. Let's continue."})
        result.extend(recent_history)

        if debug_label and self.config.logging_enabled:
            logger.debug(f"[{debug_label}]: {summarize_history_structure(result)}")

        return result

    async def compress_with_summary(
        self,
        history: list[dict],
        summary_generator: Callable[[str], Any],
    ) -> list[dict]:
        """使用智能摘要压缩历史消息

        Args:
            history: 历史消息
            summary_generator: 摘要生成函数

        Returns:
            压缩后的历史消息
        """
        total_chars = len(json.dumps(history, ensure_ascii=False))
        if total_chars <= self.config.summary_threshold:
            return history

        if len(history) <= self.config.summary_keep_recent:
            return history

        # 分离早期消息和最近消息
        keep_recent = self.config.summary_keep_recent
        old_history = history[:-keep_recent]
        recent_history = history[-keep_recent:]

        # 生成摘要
        summary = await self.generate_summary(old_history, summary_generator)

        if not summary:
            # 摘要失败，回退到简单截断
            self._truncated = True
            self._truncate_info = f"摘要生成失败，回退截断: {len(history)} -> {len(recent_history)} 条消息"
            if self.config.logging_enabled:
                logger.warning(self._truncate_info)
            return recent_history

        # 构建带摘要的历史
        result = self._build_summary_history(summary, recent_history, "智能摘要结构")

        self._truncated = True
        self._truncate_info = f"智能摘要: {len(history)} -> {len(result)} 条消息 (摘要 {len(summary)} 字符)"

        if self.config.logging_enabled:
            logger.info(self._truncate_info)

        return result

    # ==================== 判断方法 ====================

    def should_pre_truncate(self, history: list[dict], user_content: str) -> bool:
        """检查是否需要预截断

        Args:
            history: 历史消息列表
            user_content: 用户当前消息

        Returns:
            是否需要预截断
        """
        if TruncateStrategy.PRE_ESTIMATE not in self.config.strategies:
            return False

        total_chars = len(json.dumps(history, ensure_ascii=False)) + len(user_content)
        return total_chars > self.config.estimate_threshold

    def should_summarize(self, history: list[dict]) -> bool:
        """检查是否需要摘要

        Args:
            history: 历史消息列表

        Returns:
            是否需要摘要
        """
        return self.should_smart_summarize(history) or self.should_auto_truncate_summarize(history)

    def should_smart_summarize(self, history: list[dict]) -> bool:
        """检查是否需要智能摘要

        Args:
            history: 历史消息列表

        Returns:
            是否需要智能摘要
        """
        if TruncateStrategy.SMART_SUMMARY not in self.config.strategies:
            return False

        total_chars = len(json.dumps(history, ensure_ascii=False))
        return (
            total_chars > self.config.summary_threshold
            and len(history) > self.config.summary_keep_recent
        )

    def should_auto_truncate_summarize(self, history: list[dict]) -> bool:
        """检查是否需要自动截断前摘要

        Args:
            history: 历史消息列表

        Returns:
            是否需要自动截断前摘要
        """
        if TruncateStrategy.AUTO_TRUNCATE not in self.config.strategies:
            return False

        if len(history) <= 1:
            return False

        total_chars = len(json.dumps(history, ensure_ascii=False))
        return len(history) > self.config.max_messages or total_chars > self.config.max_chars

    def should_pre_summary_for_error_retry(self, history: list[dict], user_content: str = "") -> bool:
        """检查是否需要错误重试前预摘要

        Args:
            history: 历史消息列表
            user_content: 用户当前消息

        Returns:
            是否需要预摘要
        """
        if TruncateStrategy.ERROR_RETRY not in self.config.strategies:
            return False
        if not history:
            return False
        _, _, total_chars = self.estimate_request_chars(history, user_content)
        return total_chars > self.config.estimate_threshold

    # ==================== 预处理方法 ====================

    def pre_process(self, history: list[dict], user_content: str = "") -> list[dict]:
        """预处理历史消息（同步版本，不包含摘要）

        根据配置的策略进行预处理。

        Args:
            history: 历史消息列表
            user_content: 用户当前消息

        Returns:
            处理后的历史消息
        """
        self.reset()

        if not history:
            return history

        result = history

        # 策略 1: 自动截断
        if TruncateStrategy.AUTO_TRUNCATE in self.config.strategies:
            result = self.truncate_by_count(result, self.config.max_messages)
            result = self.truncate_by_chars(result, self.config.max_chars)

        # 策略 4: 预估检测
        if TruncateStrategy.PRE_ESTIMATE in self.config.strategies:
            total_chars = len(json.dumps(result, ensure_ascii=False)) + len(user_content)
            if total_chars > self.config.estimate_threshold:
                target_chars = int(self.config.estimate_threshold * 0.8)  # 留 20% 余量
                result = self.truncate_by_chars(result, target_chars)

        return result

    async def pre_process_async(
        self,
        history: list[dict],
        user_content: str = "",
        summary_generator: Optional[Callable[[str], Any]] = None,
    ) -> list[dict]:
        """预处理历史消息（异步版本，支持智能摘要）

        Args:
            history: 历史消息列表
            user_content: 用户当前消息
            summary_generator: 摘要生成函数

        Returns:
            处理后的历史消息
        """
        self.reset()

        if not history:
            return history

        result = history
        pre_summarized = False

        # 错误重试预摘要（避免首次请求直接超限）
        if TruncateStrategy.ERROR_RETRY in self.config.strategies and summary_generator:
            if self.should_pre_summary_for_error_retry(result, user_content):
                target_count = self.config.retry_max_messages
                if len(result) > target_count:
                    old_history = result[:-target_count]
                    recent_history = result[-target_count:]

                    # 尝试从缓存获取
                    cache_key = self._summary_cache_key(target_count)
                    old_count = len(old_history)
                    old_chars = len(json.dumps(old_history, ensure_ascii=False))

                    cached = None
                    if cache_key and self.config.summary_cache_enabled:
                        cached = _summary_cache.get(
                            cache_key,
                            old_count,
                            old_chars,
                            self.config.summary_cache_min_delta_messages,
                            self.config.summary_cache_min_delta_chars,
                            self.config.summary_cache_max_age_seconds,
                        )

                    if cached:
                        result = self._build_summary_history(
                            cached, recent_history, "错误重试预摘要缓存结构"
                        )
                        self._truncated = True
                        self._truncate_info = (
                            f"错误重试预摘要(缓存): {len(history)} -> {len(result)} 条消息"
                        )
                        pre_summarized = True
                    else:
                        summary = await self.generate_summary(old_history, summary_generator)
                        if summary:
                            result = self._build_summary_history(
                                summary, recent_history, "错误重试预摘要结构"
                            )
                            self._truncated = True
                            self._truncate_info = (
                                f"错误重试预摘要: {len(history)} -> {len(result)} 条消息 "
                                f"(摘要 {len(summary)} 字符)"
                            )
                            pre_summarized = True
                            if cache_key and self.config.summary_cache_enabled:
                                _summary_cache.set(cache_key, summary, old_count, old_chars)

        # 策略 2: 智能摘要
        summary_applied = False
        if TruncateStrategy.SMART_SUMMARY in self.config.strategies and summary_generator:
            if self.should_smart_summarize(result) and not pre_summarized:
                result = await self.compress_with_summary(result, summary_generator)
                summary_applied = True

        # 自动截断前摘要
        if (
            TruncateStrategy.AUTO_TRUNCATE in self.config.strategies
            and summary_generator
            and not summary_applied
            and self.should_auto_truncate_summarize(result)
        ):
            # 在自动截断前生成摘要
            if len(result) > 1 and self.config.max_messages > 2:
                keep_recent = min(len(result) - 1, self.config.max_messages - 2)
                if keep_recent > 0:
                    old_history = result[:-keep_recent]
                    recent_history = result[-keep_recent:]

                    summary = await self.generate_summary(old_history, summary_generator)
                    if summary:
                        result = self._build_summary_history(
                            summary, recent_history, "自动截断前摘要结构"
                        )
                        self._truncated = True
                        self._truncate_info = (
                            f"自动截断前摘要: {len(history)} -> {len(result)} 条消息 "
                            f"(摘要 {len(summary)} 字符)"
                        )

        # 策略 1: 自动截断
        if TruncateStrategy.AUTO_TRUNCATE in self.config.strategies:
            result = self.truncate_by_count(result, self.config.max_messages)
            result = self.truncate_by_chars(result, self.config.max_chars)

        # 策略 4: 预估检测
        if TruncateStrategy.PRE_ESTIMATE in self.config.strategies:
            total_chars = len(json.dumps(result, ensure_ascii=False)) + len(user_content)
            if total_chars > self.config.estimate_threshold:
                target_chars = int(self.config.estimate_threshold * 0.8)
                result = self.truncate_by_chars(result, target_chars)

        return result

    # ==================== 错误处理方法 ====================

    def handle_length_error(
        self, history: list[dict], retry_count: int = 0
    ) -> tuple[list[dict], bool]:
        """处理长度超限错误（同步版本，仅截断）

        Args:
            history: 历史消息列表
            retry_count: 当前重试次数

        Returns:
            (截断后的历史, 是否应该重试)
        """
        if TruncateStrategy.ERROR_RETRY not in self.config.strategies:
            return history, False

        if retry_count >= self.config.max_retries:
            return history, False

        # 根据重试次数逐步减少消息
        factor = 1.0 - (retry_count * 0.3)  # 每次减少 30%
        target_count = max(5, int(self.config.retry_max_messages * factor))

        self.reset()
        truncated = self.truncate_by_count(history, target_count)

        if len(truncated) < len(history):
            self._truncate_info = (
                f"错误重试截断 (第 {retry_count + 1} 次): "
                f"{len(history)} -> {len(truncated)} 条消息"
            )
            if self.config.logging_enabled:
                logger.info(self._truncate_info)
            return truncated, True

        return history, False

    async def handle_length_error_async(
        self,
        history: list[dict],
        retry_count: int = 0,
        summary_generator: Optional[Callable[[str], Any]] = None,
    ) -> tuple[list[dict], bool]:
        """处理长度超限错误（异步版本，优先摘要）

        Args:
            history: 历史消息列表
            retry_count: 当前重试次数
            summary_generator: 摘要生成函数

        Returns:
            (处理后的历史, 是否应该重试)
        """
        if TruncateStrategy.ERROR_RETRY not in self.config.strategies:
            return history, False

        if retry_count >= self.config.max_retries:
            return history, False

        if not history:
            return history, False

        self.reset()

        factor = 1.0 - (retry_count * 0.3)
        target_count = max(5, int(self.config.retry_max_messages * factor))

        if len(history) <= target_count:
            return history, False

        # 优先尝试摘要
        if summary_generator:
            old_history = history[:-target_count]
            recent_history = history[-target_count:]

            cache_key = self._summary_cache_key(target_count)
            old_count = len(old_history)
            old_chars = len(json.dumps(old_history, ensure_ascii=False))

            # 尝试从缓存获取
            cached = None
            if cache_key and self.config.summary_cache_enabled:
                cached = _summary_cache.get(
                    cache_key,
                    old_count,
                    old_chars,
                    self.config.summary_cache_min_delta_messages,
                    self.config.summary_cache_min_delta_chars,
                    self.config.summary_cache_max_age_seconds,
                )

            if cached:
                result = self._build_summary_history(cached, recent_history, "错误重试摘要缓存结构")
                self._truncated = True
                self._truncate_info = (
                    f"错误重试摘要(缓存) (第 {retry_count + 1} 次): "
                    f"{len(history)} -> {len(result)} 条消息"
                )
                if self.config.logging_enabled:
                    logger.info(self._truncate_info)
                return result, True

            # 生成新摘要
            summary = await self.generate_summary(old_history, summary_generator)
            if summary:
                result = self._build_summary_history(summary, recent_history, "错误重试摘要结构")
                self._truncated = True
                self._truncate_info = (
                    f"错误重试摘要 (第 {retry_count + 1} 次): "
                    f"{len(history)} -> {len(result)} 条消息 (摘要 {len(summary)} 字符)"
                )
                if cache_key and self.config.summary_cache_enabled:
                    _summary_cache.set(cache_key, summary, old_count, old_chars)
                if self.config.logging_enabled:
                    logger.info(self._truncate_info)
                return result, True

        # 摘要失败或无 summary_generator，回退到按数量截断
        self.reset()
        truncated = self.truncate_by_count(history, target_count)
        if len(truncated) < len(history):
            self._truncate_info = (
                f"错误重试截断 (第 {retry_count + 1} 次): "
                f"{len(history)} -> {len(truncated)} 条消息"
            )
            if self.config.logging_enabled:
                logger.info(self._truncate_info)
            return truncated, True

        return history, False

    # ==================== 工具方法 ====================

    def get_warning_header(self) -> Optional[str]:
        """获取截断警告头

        Returns:
            警告信息，未发生截断或配置禁用时返回 None
        """
        if not self.config.add_warning_header or not self._truncated:
            return None
        return self._truncate_info


# ==================== 全局配置管理 ====================

_history_config = HistoryConfig()


def get_history_config() -> HistoryConfig:
    """获取全局历史消息配置"""
    return _history_config


def set_history_config(config: HistoryConfig) -> None:
    """设置全局历史消息配置"""
    global _history_config
    _history_config = config


def update_history_config(data: dict) -> None:
    """更新全局历史消息配置"""
    global _history_config
    _history_config = HistoryConfig.from_dict(data)
