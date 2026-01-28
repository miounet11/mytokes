"""历史消息配置管理

提供 HistoryConfig 配置类和 YAML 配置加载功能。
"""

import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import yaml


class TruncateStrategy(str, Enum):
    """截断策略枚举"""

    NONE = "none"  # 不截断
    AUTO_TRUNCATE = "auto_truncate"  # 自动截断（保留最近 N 条）
    SMART_SUMMARY = "smart_summary"  # 智能摘要
    ERROR_RETRY = "error_retry"  # 错误时截断重试
    PRE_ESTIMATE = "pre_estimate"  # 预估检测


@dataclass
class HistoryConfig:
    """历史消息配置

    Attributes:
        strategies: 启用的策略列表
        max_messages: 最大消息数
        max_chars: 最大字符数
        summary_keep_recent: 摘要时保留最近 N 条完整消息
        summary_threshold: 触发摘要的字符数阈值
        summary_max_length: 摘要最大长度
        retry_max_messages: 重试时保留的消息数
        max_retries: 最大重试次数
        estimate_threshold: 预估阈值（字符数）
        chars_per_token: 每 token 约等于多少字符
        summary_cache_enabled: 是否启用摘要缓存
        summary_cache_min_delta_messages: 旧历史新增 N 条后刷新摘要
        summary_cache_min_delta_chars: 旧历史新增字符数阈值
        summary_cache_max_age_seconds: 摘要最大复用时间
        summary_cache_max_entries: 最大缓存条目数
        add_warning_header: 截断时是否添加警告信息
        logging_enabled: 是否启用日志
        logging_level: 日志级别
    """

    # 启用的策略（可多选）
    strategies: list[TruncateStrategy] = field(
        default_factory=lambda: [TruncateStrategy.ERROR_RETRY]
    )

    # 自动截断配置
    max_messages: int = 30  # 最大消息数
    max_chars: int = 150000  # 最大字符数（约 50k tokens）

    # 智能摘要配置
    summary_keep_recent: int = 10  # 摘要时保留最近 N 条完整消息
    summary_threshold: int = 100000  # 触发摘要的字符数阈值
    summary_max_length: int = 2000  # 摘要最大长度

    # 错误重试配置
    retry_max_messages: int = 20  # 重试时保留的消息数
    max_retries: int = 2  # 最大重试次数

    # 预估配置
    estimate_threshold: int = 180000  # 预估阈值（字符数）
    chars_per_token: float = 3.0  # 每 token 约等于多少字符

    # 摘要缓存配置
    summary_cache_enabled: bool = True
    summary_cache_min_delta_messages: int = 3  # 旧历史新增 N 条后刷新摘要
    summary_cache_min_delta_chars: int = 4000  # 旧历史新增字符数阈值
    summary_cache_max_age_seconds: int = 180  # 摘要最大复用时间
    summary_cache_max_entries: int = 128  # 最大缓存条目数

    # 是否添加截断警告
    add_warning_header: bool = True

    # 日志配置
    logging_enabled: bool = True
    logging_level: str = "INFO"

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            "strategies": [s.value for s in self.strategies],
            "max_messages": self.max_messages,
            "max_chars": self.max_chars,
            "summary_keep_recent": self.summary_keep_recent,
            "summary_threshold": self.summary_threshold,
            "summary_max_length": self.summary_max_length,
            "retry_max_messages": self.retry_max_messages,
            "max_retries": self.max_retries,
            "estimate_threshold": self.estimate_threshold,
            "chars_per_token": self.chars_per_token,
            "summary_cache_enabled": self.summary_cache_enabled,
            "summary_cache_min_delta_messages": self.summary_cache_min_delta_messages,
            "summary_cache_min_delta_chars": self.summary_cache_min_delta_chars,
            "summary_cache_max_age_seconds": self.summary_cache_max_age_seconds,
            "summary_cache_max_entries": self.summary_cache_max_entries,
            "add_warning_header": self.add_warning_header,
            "logging_enabled": self.logging_enabled,
            "logging_level": self.logging_level,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "HistoryConfig":
        """从字典创建配置"""
        strategies_raw = data.get("strategies", ["error_retry"])
        strategies = []
        for s in strategies_raw:
            if isinstance(s, TruncateStrategy):
                strategies.append(s)
            elif isinstance(s, str):
                try:
                    strategies.append(TruncateStrategy(s))
                except ValueError:
                    pass  # 忽略无效的策略

        return cls(
            strategies=strategies or [TruncateStrategy.ERROR_RETRY],
            max_messages=data.get("max_messages", 30),
            max_chars=data.get("max_chars", 150000),
            summary_keep_recent=data.get("summary_keep_recent", 10),
            summary_threshold=data.get("summary_threshold", 100000),
            summary_max_length=data.get("summary_max_length", 2000),
            retry_max_messages=data.get("retry_max_messages", 20),
            max_retries=data.get("max_retries", 2),
            estimate_threshold=data.get("estimate_threshold", 180000),
            chars_per_token=data.get("chars_per_token", 3.0),
            summary_cache_enabled=data.get("summary_cache_enabled", True),
            summary_cache_min_delta_messages=data.get("summary_cache_min_delta_messages", 3),
            summary_cache_min_delta_chars=data.get("summary_cache_min_delta_chars", 4000),
            summary_cache_max_age_seconds=data.get("summary_cache_max_age_seconds", 180),
            summary_cache_max_entries=data.get("summary_cache_max_entries", 128),
            add_warning_header=data.get("add_warning_header", True),
            logging_enabled=data.get("logging_enabled", True),
            logging_level=data.get("logging_level", "INFO"),
        )

    def validate(self) -> list[str]:
        """验证配置，返回错误列表"""
        errors = []

        if self.max_messages < 1:
            errors.append("max_messages must be at least 1")
        if self.max_chars < 1000:
            errors.append("max_chars must be at least 1000")
        if self.summary_keep_recent < 1:
            errors.append("summary_keep_recent must be at least 1")
        if self.summary_threshold < 1000:
            errors.append("summary_threshold must be at least 1000")
        if self.retry_max_messages < 1:
            errors.append("retry_max_messages must be at least 1")
        if self.max_retries < 0:
            errors.append("max_retries must be non-negative")
        if self.estimate_threshold < 1000:
            errors.append("estimate_threshold must be at least 1000")
        if self.chars_per_token <= 0:
            errors.append("chars_per_token must be positive")

        return errors


def load_config_from_file(file_path: str | Path) -> HistoryConfig:
    """从 YAML 文件加载配置

    Args:
        file_path: 配置文件路径

    Returns:
        HistoryConfig 配置对象

    Raises:
        FileNotFoundError: 配置文件不存在
        yaml.YAMLError: YAML 解析错误
    """
    file_path = Path(file_path)

    if not file_path.exists():
        raise FileNotFoundError(f"Config file not found: {file_path}")

    with open(file_path, encoding="utf-8") as f:
        raw_data = yaml.safe_load(f)

    return load_config(raw_data)


def load_config(data: dict[str, Any] | None = None) -> HistoryConfig:
    """从字典加载配置

    支持两种格式：
    1. 嵌套格式（从 YAML 文件加载）：
       ```yaml
       history_manager:
         strategies: [...]
         limits:
           max_messages: 30
       ```
    2. 扁平格式（直接传入参数）：
       ```python
       {"strategies": [...], "max_messages": 30}
       ```

    Args:
        data: 配置字典，为 None 时返回默认配置

    Returns:
        HistoryConfig 配置对象
    """
    if data is None:
        return HistoryConfig()

    # 支持嵌套格式（YAML 风格）
    if "history_manager" in data:
        hm = data["history_manager"]

        # 解析策略
        strategies = hm.get("strategies", ["error_retry"])

        # 解析基础限制
        limits = hm.get("limits", {})
        max_messages = limits.get("max_messages", 30)
        max_chars = limits.get("max_chars", 150000)

        # 解析摘要配置
        summary = hm.get("summary", {})
        summary_keep_recent = summary.get("keep_recent", 10)
        summary_threshold = summary.get("threshold", 100000)
        summary_max_length = summary.get("max_length", 2000)

        # 解析重试配置
        retry = hm.get("retry", {})
        retry_max_messages = retry.get("max_messages", 20)
        max_retries = retry.get("max_retries", 2)

        # 解析预估配置
        estimate = hm.get("estimate", {})
        estimate_threshold = estimate.get("threshold", 180000)
        chars_per_token = estimate.get("chars_per_token", 3.0)

        # 解析缓存配置
        cache = hm.get("cache", {})
        cache_enabled = cache.get("enabled", True)
        cache_min_delta_messages = cache.get("min_delta_messages", 3)
        cache_min_delta_chars = cache.get("min_delta_chars", 4000)
        cache_max_age_seconds = cache.get("max_age_seconds", 180)
        cache_max_entries = cache.get("max_entries", 128)

        # 解析警告配置
        warning = hm.get("warning", {})
        add_warning_header = warning.get("add_header", True)

        # 解析日志配置
        logging_cfg = hm.get("logging", {})
        logging_enabled = logging_cfg.get("enabled", True)
        logging_level = logging_cfg.get("level", "INFO")

        flat_data = {
            "strategies": strategies,
            "max_messages": max_messages,
            "max_chars": max_chars,
            "summary_keep_recent": summary_keep_recent,
            "summary_threshold": summary_threshold,
            "summary_max_length": summary_max_length,
            "retry_max_messages": retry_max_messages,
            "max_retries": max_retries,
            "estimate_threshold": estimate_threshold,
            "chars_per_token": chars_per_token,
            "summary_cache_enabled": cache_enabled,
            "summary_cache_min_delta_messages": cache_min_delta_messages,
            "summary_cache_min_delta_chars": cache_min_delta_chars,
            "summary_cache_max_age_seconds": cache_max_age_seconds,
            "summary_cache_max_entries": cache_max_entries,
            "add_warning_header": add_warning_header,
            "logging_enabled": logging_enabled,
            "logging_level": logging_level,
        }
        return HistoryConfig.from_dict(flat_data)

    # 扁平格式（直接参数）
    return HistoryConfig.from_dict(data)


def get_default_config_path() -> Path | None:
    """获取默认配置文件路径

    搜索顺序：
    1. 环境变量 AI_HISTORY_MANAGER_CONFIG
    2. 当前目录 ./config/history.yaml
    3. 当前目录 ./history.yaml
    4. 包内默认配置

    Returns:
        配置文件路径，不存在返回 None
    """
    # 环境变量
    env_path = os.environ.get("AI_HISTORY_MANAGER_CONFIG")
    if env_path and Path(env_path).exists():
        return Path(env_path)

    # 当前目录
    for relative_path in ["config/history.yaml", "history.yaml"]:
        path = Path.cwd() / relative_path
        if path.exists():
            return path

    # 包内默认配置
    package_config = Path(__file__).parent.parent.parent.parent / "config" / "history.yaml"
    if package_config.exists():
        return package_config

    return None
