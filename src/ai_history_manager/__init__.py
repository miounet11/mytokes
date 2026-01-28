"""AI History Manager - 智能对话历史消息管理器

处理 AI API 的输入长度限制，提供四种核心策略：
1. 自动截断 - 发送前按数量/字符数截断
2. 智能摘要 - 用 AI 生成早期对话摘要
3. 错误重试 - 遇到长度错误时截断后重试
4. 预估检测 - 发送前预估 token 数量

使用示例:
```python
from ai_history_manager import HistoryManager, HistoryConfig, TruncateStrategy

# 创建配置
config = HistoryConfig(
    strategies=[TruncateStrategy.ERROR_RETRY, TruncateStrategy.SMART_SUMMARY],
    max_messages=30,
    max_chars=150000
)

# 创建管理器
manager = HistoryManager(config)

# 预处理历史消息
processed_history = manager.pre_process(history, user_content)

# 或使用异步版本（支持智能摘要）
processed_history = await manager.pre_process_async(
    history, user_content, summary_generator=my_summary_func
)
```
"""

from .config import (
    HistoryConfig,
    TruncateStrategy,
    load_config,
    load_config_from_file,
)
from .manager import HistoryManager
from .cache import SummaryCache

__version__ = "0.1.0"
__all__ = [
    "HistoryManager",
    "HistoryConfig",
    "TruncateStrategy",
    "SummaryCache",
    "load_config",
    "load_config_from_file",
]
