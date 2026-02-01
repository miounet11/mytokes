# 智能摘要（Smart Summary）机制深度分析

## 📋 概述

智能摘要是 `ai_history_manager` 库的核心功能之一，用于在对话历史过长时，将早期消息压缩为摘要，保留最近的完整上下文。

---

## 🔄 请求处理流程

```
┌─────────────────────────────────────────────────────────────────────┐
│                    Claude Code CLI 发送请求                          │
└─────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    api_server.py 接收请求                            │
│                    /v1/messages 或 /v1/chat/completions             │
└─────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────┐
│  ① 创建 HistoryManager                                              │
│     session_id = generate_session_id(messages)                      │
│     manager = HistoryManager(HISTORY_CONFIG, cache_key=session_id)  │
└─────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────┐
│  ② 检查是否需要摘要                                                  │
│     manager.should_summarize(messages)                              │
│                                                                     │
│     触发条件：                                                       │
│     - 总字符数 > summary_threshold (80000)                          │
│     - 消息数 > summary_keep_recent (8)                              │
│     - 或消息数 > max_messages (25)                                  │
│     - 或总字符数 > max_chars (100000)                               │
└─────────────────────────────────────────────────────────────────────┘
                                    │
                    ┌───────────────┴───────────────┐
                    │                               │
              需要摘要                          不需要摘要
                    │                               │
                    ▼                               ▼
┌───────────────────────────────┐   ┌───────────────────────────────┐
│  ③a 异步预处理（带摘要）       │   │  ③b 同步预处理（简单截断）     │
│  pre_process_async()          │   │  pre_process()                │
│                               │   │                               │
│  - 分离早期消息和最近消息      │   │  - 按数量截断                  │
│  - 调用 AI 生成摘要           │   │  - 按字符数截断                │
│  - 构建带摘要的新历史         │   │                               │
└───────────────────────────────┘   └───────────────────────────────┘
                    │                               │
                    └───────────────┬───────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────┐
│  ④ 发送请求到上游 API (Kiro/AWS Bedrock)                            │
│     使用处理后的 processed_messages                                  │
└─────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────┐
│  ⑤ 如果上游返回 "Input too long" 错误                               │
│     触发 handle_length_error_async()                                │
│     - 进一步截断消息                                                 │
│     - 可能再次生成摘要                                               │
│     - 重试请求                                                       │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 🎯 智能摘要的具体作用

### 1. 触发时机

智能摘要在以下情况下触发：

| 条件 | 阈值 | 说明 |
|------|------|------|
| `should_smart_summarize()` | 字符数 > 80000 且 消息数 > 8 | 主动摘要 |
| `should_auto_truncate_summarize()` | 消息数 > 25 或 字符数 > 100000 | 自动截断前摘要 |
| `should_pre_summary_for_error_retry()` | 字符数 > 100000 | 错误重试前预摘要 |

### 2. 摘要生成过程

```python
# 位置：manager.py:397-443
async def compress_with_summary(history, summary_generator):
    # 1. 检查是否需要摘要
    if total_chars <= summary_threshold:
        return history  # 不需要摘要

    # 2. 分离消息
    old_history = history[:-keep_recent]    # 早期消息（将被摘要）
    recent_history = history[-keep_recent:]  # 最近消息（保留完整）

    # 3. 生成摘要
    summary = await generate_summary(old_history, summary_generator)

    # 4. 构建新历史
    result = [
        {"role": "user", "content": f"[Earlier conversation summary]\n{summary}\n\n[Continuing...]"},
        {"role": "assistant", "content": "I understand the context. Let's continue."},
        ...recent_history
    ]
    return result
```

### 3. 摘要生成函数

```python
# 位置：api_server.py:710-742
async def call_kiro_for_summary(prompt: str) -> str:
    """调用 Kiro API 生成摘要"""
    request_body = {
        "model": "claude-haiku-4",  # 使用快速模型
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "max_tokens": 2000,
    }
    # 发送请求并返回摘要文本
```

**关键点**：摘要使用 `claude-haiku-4` 模型，速度快、成本低。

---

## 📊 配置参数详解

```python
# 位置：api_server.py:94-110
HISTORY_CONFIG = HistoryConfig(
    strategies=[
        TruncateStrategy.PRE_ESTIMATE,      # 预估检测
        TruncateStrategy.AUTO_TRUNCATE,     # 自动截断
        TruncateStrategy.SMART_SUMMARY,     # 智能摘要 ⭐
        TruncateStrategy.ERROR_RETRY,       # 错误重试
    ],
    max_messages=25,           # 最大消息数
    max_chars=100000,          # 最大字符数
    summary_keep_recent=8,     # 摘要时保留最近 N 条消息
    summary_threshold=80000,   # 触发摘要的字符数阈值
    retry_max_messages=15,     # 重试时保留的消息数
    max_retries=3,             # 最大重试次数
    estimate_threshold=100000, # 预估截断阈值
)
```

### 参数作用说明

| 参数 | 值 | 作用 |
|------|-----|------|
| `summary_threshold` | 80000 | 当总字符数超过此值时触发摘要 |
| `summary_keep_recent` | 8 | 摘要时保留最近 8 条消息完整 |
| `max_messages` | 25 | 超过 25 条消息时触发自动截断 |
| `max_chars` | 100000 | 超过 100000 字符时触发自动截断 |

---

## 🔍 实际生效环节

### 环节 1：请求预处理（主要生效点）

```python
# 位置：api_server.py:2930-2935
if manager.should_summarize(messages):
    # ⭐ 这里调用智能摘要
    processed_messages = await manager.pre_process_async(
        messages, user_content, call_kiro_for_summary
    )
else:
    processed_messages = manager.pre_process(messages, user_content)
```

**作用**：在发送请求到上游 API 之前，检查消息是否过长，如果过长则生成摘要压缩。

### 环节 2：错误重试处理

```python
# 位置：api_server.py:2998-3011
if is_content_length_error(response.status_code, error_str):
    logger.info(f"[{request_id}] 检测到长度错误，尝试截断重试")

    # ⭐ 这里可能再次调用摘要
    truncated, should_retry = await manager.handle_length_error_async(
        kiro_request["messages"],
        retry_count,
        call_kiro_for_summary,
    )

    if should_retry:
        kiro_request["messages"] = truncated
        retry_count += 1
        continue  # 重试请求
```

**作用**：当上游 API 返回 "Input too long" 错误时，进一步截断并可能生成摘要，然后重试。

---

## 📈 摘要效果示例

### 摘要前

```
消息数: 50 条
总字符数: 150000
```

### 摘要后

```
消息数: 10 条 (2 条摘要 + 8 条最近消息)
总字符数: ~30000

结构:
1. [user] "[Earlier conversation summary]\n用户目标是...已完成...当前状态..."
2. [assistant] "I understand the context. Let's continue."
3-10. [最近 8 条完整消息]
```

---

## ⚠️ 注意事项

### 1. 摘要可能丢失细节

摘要会压缩早期对话，可能丢失一些细节信息。如果用户询问早期对话的具体内容，可能无法准确回答。

### 2. 摘要生成需要额外 API 调用

每次生成摘要都会调用一次 `claude-haiku-4` API，增加延迟和成本。

### 3. 摘要缓存

```python
summary_cache_enabled=True
```

启用摘要缓存后，相同会话的摘要会被缓存，避免重复生成。

---

## 🔧 调优建议

### 如果上下文丢失太快

```python
summary_keep_recent=12,    # 增加保留的最近消息数
summary_threshold=120000,  # 提高摘要触发阈值
```

### 如果经常遇到 "Input too long" 错误

```python
max_chars=80000,           # 降低最大字符数
summary_threshold=60000,   # 更早触发摘要
```

### 如果响应速度慢

```python
# 禁用智能摘要，只使用简单截断
strategies=[
    TruncateStrategy.PRE_ESTIMATE,
    TruncateStrategy.AUTO_TRUNCATE,
    # TruncateStrategy.SMART_SUMMARY,  # 注释掉
    TruncateStrategy.ERROR_RETRY,
],
```

---

## 📝 总结

| 问题 | 答案 |
|------|------|
| 智能摘要在哪里生效？ | 请求预处理阶段（发送到上游 API 之前） |
| 什么时候触发？ | 字符数 > 80000 且 消息数 > 8 |
| 谁生成摘要？ | `claude-haiku-4` 模型 |
| 保留多少最近消息？ | 8 条（可配置） |
| 摘要内容包括什么？ | 用户目标、已完成操作、当前状态 |

**核心价值**：在保持对话连贯性的同时，避免 "Input too long" 错误，让长对话能够持续进行。
