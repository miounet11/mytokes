# AI History Manager

> 智能 AI 对话历史消息管理器，处理 API 输入长度限制

[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

## 功能特性

- 🔄 **自动截断** - 发送前按消息数/字符数自动截断
- 🧠 **智能摘要** - 用 AI 生成早期对话摘要，保留关键上下文
- 🔁 **错误重试** - 遇到长度错误时智能截断并重试
- 📊 **预估检测** - 发送前预估 token 数量，超限提前处理
- 💾 **摘要缓存** - 基于变化量检测的智能缓存机制
- 🔌 **中间件集成** - FastAPI 中间件，低侵入性集成

## 安装

```bash
pip install ai-history-manager
```

或者从源码安装：

```bash
git clone https://github.com/yourname/ai-history-manager.git
cd ai-history-manager
pip install -e .
```

## 快速开始

### 基础使用

```python
from ai_history_manager import HistoryManager, HistoryConfig, TruncateStrategy

# 创建配置
config = HistoryConfig(
    strategies=[TruncateStrategy.ERROR_RETRY, TruncateStrategy.SMART_SUMMARY],
    max_messages=30,
    max_chars=150000
)

# 创建管理器
manager = HistoryManager(config, cache_key="session_123")

# 同步预处理（不包含摘要）
processed_history = manager.pre_process(history, user_content)

# 异步预处理（支持智能摘要）
processed_history = await manager.pre_process_async(
    history, user_content, summary_generator=my_summary_func
)

# 检查是否发生截断
if manager.was_truncated:
    print(f"历史被截断: {manager.truncate_info}")
```

### FastAPI 中间件

```python
from fastapi import FastAPI
from ai_history_manager.middleware import HistoryManagerMiddleware
from ai_history_manager import HistoryConfig, TruncateStrategy

app = FastAPI()

# 方式 1: 使用配置文件
app.add_middleware(
    HistoryManagerMiddleware,
    config_path="config/history.yaml",
    summary_generator=my_summary_function
)

# 方式 2: 手动配置
config = HistoryConfig(
    strategies=[TruncateStrategy.ERROR_RETRY],
    max_messages=30
)
app.add_middleware(
    HistoryManagerMiddleware,
    config=config
)
```

### 处理长度错误

```python
from ai_history_manager.utils import is_content_length_error

# 检测是否为长度错误
if is_content_length_error(response.status_code, response.text):
    # 使用管理器处理
    truncated_history, should_retry = await manager.handle_length_error_async(
        history,
        retry_count=0,
        summary_generator=my_summary_func
    )

    if should_retry:
        # 使用截断后的历史重试请求
        response = await call_api(truncated_history)
```

### 使用 Kiro API 适配器

```python
from ai_history_manager.adapters import KiroSummaryAdapter
from ai_history_manager import HistoryManager

# 创建适配器
adapter = KiroSummaryAdapter(
    api_url="https://kiro.api.endpoint/v1/conversations",
    token="your-token",
    machine_id="machine-id"
)

# 使用适配器作为摘要生成器
manager = HistoryManager(config)
processed = await manager.pre_process_async(
    history, user_content,
    summary_generator=adapter.generate_summary
)
```

## 配置文件

创建 `config/history.yaml`:

```yaml
history_manager:
  # 启用的策略（可多选）
  strategies:
    - error_retry      # 错误重试（推荐）
    - smart_summary    # 智能摘要
    - auto_truncate    # 自动截断
    - pre_estimate     # 预估检测

  # 基础限制
  limits:
    max_messages: 30           # 最大消息数
    max_chars: 150000          # 最大字符数

  # 智能摘要配置
  summary:
    keep_recent: 10            # 保留最近 N 条消息
    threshold: 100000          # 触发摘要的字符数阈值
    max_length: 2000           # 摘要最大长度

  # 错误重试配置
  retry:
    max_messages: 20           # 重试时保留的消息数
    max_retries: 2             # 最大重试次数

  # 预估检测配置
  estimate:
    threshold: 180000          # 预估阈值（字符数）
    chars_per_token: 3.0       # 每 token 约等于多少字符

  # 摘要缓存配置
  cache:
    enabled: true
    min_delta_messages: 3      # 触发刷新的新增消息数
    min_delta_chars: 4000      # 触发刷新的新增字符数
    max_age_seconds: 180       # 最大缓存时间
```

## 策略说明

### 1. 错误重试 (ERROR_RETRY) - 推荐

遇到 `CONTENT_LENGTH_EXCEEDS_THRESHOLD` 等长度错误时：
1. 优先尝试生成摘要
2. 摘要失败则按数量截断
3. 每次重试减少 30% 消息
4. 支持配置最大重试次数

### 2. 智能摘要 (SMART_SUMMARY)

当历史消息超过阈值时：
1. 分离早期消息和最近消息
2. 调用 AI 生成早期消息摘要
3. 构建摘要 + 占位响应 + 最近消息的新历史
4. 支持摘要缓存，避免重复生成

### 3. 自动截断 (AUTO_TRUNCATE)

发送前自动检查并截断：
1. 先按消息数量截断
2. 再按字符数截断
3. 保留最近的消息

### 4. 预估检测 (PRE_ESTIMATE)

发送前预估 token 数量：
1. 使用 `chars_per_token` 估算
2. 超过阈值时预先截断
3. 留 20% 余量避免边界问题

## API 参考

### HistoryManager

```python
class HistoryManager:
    def __init__(self, config: HistoryConfig = None, cache_key: str = None)

    # 属性
    @property
    def was_truncated(self) -> bool
    @property
    def truncate_info(self) -> str

    # 同步方法
    def pre_process(self, history: list, user_content: str = "") -> list
    def handle_length_error(self, history: list, retry_count: int = 0) -> tuple[list, bool]

    # 异步方法
    async def pre_process_async(self, history, user_content, summary_generator) -> list
    async def handle_length_error_async(self, history, retry_count, summary_generator) -> tuple

    # 估算方法
    def estimate_tokens(self, text: str) -> int
    def estimate_history_size(self, history: list) -> tuple[int, int]
    def estimate_request_chars(self, history, user_content) -> tuple[int, int, int]

    # 判断方法
    def should_pre_truncate(self, history, user_content) -> bool
    def should_summarize(self, history) -> bool
```

### HistoryConfig

```python
@dataclass
class HistoryConfig:
    strategies: list[TruncateStrategy]
    max_messages: int = 30
    max_chars: int = 150000
    summary_keep_recent: int = 10
    summary_threshold: int = 100000
    summary_max_length: int = 2000
    retry_max_messages: int = 20
    max_retries: int = 2
    estimate_threshold: int = 180000
    chars_per_token: float = 3.0
    summary_cache_enabled: bool = True
    summary_cache_min_delta_messages: int = 3
    summary_cache_min_delta_chars: int = 4000
    summary_cache_max_age_seconds: int = 180
    add_warning_header: bool = True
```

## 测试

```bash
# 安装开发依赖
pip install -e ".[dev]"

# 运行测试
pytest

# 运行测试并显示覆盖率
pytest --cov=ai_history_manager --cov-report=term-missing
```

## 许可证

MIT License

## 致谢

本项目参考了 [kiro_proxy](https://github.com/yourname/kiro_proxy) 的优秀实现。
