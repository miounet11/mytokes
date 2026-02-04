import asyncio
import hashlib
import re
import random
from typing import Tuple, Optional
from app.core.config import MODEL_ROUTING_CONFIG, logger

# 用于文件路径匹配
_RE_FILE_PATH = re.compile(r'[/\\][\w\-\.]+\.(py|js|ts|jsx|tsx|go|rs|java|cpp|c|h|md|yaml|yml|json|toml)')

class ModelRouter:
    """智能模型路由器 - "Opus 大脑, Sonnet 双手" 策略"""

    def __init__(self, config: dict = None):
        self.config = config or MODEL_ROUTING_CONFIG
        self.stats = {
            "opus": 0,
            "sonnet": 0,
            "haiku": 0,
            "opus_degraded": 0,
            "opus_plan_mode": 0,      # Plan Mode 使用 Opus 的次数
            "opus_first_turn": 0,     # 首轮使用 Opus 的次数
            "opus_keywords": 0,       # 关键词触发 Opus 的次数
            "sonnet_enhanced": 0,     # Sonnet 上下文增强的次数
        }
        self._lock = asyncio.Lock()
        # 预处理关键词为小写，避免每次匹配时重复转换
        self._opus_keywords_lower = [kw.lower() for kw in self.config.get("opus_keywords", [])]
        self._sonnet_keywords_lower = [kw.lower() for kw in self.config.get("sonnet_keywords", [])]

        # Opus 并发控制
        self._opus_semaphore = asyncio.Semaphore(self.config.get("opus_max_concurrent", 15))
        self._opus_current = 0

        # Plan Mode 检测标记
        self._plan_mode_markers = [
            "enterplanmode", "exitplanmode", "plan mode",
            "进入规划", "规划模式", "制定计划",
            "in plan mode", "planning mode",
        ]

    def _count_chars(self, messages: list, system: str = "") -> int:
        """统计总字符数"""
        total = len(str(system)) if system else 0
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                total += len(content)
            elif isinstance(content, list):
                for item in content:
                    if isinstance(item, dict):
                        total += len(str(item.get("text", "")))
                        total += len(str(item.get("content", "")))
                    elif isinstance(item, str):
                        total += len(item)
        return total

    def _count_tool_calls(self, messages: list) -> int:
        """统计历史中的工具调用次数"""
        count = 0
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict):
                        if item.get("type") in ("tool_use", "tool_result"):
                            count += 1
        return count

    def _count_files_mentioned(self, messages: list) -> int:
        """统计提及的文件数量（简单估算）"""
        files = set()

        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                matches = _RE_FILE_PATH.findall(content)
                files.update(matches)
            elif isinstance(content, list):
                for item in content:
                    if isinstance(item, dict):
                        text = item.get("text", "") or item.get("content", "")
                        if isinstance(text, str):
                            matches = _RE_FILE_PATH.findall(text)
                            files.update(matches)
        return len(files)

    def _get_last_user_message(self, messages: list) -> str:
        """获取最后一条用户消息"""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, str):
                    return content
                elif isinstance(content, list):
                    texts = []
                    for item in content:
                        if isinstance(item, dict) and item.get("type") == "text":
                            texts.append(item.get("text", ""))
                    return " ".join(texts)
        return ""

    def _contains_keywords_optimized(self, text: str, keywords_lower: list) -> tuple[bool, str]:
        """优化版关键词检查，使用预处理的小写关键词列表"""
        text_lower = text.lower()
        for kw in keywords_lower:
            if kw in text_lower:
                return True, kw
        return False, ""

    def _count_user_messages(self, messages: list) -> int:
        """统计用户消息数量"""
        return sum(1 for msg in messages if msg.get("role") == "user")

    def _is_plan_mode(self, messages: list) -> bool:
        """检测是否处于 Plan Mode"""
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                content_lower = content.lower()
                for marker in self._plan_mode_markers:
                    if marker in content_lower:
                        return True
            elif isinstance(content, list):
                for item in content:
                    if isinstance(item, dict):
                        text = item.get("text", "") or item.get("content", "")
                        if isinstance(text, str):
                            text_lower = text.lower()
                            for marker in self._plan_mode_markers:
                                if marker in text_lower:
                                    return True
        return False

    def should_use_opus(self, request_body: dict) -> tuple[bool, str]:
        """概率路由决策 - 目标: Opus 20%, Sonnet 80%"""
        if not self.config.get("enabled", True):
            return True, "路由已禁用"

        messages = request_body.get("messages", [])
        last_user_msg = self._get_last_user_message(messages)
        user_msg_count = self._count_user_messages(messages)
        tool_calls = self._count_tool_calls(messages)

        # 优先级 1: Plan Mode 强制 Opus
        if self.config.get("force_opus_on_plan_mode", True) and self._is_plan_mode(messages):
            return True, "PlanMode"

        # 优先级 2: Extended Thinking 强制 Opus
        if self.config.get("force_opus_on_thinking", True):
            if request_body.get("thinking") or request_body.get("extended_thinking"):
                return True, "ExtendedThinking"

        # 优先级 3: Opus 关键词强制 Opus
        found, matched_kw = self._contains_keywords_optimized(last_user_msg, self._opus_keywords_lower)
        if found:
            return True, f"Opus关键词[{matched_kw}]"

        # 优先级 4: Sonnet 关键词强制 Sonnet
        found, matched_kw = self._contains_keywords_optimized(last_user_msg, self._sonnet_keywords_lower)
        if found:
            return False, f"Sonnet关键词[{matched_kw}]"

        # 优先级 5: 执行阶段 - 高概率 Sonnet
        exec_threshold = self.config.get("execution_tool_threshold", 3)
        if tool_calls >= exec_threshold:
            exec_sonnet_prob = self.config.get("execution_sonnet_probability", 90)
            if random.randint(1, 100) <= exec_sonnet_prob:
                return False, f"执行阶段({tool_calls}次工具,{exec_sonnet_prob}%Sonnet)"
            return True, f"执行阶段({tool_calls}次工具,Opus抽中)"

        # 优先级 6: 首轮对话 - 较高概率 Opus
        first_turn_max = self.config.get("first_turn_max_messages", 2)
        if user_msg_count <= first_turn_max:
            first_turn_opus_prob = self.config.get("first_turn_opus_probability", 50)
            if random.randint(1, 100) <= first_turn_opus_prob:
                return True, f"首轮({user_msg_count}条,{first_turn_opus_prob}%Opus)"
            return False, f"首轮({user_msg_count}条,Sonnet抽中)"

        # 优先级 7: 默认概率 - 20% Opus, 80% Sonnet
        base_opus_prob = self.config.get("base_opus_probability", 20)
        if random.randint(1, 100) <= base_opus_prob:
            return True, f"默认概率({base_opus_prob}%Opus)"
        return False, f"默认概率({100-base_opus_prob}%Sonnet)"

    async def route(self, request_body: dict) -> tuple[str, str]:
        """路由到合适的模型"""
        original_model = request_body.get("model", "")

        if "opus" not in original_model.lower():
            async with self._lock:
                if "haiku" in original_model.lower():
                    self.stats["haiku"] += 1
                else:
                    self.stats["sonnet"] += 1
            return original_model, "非Opus请求"

        should_opus, reason = self.should_use_opus(request_body)

        if should_opus:
            max_concurrent = self.config.get("opus_max_concurrent", 15)
            async with self._lock:
                current_opus = self.stats["opus"] - self.stats.get("opus_completed", 0)

            if current_opus >= max_concurrent:
                async with self._lock:
                    self.stats["sonnet"] += 1
                    self.stats["opus_degraded"] += 1
                return self.config.get("sonnet_model"), f"Opus已满({current_opus}/{max_concurrent}),降级Sonnet"

            async with self._lock:
                self.stats["opus"] += 1
            return self.config.get("opus_model"), reason

        async with self._lock:
            self.stats["sonnet"] += 1
        return self.config.get("sonnet_model"), reason

    def route_sync(self, request_body: dict) -> tuple[str, str]:
        """路由到合适的模型（同步版本）- 使用相同的概率逻辑"""
        original_model = request_body.get("model", "")

        if "opus" not in original_model.lower():
            if "haiku" in original_model.lower():
                self.stats["haiku"] += 1
            else:
                self.stats["sonnet"] += 1
            return original_model, "非Opus请求"

        should_opus, reason = self.should_use_opus(request_body)

        if should_opus:
            self.stats["opus"] += 1
            return self.config.get("opus_model", "claude-opus-4-5-20251101"), reason
        else:
            self.stats["sonnet"] += 1
            return self.config.get("sonnet_model", "claude-sonnet-4-5-20250929"), reason

    def get_stats(self) -> dict:
        """获取路由统计"""
        total = self.stats["opus"] + self.stats["sonnet"] + self.stats["haiku"]
        if total == 0:
            opus_pct = sonnet_pct = haiku_pct = 0
        else:
            opus_pct = round(self.stats["opus"] / total * 100, 1)
            sonnet_pct = round(self.stats["sonnet"] / total * 100, 1)
            haiku_pct = round(self.stats["haiku"] / total * 100, 1)

        return {
            "opus_requests": self.stats["opus"],
            "sonnet_requests": self.stats["sonnet"],
            "haiku_requests": self.stats["haiku"],
            "opus_degraded": self.stats.get("opus_degraded", 0),
            "total_requests": total,
            "opus_percent": f"{opus_pct}%",
            "sonnet_percent": f"{sonnet_pct}%",
            "haiku_percent": f"{haiku_pct}%",
            "opus_max_concurrent": self.config.get("opus_max_concurrent", 10),
        }

# 全局模型路由器实例
model_router = ModelRouter(MODEL_ROUTING_CONFIG)
