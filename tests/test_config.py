"""配置测试"""
import pytest
import tempfile
import os
from pathlib import Path

from ai_history_manager import HistoryConfig, TruncateStrategy, load_config, load_config_from_file


class TestHistoryConfig:
    """HistoryConfig 测试"""

    def test_default_values(self):
        """测试默认值"""
        config = HistoryConfig()

        assert config.max_messages == 30
        assert config.max_chars == 150000
        assert config.summary_keep_recent == 10
        assert config.summary_threshold == 100000
        assert config.retry_max_messages == 20
        assert config.max_retries == 2
        assert config.estimate_threshold == 180000
        assert config.chars_per_token == 3.0
        assert config.summary_cache_enabled is True
        assert config.add_warning_header is True

    def test_to_dict(self):
        """测试转换为字典"""
        config = HistoryConfig(
            strategies=[TruncateStrategy.ERROR_RETRY, TruncateStrategy.SMART_SUMMARY],
            max_messages=20,
        )

        data = config.to_dict()
        assert "error_retry" in data["strategies"]
        assert "smart_summary" in data["strategies"]
        assert data["max_messages"] == 20

    def test_from_dict(self):
        """测试从字典创建"""
        data = {
            "strategies": ["error_retry", "auto_truncate"],
            "max_messages": 25,
            "max_chars": 120000,
        }

        config = HistoryConfig.from_dict(data)
        assert TruncateStrategy.ERROR_RETRY in config.strategies
        assert TruncateStrategy.AUTO_TRUNCATE in config.strategies
        assert config.max_messages == 25
        assert config.max_chars == 120000

    def test_from_dict_invalid_strategy(self):
        """测试无效策略被忽略"""
        data = {
            "strategies": ["error_retry", "invalid_strategy"],
        }

        config = HistoryConfig.from_dict(data)
        assert TruncateStrategy.ERROR_RETRY in config.strategies
        assert len(config.strategies) == 1

    def test_validate_success(self):
        """测试验证通过"""
        config = HistoryConfig()
        errors = config.validate()
        assert len(errors) == 0

    def test_validate_errors(self):
        """测试验证错误"""
        config = HistoryConfig(
            max_messages=0,
            max_chars=500,
            summary_keep_recent=0,
            chars_per_token=0,
        )

        errors = config.validate()
        assert len(errors) > 0
        assert any("max_messages" in e for e in errors)
        assert any("max_chars" in e for e in errors)


class TestLoadConfig:
    """load_config 测试"""

    def test_load_config_none(self):
        """测试加载 None"""
        config = load_config(None)
        assert isinstance(config, HistoryConfig)

    def test_load_config_flat(self):
        """测试加载扁平格式"""
        data = {
            "strategies": ["error_retry"],
            "max_messages": 20,
        }

        config = load_config(data)
        assert config.max_messages == 20

    def test_load_config_nested(self):
        """测试加载嵌套格式（YAML 风格）"""
        data = {
            "history_manager": {
                "strategies": ["error_retry", "smart_summary"],
                "limits": {
                    "max_messages": 25,
                    "max_chars": 120000,
                },
                "summary": {
                    "keep_recent": 8,
                    "threshold": 80000,
                },
                "retry": {
                    "max_messages": 15,
                    "max_retries": 3,
                },
                "cache": {
                    "enabled": True,
                    "max_age_seconds": 120,
                },
            }
        }

        config = load_config(data)
        assert config.max_messages == 25
        assert config.max_chars == 120000
        assert config.summary_keep_recent == 8
        assert config.summary_threshold == 80000
        assert config.retry_max_messages == 15
        assert config.max_retries == 3
        assert config.summary_cache_max_age_seconds == 120


class TestLoadConfigFromFile:
    """load_config_from_file 测试"""

    def test_load_from_yaml_file(self):
        """测试从 YAML 文件加载"""
        yaml_content = """
history_manager:
  strategies:
    - error_retry
    - smart_summary
  limits:
    max_messages: 30
    max_chars: 150000
  summary:
    keep_recent: 10
  cache:
    enabled: true
"""

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as f:
            f.write(yaml_content)
            f.flush()

            try:
                config = load_config_from_file(f.name)
                assert config.max_messages == 30
                assert config.summary_keep_recent == 10
                assert config.summary_cache_enabled is True
            finally:
                os.unlink(f.name)

    def test_load_from_nonexistent_file(self):
        """测试加载不存在的文件"""
        with pytest.raises(FileNotFoundError):
            load_config_from_file("/nonexistent/path/config.yaml")


class TestTruncateStrategy:
    """TruncateStrategy 测试"""

    def test_strategy_values(self):
        """测试策略值"""
        assert TruncateStrategy.NONE.value == "none"
        assert TruncateStrategy.AUTO_TRUNCATE.value == "auto_truncate"
        assert TruncateStrategy.SMART_SUMMARY.value == "smart_summary"
        assert TruncateStrategy.ERROR_RETRY.value == "error_retry"
        assert TruncateStrategy.PRE_ESTIMATE.value == "pre_estimate"

    def test_strategy_from_string(self):
        """测试从字符串创建"""
        strategy = TruncateStrategy("error_retry")
        assert strategy == TruncateStrategy.ERROR_RETRY
