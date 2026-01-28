"""缓存测试"""
import time
import pytest
from ai_history_manager.cache import SummaryCache


class TestSummaryCache:
    """摘要缓存测试"""

    def test_set_and_get(self):
        """测试基本设置和获取"""
        cache = SummaryCache(max_entries=10)

        cache.set(
            key="test_key",
            summary="test summary",
            old_history_count=5,
            old_history_chars=1000,
        )

        result = cache.get(
            key="test_key",
            old_history_count=5,
            old_history_chars=1000,
            min_delta_messages=3,
            min_delta_chars=4000,
            max_age_seconds=180,
        )

        assert result == "test summary"

    def test_get_nonexistent(self):
        """测试获取不存在的键"""
        cache = SummaryCache()

        result = cache.get(
            key="nonexistent",
            old_history_count=0,
            old_history_chars=0,
            min_delta_messages=3,
            min_delta_chars=4000,
            max_age_seconds=180,
        )

        assert result is None

    def test_expire_by_age(self):
        """测试过期清理"""
        cache = SummaryCache()

        cache.set(
            key="test_key",
            summary="test summary",
            old_history_count=5,
            old_history_chars=1000,
        )

        # 修改时间戳使其过期
        cache._entries["test_key"].updated_at = time.time() - 200

        result = cache.get(
            key="test_key",
            old_history_count=5,
            old_history_chars=1000,
            min_delta_messages=3,
            min_delta_chars=4000,
            max_age_seconds=180,
        )

        assert result is None

    def test_expire_by_delta_messages(self):
        """测试消息数变化导致的刷新"""
        cache = SummaryCache()

        cache.set(
            key="test_key",
            summary="test summary",
            old_history_count=5,
            old_history_chars=1000,
        )

        # 消息数增加超过阈值
        result = cache.get(
            key="test_key",
            old_history_count=10,  # 增加了 5 条
            old_history_chars=1000,
            min_delta_messages=3,  # 阈值是 3
            min_delta_chars=4000,
            max_age_seconds=180,
        )

        assert result is None

    def test_expire_by_delta_chars(self):
        """测试字符数变化导致的刷新"""
        cache = SummaryCache()

        cache.set(
            key="test_key",
            summary="test summary",
            old_history_count=5,
            old_history_chars=1000,
        )

        # 字符数增加超过阈值
        result = cache.get(
            key="test_key",
            old_history_count=5,
            old_history_chars=6000,  # 增加了 5000 字符
            min_delta_messages=3,
            min_delta_chars=4000,  # 阈值是 4000
            max_age_seconds=180,
        )

        assert result is None

    def test_lru_eviction(self):
        """测试 LRU 淘汰"""
        cache = SummaryCache(max_entries=2)

        cache.set("key1", "summary1", 1, 100)
        cache.set("key2", "summary2", 2, 200)
        cache.set("key3", "summary3", 3, 300)  # 应该淘汰 key1

        assert cache.size() == 2
        assert cache.get("key1", 1, 100, 0, 0, 0) is None
        assert cache.get("key2", 2, 200, 0, 0, 0) == "summary2"
        assert cache.get("key3", 3, 300, 0, 0, 0) == "summary3"

    def test_invalidate(self):
        """测试手动失效"""
        cache = SummaryCache()

        cache.set("test_key", "test summary", 5, 1000)
        assert cache.invalidate("test_key")
        assert not cache.invalidate("test_key")  # 第二次返回 False

    def test_clear(self):
        """测试清空缓存"""
        cache = SummaryCache()

        cache.set("key1", "summary1", 1, 100)
        cache.set("key2", "summary2", 2, 200)

        count = cache.clear()
        assert count == 2
        assert cache.size() == 0

    def test_cleanup_expired(self):
        """测试清理过期缓存"""
        cache = SummaryCache()

        cache.set("key1", "summary1", 1, 100)
        cache.set("key2", "summary2", 2, 200)

        # 修改 key1 的时间戳使其过期
        cache._entries["key1"].updated_at = time.time() - 200

        count = cache.cleanup_expired(max_age_seconds=180)
        assert count == 1
        assert cache.size() == 1
