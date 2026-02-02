"""模型路由服务

根据请求内容智能选择最合适的模型。
支持基于关键词、对话阶段、消息长度等多种路由策略。
"""

import re
import random
import hashlib
from typing import Optional
from dataclasses import dataclass, field
from datetime import datetime
from collections import defaultdict

from ..config import get_settings, ModelRoutingConfig
from ..utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class RoutingDecision:
    """路由决策结果"""
    original_model: str
    routed_model: str
    reason: str
    priority: int = 0
    confidence: float = 1.0


@dataclass
class RoutingStats:
    """路由统计信息"""
    total_requests: int = 0
    opus_requests: int = 0
    sonnet_requests: int = 0
    haiku_requests: int = 0
    downgrade_count: int = 0
    upgrade_count: int = 0
    routing_reasons: dict = field(default_factory=lambda: defaultdict(int))

    def record(self, decision: RoutingDecision):
        """记录路由决策"""
        self.total_requests += 1

        if "opus" in decision.routed_model.lower():
            self.opus_requests += 1
        elif "sonnet" in decision.routed_model.lower():
            self.sonnet_requests += 1
        elif "haiku" in decision.routed_model.lower():
            self.haiku_requests += 1

        if decision.original_model != decision.routed_model:
            if self._is_upgrade(decision.original_model, decision.routed_model):
                self.upgrade_count += 1
            else:
                self.downgrade_count += 1

        self.routing_reasons[decision.reason] += 1

    def _is_upgrade(self, from_model: str, to_model: str) -> bool:
        """判断是否为升级"""
        model_rank = {"haiku": 1, "sonnet": 2, "opus": 3}

        from_rank = 0
        to_rank = 0

        for name, rank in model_rank.items():
            if name in from_model.lower():
                from_rank = rank
            if name in to_model.lower():
                to_rank = rank

        return to_rank > from_rank

    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            "total_requests": self.total_requests,
            "opus_requests": self.opus_requests,
            "sonnet_requests": self.sonnet_requests,
            "haiku_requests": self.haiku_requests,
            "downgrade_count": self.downgrade_count,
            "upgrade_count": self.upgrade_count,
            "routing_reasons": dict(self.routing_reasons),
            "opus_ratio": self.opus_requests / max(self.total_requests, 1),
            "sonnet_ratio": self.sonnet_requests / max(self.total_requests, 1),
        }


class ModelRouter:
    """模型路由器

    根据多种策略选择最合适的模型：
    1. 显式模型指定（最高优先级）
    2. 关键词匹配
    3. 对话阶段检测
    4. 消息长度/复杂度
    5. 概率路由（默认）
    """

    def __init__(self, config: Optional[ModelRoutingConfig] = None):
        self.config = config or get_settings().model_routing
        self.stats = RoutingStats()

        # 编译关键词正则
        self._opus_pattern = self._compile_keywords(self.config.opus_keywords)

        # 复杂任务关键词
        self._complex_task_keywords = [
            r"\barchitect\b", r"\bdesign\s+pattern\b", r"\brefactor\b",
            r"\boptimize\b", r"\bsecurity\b", r"\bperformance\b",
            r"\bscalability\b", r"\bdistributed\b", r"\bmicroservice\b",
            r"\b架构\b", r"\b重构\b", r"\b优化\b", r"\b设计模式\b",
            r"\b安全\b", r"\b性能\b", r"\b分布式\b",
        ]
        self._complex_pattern = re.compile(
            "|".join(self._complex_task_keywords),
            re.IGNORECASE
        )

    def _compile_keywords(self, keywords: list[str]) -> re.Pattern:
        """编译关键词为正则表达式"""
        patterns = [re.escape(kw) for kw in keywords]
        return re.compile("|".join(patterns), re.IGNORECASE)

    def route(
        self,
        request_body: dict,
        request_id: Optional[str] = None
    ) -> RoutingDecision:
        """执行路由决策

        Args:
            request_body: 请求体
            request_id: 请求 ID（用于日志）

        Returns:
            RoutingDecision
        """
        if not self.config.enabled:
            original = request_body.get("model", self.config.sonnet_model)
            return RoutingDecision(
                original_model=original,
                routed_model=original,
                reason="routing_disabled",
            )

        original_model = request_body.get("model", "")
        messages = request_body.get("messages", [])

        # 按优先级检查各种路由策略
        decision = (
            self._check_explicit_model(original_model) or
            self._check_keyword_routing(messages) or
            self._check_conversation_phase(messages) or
            self._check_complexity(messages) or
            self._default_routing(original_model, request_id)
        )

        # 记录统计
        self.stats.record(decision)

        logger.debug(
            f"Routing decision: {decision.original_model} -> {decision.routed_model} "
            f"(reason: {decision.reason}, priority: {decision.priority})"
        )

        return decision

    def _check_explicit_model(self, model: str) -> Optional[RoutingDecision]:
        """检查是否显式指定了模型

        优先级: 1 (最高)
        """
        if not model:
            return None

        model_lower = model.lower()

        # 检查是否明确指定了特定模型
        if "opus" in model_lower:
            return RoutingDecision(
                original_model=model,
                routed_model=self.config.opus_model,
                reason="explicit_opus",
                priority=1,
            )
        elif "haiku" in model_lower:
            return RoutingDecision(
                original_model=model,
                routed_model=self.config.haiku_model,
                reason="explicit_haiku",
                priority=1,
            )
        elif "sonnet" in model_lower:
            return RoutingDecision(
                original_model=model,
                routed_model=self.config.sonnet_model,
                reason="explicit_sonnet",
                priority=1,
            )

        return None

    def _check_keyword_routing(self, messages: list[dict]) -> Optional[RoutingDecision]:
        """基于关键词的路由

        优先级: 2
        """
        # 提取最近的用户消息
        user_content = self._extract_recent_user_content(messages, limit=3)

        if not user_content:
            return None

        # 检查 Opus 关键词
        if self._opus_pattern.search(user_content):
            return RoutingDecision(
                original_model="",
                routed_model=self.config.opus_model,
                reason="keyword_opus",
                priority=2,
            )

        return None

    def _check_conversation_phase(self, messages: list[dict]) -> Optional[RoutingDecision]:
        """基于对话阶段的路由

        优先级: 3

        - 对话开始阶段（前3轮）：使用 Opus 建立上下文
        - 中间阶段：根据复杂度选择
        - 后期阶段：可以降级到 Sonnet
        """
        message_count = len(messages)

        # 对话开始阶段，使用更强的模型
        if message_count <= 2:
            return RoutingDecision(
                original_model="",
                routed_model=self.config.opus_model,
                reason="conversation_start",
                priority=3,
            )

        # 长对话可以考虑降级
        if message_count > self.config.downgrade_message_threshold:
            total_chars = sum(
                len(self._get_message_text(m))
                for m in messages
            )

            if total_chars > self.config.downgrade_char_threshold:
                return RoutingDecision(
                    original_model="",
                    routed_model=self.config.sonnet_model,
                    reason="long_conversation_downgrade",
                    priority=3,
                )

        return None

    def _check_complexity(self, messages: list[dict]) -> Optional[RoutingDecision]:
        """基于任务复杂度的路由

        优先级: 4
        """
        user_content = self._extract_recent_user_content(messages, limit=2)

        if not user_content:
            return None

        # 检查复杂任务关键词
        if self._complex_pattern.search(user_content):
            return RoutingDecision(
                original_model="",
                routed_model=self.config.opus_model,
                reason="complex_task",
                priority=4,
            )

        # 检查代码相关任务
        code_indicators = [
            r"```",  # 代码块
            r"\bfunction\b", r"\bclass\b", r"\bdef\b",
            r"\bimport\b", r"\brequire\b",
        ]
        code_pattern = re.compile("|".join(code_indicators))

        if code_pattern.search(user_content):
            # 代码任务使用 Sonnet（性价比更高）
            return RoutingDecision(
                original_model="",
                routed_model=self.config.sonnet_model,
                reason="code_task",
                priority=4,
            )

        return None

    def _default_routing(
        self,
        original_model: str,
        request_id: Optional[str] = None
    ) -> RoutingDecision:
        """默认路由策略

        优先级: 5 (最低)

        使用概率路由，基于请求 ID 的哈希值确保同一会话使用相同模型。
        """
        # 使用请求 ID 生成确定性随机数
        if request_id:
            hash_value = int(hashlib.md5(request_id.encode()).hexdigest(), 16)
            random_value = (hash_value % 100) / 100
        else:
            random_value = random.random()

        # 70% Sonnet, 30% Opus
        if random_value < 0.7:
            return RoutingDecision(
                original_model=original_model or self.config.sonnet_model,
                routed_model=self.config.sonnet_model,
                reason="default_sonnet",
                priority=5,
            )
        else:
            return RoutingDecision(
                original_model=original_model or self.config.opus_model,
                routed_model=self.config.opus_model,
                reason="default_opus",
                priority=5,
            )

    def _extract_recent_user_content(self, messages: list[dict], limit: int = 3) -> str:
        """提取最近的用户消息内容"""
        user_messages = [
            m for m in messages
            if m.get("role") == "user"
        ][-limit:]

        contents = []
        for msg in user_messages:
            contents.append(self._get_message_text(msg))

        return " ".join(contents)

    def _get_message_text(self, message: dict) -> str:
        """获取消息的文本内容"""
        content = message.get("content", "")

        if isinstance(content, str):
            return content

        if isinstance(content, list):
            texts = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        texts.append(block.get("text", ""))
                elif isinstance(block, str):
                    texts.append(block)
            return " ".join(texts)

        return ""

    def get_stats(self) -> dict:
        """获取路由统计"""
        return self.stats.to_dict()

    def reset_stats(self):
        """重置统计"""
        self.stats = RoutingStats()


# 全局路由器实例
_router: Optional[ModelRouter] = None


def get_router() -> ModelRouter:
    """获取全局路由器实例"""
    global _router
    if _router is None:
        _router = ModelRouter()
    return _router


def route_model(request_body: dict, request_id: Optional[str] = None) -> RoutingDecision:
    """路由模型（便捷函数）"""
    return get_router().route(request_body, request_id)
