# AI History Manager - 项目说明文档

## 项目概述

AI History Manager 是一个智能对话历史消息管理代理服务，主要用于解决 AI API 的上下文长度限制问题。它作为中间层代理，支持 **Anthropic API 格式** 和 **OpenAI API 格式**，并通过智能截断和摘要策略来管理对话历史。

### 核心功能

1. **协议转换**：Anthropic 格式 ↔ OpenAI 格式双向转换
2. **工具调用处理**：自动转换内联工具调用为标准 `tool_use` 格式
3. **历史消息管理**：智能截断、摘要、预估等多种策略
4. **Claude Code CLI 兼容**：完美支持 Claude Code CLI 的工具调用功能

---

## 架构图

```
┌─────────────────────────────────────────────────────────────────┐
│                      Claude Code CLI                             │
│                    (Anthropic API 格式)                          │
└─────────────────────────┬───────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────────┐
│              AI History Manager (Port 8100)                      │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  1. 接收 Anthropic 格式请求                                │   │
│  │  2. 历史消息管理 (截断/摘要/预估)                          │   │
│  │  3. 转换为 OpenAI 格式                                     │   │
│  │  4. 内联 tool_use/tool_result 为文本                       │   │
│  └──────────────────────────────────────────────────────────┘   │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  响应处理:                                                  │   │
│  │  1. 接收 OpenAI 格式响应                                   │   │
│  │  2. 解析内联工具调用 [Calling tool: xxx]                   │   │
│  │  3. 转换为 Anthropic tool_use content blocks              │   │
│  │  4. 返回标准 Anthropic 格式                                │   │
│  └──────────────────────────────────────────────────────────┘   │
└─────────────────────────┬───────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────────┐
│              Tokens Gateway (Port 8000)                          │
│              (fakeoai/tokens Go 程序)                            │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  - 路由: /kiro/v1/chat/completions                        │   │
│  │  - OpenAI 格式 → Kiro API 格式                            │   │
│  │  - 多渠道/密钥管理                                         │   │
│  └──────────────────────────────────────────────────────────┘   │
└─────────────────────────┬───────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────────┐
│                      Kiro API                                    │
│                   (AWS Bedrock)                                  │
└─────────────────────────────────────────────────────────────────┘
```

---

## 配置说明

### 1. 服务配置 (`api_server.py`)

```python
# Kiro 代理地址 (tokens 网关, 使用内网地址)
KIRO_PROXY_BASE = "http://127.0.0.1:8000"
KIRO_PROXY_URL = f"{KIRO_PROXY_BASE}/kiro/v1/chat/completions"
KIRO_MODELS_URL = f"{KIRO_PROXY_BASE}/kiro/v1/models"
KIRO_API_KEY = "dba22273-65d3-4dc1-8ce9-182f680b2bf5"

# 服务端口
SERVICE_PORT = 8100
REQUEST_TIMEOUT = 300  # 5分钟超时
```

### 2. 历史消息管理配置

```python
HISTORY_CONFIG = HistoryConfig(
    strategies=[
        TruncateStrategy.PRE_ESTIMATE,      # 发送前预估 token 数量
        TruncateStrategy.AUTO_TRUNCATE,     # 按数量/字符数自动截断
        TruncateStrategy.SMART_SUMMARY,     # AI 生成早期对话摘要
        TruncateStrategy.ERROR_RETRY,       # 遇到长度错误时截断重试
    ],
    max_messages=25,           # 最大消息数量
    max_chars=100000,          # 最大字符数
    summary_keep_recent=8,     # 摘要时保留最近消息数
    summary_threshold=80000,   # 触发摘要的字符阈值
    retry_max_messages=15,     # 重试时保留的消息数
    max_retries=3,             # 最大重试次数
    estimate_threshold=100000, # 预估截断阈值
    summary_cache_enabled=True,
    add_warning_header=True,
)
```

### 3. 策略说明

| 策略 | 说明 | 触发条件 |
|------|------|----------|
| `PRE_ESTIMATE` | 发送前预估 token 数量并截断 | 估算 token > `estimate_threshold` |
| `AUTO_TRUNCATE` | 按消息数/字符数自动截断 | 消息数 > `max_messages` 或字符数 > `max_chars` |
| `SMART_SUMMARY` | 使用 AI 生成早期对话摘要 | 字符数 > `summary_threshold` |
| `ERROR_RETRY` | 遇到长度错误时截断后重试 | API 返回 "Input is too long" 错误 |

---

## API 端点

### Anthropic 兼容端点

| 端点 | 方法 | 说明 |
|------|------|------|
| `/v1/messages` | POST | Anthropic Messages API (主要端点) |
| `/v1/messages/count_tokens` | POST | Token 计数 |
| `/v1/models` | GET | 模型列表 |

### OpenAI 兼容端点

| 端点 | 方法 | 说明 |
|------|------|------|
| `/v1/chat/completions` | POST | OpenAI Chat Completions API |

### 管理端点

| 端点 | 方法 | 说明 |
|------|------|------|
| `/` | GET | 健康检查 |
| `/admin/config` | GET | 查看当前配置 |
| `/admin/config/history` | POST | 更新历史管理配置 |

---

## 工具调用处理

### 问题背景

Tokens Gateway (Go 程序) 不支持 OpenAI 的 `tool_calls` / `role="tool"` 格式。因此需要：
1. **请求处理**：将 Anthropic 的 `tool_use` / `tool_result` 转换为内联文本
2. **响应处理**：将模型返回的内联工具调用解析回 Anthropic 标准格式

### 请求转换

Anthropic 格式（原始）:
```json
{
  "role": "assistant",
  "content": [
    {"type": "tool_use", "id": "toolu_xxx", "name": "Edit", "input": {...}}
  ]
}
```

转换为内联文本:
```json
{
  "role": "assistant",
  "content": "[Calling tool: Edit]\nInput: {...}"
}
```

### 响应解析

当模型返回内联格式时：
```
[Calling tool: Edit]
  Input: {"file_path": "/path/to/file", "new_string": "...", "old_string": "..."}
```

自动解析为 Anthropic 标准 `tool_use` content block：
```json
{
  "type": "tool_use",
  "id": "toolu_xxx",
  "name": "Edit",
  "input": {"file_path": "/path/to/file", "new_string": "...", "old_string": "..."}
}
```

### JSON 解析增强

支持处理复杂 JSON：
- **嵌套 JSON**：使用括号计数算法，支持任意嵌套深度
- **未转义换行符**：自动转义 JSON 字符串值中的控制字符
- **尾随逗号**：自动移除 JSON 中的尾随逗号

---

## 启动方式

### 直接启动

```bash
cd /www/wwwroot/ai-history-manager
uvicorn api_server:app --host 0.0.0.0 --port 8100
```

### 后台运行

```bash
cd /www/wwwroot/ai-history-manager
nohup uvicorn api_server:app --host 0.0.0.0 --port 8100 > /var/log/ai-history-manager.log 2>&1 &
```

### 服务管理

```bash
# 查看服务状态
ps aux | grep "uvicorn.*8100"

# 查看日志
tail -f /var/log/ai-history-manager.log

# 停止服务
pkill -f "uvicorn.*8100"
```

---

## Claude Code CLI 配置

在 Claude Code CLI 中配置自定义 API 端点：

```bash
# 设置 API 端点
export ANTHROPIC_BASE_URL="http://your-server:8100"
export ANTHROPIC_API_KEY="your-api-key"

# 或在配置文件中设置
claude config set apiBaseUrl "http://your-server:8100"
```

---

## 支持的模型

| 模型 ID | 说明 |
|---------|------|
| `claude-opus-4-5-20251101` | Claude Opus 4.5 |
| `claude-sonnet-4-5-20250929` | Claude Sonnet 4.5 |
| `claude-haiku-4-5-20251001` | Claude Haiku 4.5 |
| `claude-sonnet-4` | Claude Sonnet 4 |
| `claude-haiku-4` | Claude Haiku 4 |
| `claude-opus-4` | Claude Opus 4 |

---

## 文件结构

```
/www/wwwroot/ai-history-manager/
├── api_server.py              # 主服务文件
├── pyproject.toml             # 项目配置
├── README.md                  # 英文文档
├── shuoming.md                # 中文说明文档
├── src/
│   └── ai_history_manager/    # 核心库
│       ├── __init__.py        # 导出接口
│       ├── manager.py         # 历史管理器
│       ├── config/            # 配置模块
│       ├── cache/             # 摘要缓存
│       ├── adapters/          # API 适配器
│       ├── middleware/        # FastAPI 中间件
│       ├── strategies/        # 截断策略
│       └── utils/             # 工具函数
├── tests/                     # 测试文件
└── config/                    # 配置文件目录
```

---

## 日志格式

```
2026-01-28 08:02:18,264 - ai_history_manager_api - INFO - [67c58b87] Anthropic -> OpenAI: model=claude-opus-4-5-20251101, stream=False, msgs=218->31, chars=57302
```

格式说明：
- `[67c58b87]` - 请求 ID
- `Anthropic -> OpenAI` - 请求类型（协议转换方向）
- `model=xxx` - 使用的模型
- `stream=True/False` - 是否流式
- `msgs=218->31` - 消息数量（原始->处理后）
- `chars=57302` - 字符数

---

## 常见问题

### 1. "Improperly formed request" 错误

**原因**：Tokens Gateway 不支持 `role="tool"` 作为最后一条消息

**解决方案**：已在代码中自动处理，确保最后一条消息是 `role="user"`

### 2. Claude Code CLI 显示 `[Calling tool: xxx]` 文本

**原因**：内联工具调用未正确解析为 Anthropic 格式

**解决方案**：已实现自动解析，包括：
- 支持缩进的 `Input:`
- 支持嵌套 JSON
- 支持字符串中的换行符

### 3. JSON 解析失败

**原因**：JSON 字符串值中包含未转义的控制字符

**解决方案**：`escape_json_string_newlines()` 函数自动处理

---

## 更新日志

### 2026-01-28 (v2)

- **高并发优化**：全局 HTTP 连接池，支持万级并发请求
- **Token 计数**：自动计算并返回 input_tokens 和 output_tokens
- **HTTP/2 支持**：启用 HTTP/2 多路复用提升性能
- **uvloop 加速**：使用 uvloop 替代标准 asyncio 事件循环

### 2026-01-28

- 修复 JSON 字符串中未转义换行符导致的解析失败
- 新增 `escape_json_string_newlines()` 函数
- 增强 `extract_json_from_position()` 的容错能力

---

## 高并发配置

### 连接池配置

```python
HTTP_POOL_MAX_CONNECTIONS = 10000    # 最大连接数（无限制模式）
HTTP_POOL_MAX_KEEPALIVE = 1000       # 保持活跃的连接数
HTTP_POOL_KEEPALIVE_EXPIRY = 60      # 连接保持时间(秒)
```

### 启动命令

```bash
bash start.sh
# 或手动启动
uvicorn api_server:app \
    --host 0.0.0.0 \
    --port 8100 \
    --workers 4 \
    --loop uvloop \
    --http httptools \
    --no-access-log
```

---

## Token 计数

### 响应格式

所有响应（流式和非流式）都包含准确的 token 计数：

**流式响应 (message_start)**：
```json
{
  "type": "message_start",
  "message": {
    "usage": {"input_tokens": 1234, "output_tokens": 0}
  }
}
```

**流式响应 (message_delta)**：
```json
{
  "type": "message_delta",
  "delta": {"stop_reason": "end_turn"},
  "usage": {"output_tokens": 567}
}
```

**非流式响应**：
```json
{
  "usage": {
    "input_tokens": 1234,
    "output_tokens": 567
  }
}
```

### 计算方式

1. **优先使用 API 返回值**：从上游 API 获取精确的 token 数量
2. **智能估算回退**：
   - 英文/代码：约 4 字符 = 1 token
   - 中文：约 1.5 字符 = 1 token

---

## 依赖

```
pyyaml>=6.0
httpx[http2]>=0.24.0
fastapi>=0.100.0
uvicorn
uvloop
httptools
pydantic
```

---

## 联系方式

如有问题，请查看日志或联系系统管理员。
