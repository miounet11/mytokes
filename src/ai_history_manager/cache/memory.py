"""内存缓存实现

提供基于 LRU 策略的摘要缓存，支持：
- 按消息变化量检测是否需要刷新
- 按字符变化量检测是否需要刷新
- 过期时间控制
"""

import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Optional


@dataclass
class SummaryCacheEntry:
    """摘要缓存条目

    Attributes:
        summary: 摘要文本
        old_history_count: 生成摘要时的旧历史消息数
        old_history_chars: 生成摘要时的旧历史字符数
        updated_at: 更新时间戳
    """

    summary: str
    old_history_count: int
    old_history_chars: int
    updated_at: float


class SummaryCache:
    """轻量摘要缓存（LRU 策略）

    基于会话 ID 和目标消息数缓存摘要结果，通过检测历史变化量
    来决定是否需要重新生成摘要。

    使用示例:
    ```python
    cache = SummaryCache(max_entries=128)

    # 尝试获取缓存
    cached = cache.get(
        key="session_123:20",
        old_history_count=10,
        old_history_chars=5000,
        min_delta_messages=3,
        min_delta_chars=4000,
        max_age_seconds=180
    )

    if cached is None:
        # 生成新摘要
        summary = await generate_summary(...)
        cache.set(
            key="session_123:20",
            summary=summary,
            old_history_count=10,
            old_history_chars=5000
        )
    ```
    """

    def __init__(self, max_entries: int = 128):
        """初始化缓存

        Args:
            max_entries: 最大缓存条目数
        """
        self._entries: OrderedDict[str, SummaryCacheEntry] = OrderedDict()
        self._max_entries = max_entries

    def get(
        self,
        key: str,
        old_history_count: int,
        old_history_chars: int,
        min_delta_messages: int,
        min_delta_chars: int,
        max_age_seconds: int,
    ) -> Optional[str]:
        """获取缓存的摘要

        检查条件（任一不满足则返回 None）：
        1. 缓存存在
        2. 未过期
        3. 旧历史消息数变化未超过阈值
        4. 旧历史字符数变化未超过阈值

        Args:
            key: 缓存键
            old_history_count: 当前旧历史消息数
            old_history_chars: 当前旧历史字符数
            min_delta_messages: 触发刷新的消息变化阈值
            min_delta_chars: 触发刷新的字符变化阈值
            max_age_seconds: 最大缓存时间（秒），0 表示不检查

        Returns:
            缓存的摘要文本，不存在或需要刷新时返回 None
        """
        entry = self._entries.get(key)
        if not entry:
            return None

        now = time.time()

        # 检查过期时间
        if max_age_seconds > 0 and now - entry.updated_at > max_age_seconds:
            self._entries.pop(key, None)
            return None

        # 检查消息数变化
        if old_history_count - entry.old_history_count >= min_delta_messages:
            return None

        # 检查字符数变化
        if old_history_chars - entry.old_history_chars >= min_delta_chars:
            return None

        # 命中缓存，移到末尾（LRU）
        self._entries.move_to_end(key)
        return entry.summary

    def set(
        self,
        key: str,
        summary: str,
        old_history_count: int,
        old_history_chars: int,
    ) -> None:
        """设置缓存

        Args:
            key: 缓存键
            summary: 摘要文本
            old_history_count: 生成摘要时的旧历史消息数
            old_history_chars: 生成摘要时的旧历史字符数
        """
        self._entries[key] = SummaryCacheEntry(
            summary=summary,
            old_history_count=old_history_count,
            old_history_chars=old_history_chars,
            updated_at=time.time(),
        )
        self._entries.move_to_end(key)

        # LRU 淘汰
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)

    def invalidate(self, key: str) -> bool:
        """使指定缓存失效

        Args:
            key: 缓存键

        Returns:
            是否存在并删除了缓存
        """
        return self._entries.pop(key, None) is not None

    def clear(self) -> int:
        """清空所有缓存

        Returns:
            清除的条目数
        """
        count = len(self._entries)
        self._entries.clear()
        return count

    def size(self) -> int:
        """获取当前缓存大小

        Returns:
            缓存条目数
        """
        return len(self._entries)

    def cleanup_expired(self, max_age_seconds: int) -> int:
        """清理过期缓存

        Args:
            max_age_seconds: 最大缓存时间（秒）

        Returns:
            清除的条目数
        """
        now = time.time()
        expired_keys = [
            key
            for key, entry in self._entries.items()
            if now - entry.updated_at > max_age_seconds
        ]
        for key in expired_keys:
            self._entries.pop(key, None)
        return len(expired_keys)
