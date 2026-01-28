"""配置管理模块"""
from .config import HistoryConfig, TruncateStrategy, load_config, load_config_from_file

__all__ = ["HistoryConfig", "TruncateStrategy", "load_config", "load_config_from_file"]
