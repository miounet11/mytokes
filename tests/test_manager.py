"""HistoryManager 测试"""
import json
import pytest
from ai_history_manager import HistoryManager, HistoryConfig, TruncateStrategy


class TestHistoryManagerBasic:
    """基础功能测试"""

    def test_default_config(self):
        """测试默认配置"""
        manager = HistoryManager()
        assert manager.config.max_messages == 30
        assert manager.config.max_chars == 150000
        assert TruncateStrategy.ERROR_RETRY in manager.config.strategies

    def test_custom_config(self):
        """测试自定义配置"""
        config = HistoryConfig(
            strategies=[TruncateStrategy.AUTO_TRUNCATE],
            max_messages=20,
            max_chars=100000,
        )
        manager = HistoryManager(config)
        assert manager.config.max_messages == 20
        assert manager.config.max_chars == 100000

    def test_reset(self):
        """测试重置状态"""
        manager = HistoryManager()
        manager._truncated = True
        manager._truncate_info = "test"
        manager.reset()
        assert not manager.was_truncated
        assert manager.truncate_info == ""


class TestTruncation:
    """截断功能测试"""

    def test_truncate_by_count(self):
        """测试按数量截断"""
        config = HistoryConfig(
            strategies=[TruncateStrategy.AUTO_TRUNCATE],
            max_messages=3,
        )
        manager = HistoryManager(config)

        history = [
            {"role": "user", "content": f"message {i}"}
            for i in range(5)
        ]

        result = manager.truncate_by_count(history, 3)
        assert len(result) == 3
        assert result[0]["content"] == "message 2"
        assert manager.was_truncated

    def test_truncate_by_count_no_change(self):
        """测试无需截断的情况"""
        manager = HistoryManager()
        history = [{"role": "user", "content": "test"}]

        result = manager.truncate_by_count(history, 10)
        assert len(result) == 1
        assert not manager.was_truncated

    def test_truncate_by_chars(self):
        """测试按字符数截断"""
        manager = HistoryManager()

        # 创建一些消息
        history = [
            {"role": "user", "content": "x" * 100}
            for _ in range(10)
        ]

        # 计算总字符数
        total = len(json.dumps(history, ensure_ascii=False))

        # 截断到一半
        result = manager.truncate_by_chars(history, total // 2)
        assert len(result) < len(history)
        assert manager.was_truncated


class TestPreProcess:
    """预处理功能测试"""

    def test_pre_process_auto_truncate(self):
        """测试自动截断预处理"""
        config = HistoryConfig(
            strategies=[TruncateStrategy.AUTO_TRUNCATE],
            max_messages=3,
        )
        manager = HistoryManager(config)

        history = [
            {"role": "user", "content": f"message {i}"}
            for i in range(5)
        ]

        result = manager.pre_process(history, "current message")
        assert len(result) == 3
        assert manager.was_truncated

    def test_pre_process_empty_history(self):
        """测试空历史"""
        manager = HistoryManager()
        result = manager.pre_process([], "test")
        assert result == []
        assert not manager.was_truncated

    def test_pre_process_no_truncate(self):
        """测试无需处理的情况"""
        config = HistoryConfig(
            strategies=[TruncateStrategy.NONE],
        )
        manager = HistoryManager(config)

        history = [
            {"role": "user", "content": "test"},
            {"role": "assistant", "content": "response"},
        ]

        result = manager.pre_process(history, "")
        assert len(result) == 2
        assert not manager.was_truncated


class TestEstimation:
    """估算功能测试"""

    def test_estimate_tokens(self):
        """测试 token 估算"""
        config = HistoryConfig(chars_per_token=4.0)
        manager = HistoryManager(config)

        tokens = manager.estimate_tokens("1234567890")  # 10 字符
        assert tokens == 2  # 10 / 4 = 2.5 -> 2

    def test_estimate_history_size(self):
        """测试历史大小估算"""
        manager = HistoryManager()
        history = [
            {"role": "user", "content": "test"},
            {"role": "assistant", "content": "response"},
        ]

        count, chars = manager.estimate_history_size(history)
        assert count == 2
        assert chars > 0

    def test_estimate_request_chars(self):
        """测试请求字符数估算"""
        manager = HistoryManager()
        history = [{"role": "user", "content": "test"}]
        user_content = "current message"

        history_chars, user_chars, total = manager.estimate_request_chars(history, user_content)
        assert history_chars > 0
        assert user_chars == len(user_content)
        assert total == history_chars + user_chars


class TestShouldMethods:
    """判断方法测试"""

    def test_should_pre_truncate(self):
        """测试预截断判断"""
        config = HistoryConfig(
            strategies=[TruncateStrategy.PRE_ESTIMATE],
            estimate_threshold=100,
        )
        manager = HistoryManager(config)

        history = [{"role": "user", "content": "x" * 200}]
        assert manager.should_pre_truncate(history, "test")

    def test_should_not_pre_truncate(self):
        """测试不需要预截断"""
        config = HistoryConfig(
            strategies=[TruncateStrategy.PRE_ESTIMATE],
            estimate_threshold=10000,
        )
        manager = HistoryManager(config)

        history = [{"role": "user", "content": "test"}]
        assert not manager.should_pre_truncate(history, "test")

    def test_should_summarize(self):
        """测试摘要判断"""
        config = HistoryConfig(
            strategies=[TruncateStrategy.SMART_SUMMARY],
            summary_threshold=100,
            summary_keep_recent=2,
        )
        manager = HistoryManager(config)

        history = [
            {"role": "user", "content": "x" * 200}
            for _ in range(5)
        ]
        assert manager.should_summarize(history)


class TestHandleLengthError:
    """长度错误处理测试"""

    def test_handle_length_error(self):
        """测试长度错误处理"""
        config = HistoryConfig(
            strategies=[TruncateStrategy.ERROR_RETRY],
            retry_max_messages=5,
            max_retries=2,
        )
        manager = HistoryManager(config)

        history = [
            {"role": "user", "content": f"message {i}"}
            for i in range(10)
        ]

        truncated, should_retry = manager.handle_length_error(history, retry_count=0)
        assert len(truncated) == 5
        assert should_retry

    def test_handle_length_error_max_retries(self):
        """测试达到最大重试次数"""
        config = HistoryConfig(
            strategies=[TruncateStrategy.ERROR_RETRY],
            max_retries=2,
        )
        manager = HistoryManager(config)

        history = [{"role": "user", "content": "test"}] * 10

        truncated, should_retry = manager.handle_length_error(history, retry_count=2)
        assert not should_retry

    def test_handle_length_error_disabled(self):
        """测试禁用错误重试"""
        config = HistoryConfig(
            strategies=[TruncateStrategy.AUTO_TRUNCATE],  # 不包含 ERROR_RETRY
        )
        manager = HistoryManager(config)

        history = [{"role": "user", "content": "test"}] * 10

        truncated, should_retry = manager.handle_length_error(history, retry_count=0)
        assert not should_retry


class TestWarningHeader:
    """警告头测试"""

    def test_get_warning_header(self):
        """测试获取警告头"""
        config = HistoryConfig(add_warning_header=True)
        manager = HistoryManager(config)

        # 未截断
        assert manager.get_warning_header() is None

        # 截断后
        manager._truncated = True
        manager._truncate_info = "test warning"
        assert manager.get_warning_header() == "test warning"

    def test_get_warning_header_disabled(self):
        """测试禁用警告头"""
        config = HistoryConfig(add_warning_header=False)
        manager = HistoryManager(config)

        manager._truncated = True
        manager._truncate_info = "test warning"
        assert manager.get_warning_header() is None
