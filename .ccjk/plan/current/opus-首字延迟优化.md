# 功能规划：Opus 首字延迟优化

## 📋 概述

### 问题描述
Opus 模型请求经常需要 20 秒才返回首字，严重影响用户体验。

### 根本原因分析

从日志分析，延迟主要来自 **同步阻塞操作**：

```
请求到达
    ↓
[阻塞 1] 触发智能摘要 (call_kiro_for_summary) → 3-7 秒
    ↓
[阻塞 2] 同步更新项目上下文 (extract_project_context) → 3-6 秒
    ↓
实际发送 Opus 请求
    ↓
Opus 模型处理 → 5-10 秒
    ↓
首字返回 (总计 15-25 秒)
```

### 关键代码位置

1. **摘要触发** (`api_server.py:3265-3278`)
   ```python
   if should_summarize:
       logger.info(f"[{request_id}] 触发智能摘要...")
       processed_messages = await manager.pre_process_async(...)  # 阻塞 3-7s

       if CONTEXT_ENHANCEMENT_CONFIG["integrate_with_summary"]:
           logger.info(f"[{request_id}] 🔄 摘要触发，同步更新项目上下文...")
           context = await extract_project_context(...)  # 阻塞 3-6s
   ```

2. **上下文提取** (`api_server.py:1021-1100`)
   - 调用 LLM API 提取上下文
   - 使用 `stream=False`，必须等待完整响应
   - timeout=30s

3. **摘要生成** (`api_server.py:1211-1244`)
   - 调用 Haiku 模型生成摘要
   - 使用 `stream=False`
   - timeout=60s

## 🎯 优化方案

### 方案 A：异步化预处理（推荐）

**核心思路**：将摘要和上下文提取改为异步后台任务，不阻塞主请求。

```
请求到达
    ↓
[并行] 启动后台任务：摘要 + 上下文提取
    ↓
[立即] 发送 Opus 请求（使用上次缓存的上下文）
    ↓
首字返回 (5-10 秒)
    ↓
[后台] 摘要完成后更新缓存，下次请求使用
```

**优点**：
- 首字延迟从 20s 降到 5-10s
- 不影响现有功能
- 上下文仍然会更新，只是延迟一个请求周期

**缺点**：
- 上下文更新有一个请求的延迟
- 需要处理并发安全

### 方案 B：条件触发优化

**核心思路**：减少不必要的摘要和上下文提取。

1. **提高摘要阈值**：从 100K 提高到 150K
2. **上下文提取频率限制**：每 5 个用户消息才提取一次
3. **缓存复用**：如果上下文未过期，直接使用缓存

### 方案 C：流式预处理

**核心思路**：在等待 Opus 响应时并行处理。

```
请求到达
    ↓
[并行启动]
├── 发送 Opus 请求
└── 摘要 + 上下文提取
    ↓
Opus 首字返回（不等待预处理完成）
    ↓
预处理完成后更新缓存
```

## 📐 技术方案（方案 A 详细设计）

### 1. 新增后台任务队列

```python
import asyncio
from collections import deque

# 后台任务队列
_background_tasks: deque[asyncio.Task] = deque(maxlen=100)

async def schedule_background_task(coro, task_name: str):
    """调度后台任务，不阻塞主流程"""
    task = asyncio.create_task(coro)
    task.set_name(task_name)
    _background_tasks.append(task)
    return task
```

### 2. 修改摘要触发逻辑

```python
# 原代码 (api_server.py:3265-3278)
if should_summarize:
    # 改为后台执行
    asyncio.create_task(
        _background_summarize(messages, session_id, request_id)
    )
    # 使用上次的摘要结果（如果有）
    processed_messages = manager.get_cached_summary(messages)
```

### 3. 新增后台摘要函数

```python
async def _background_summarize(messages, session_id, request_id):
    """后台执行摘要和上下文更新"""
    try:
        # 执行摘要
        processed = await manager.pre_process_async(
            messages, user_content, call_kiro_for_summary
        )

        # 更新上下文
        if CONTEXT_ENHANCEMENT_CONFIG["integrate_with_summary"]:
            context = await extract_project_context(messages, session_id)
            if context:
                update_session_context(session_id, context, ...)

        # 缓存结果供下次使用
        manager.cache_summary(session_id, processed)
        logger.info(f"[{request_id}] ✅ 后台摘要完成")
    except Exception as e:
        logger.warning(f"[{request_id}] 后台摘要失败: {e}")
```

### 4. 摘要缓存机制

```python
# 在 HistoryManager 类中添加
class HistoryManager:
    def __init__(self):
        self._summary_cache: dict[str, dict] = {}

    def cache_summary(self, session_id: str, summary_data: dict):
        self._summary_cache[session_id] = {
            "data": summary_data,
            "timestamp": time.time(),
            "message_count": len(summary_data.get("messages", []))
        }

    def get_cached_summary(self, session_id: str) -> dict | None:
        cached = self._summary_cache.get(session_id)
        if cached and time.time() - cached["timestamp"] < 300:  # 5分钟有效
            return cached["data"]
        return None
```

## ✅ 验收标准

1. **性能指标**
   - Opus 首字延迟从 20s 降到 10s 以内
   - P95 延迟 < 15s

2. **功能验收**
   - 摘要功能正常工作（后台执行）
   - 上下文增强正常工作
   - 不影响现有的续传机制

3. **稳定性**
   - 后台任务失败不影响主请求
   - 内存使用稳定（任务队列有上限）

## ⏱️ 实施计划

### 阶段 1：后台任务基础设施
- [ ] 添加后台任务调度函数
- [ ] 添加任务状态监控

### 阶段 2：摘要异步化
- [ ] 修改摘要触发逻辑为异步
- [ ] 添加摘要缓存机制
- [ ] 修改 `should_summarize` 逻辑

### 阶段 3：上下文提取异步化
- [ ] 将 `extract_project_context` 改为后台执行
- [ ] 使用缓存的上下文增强当前请求

### 阶段 4：测试验证
- [ ] 测试首字延迟
- [ ] 测试摘要功能
- [ ] 测试上下文增强

---

## 📝 迭代历史

### v1 - 2026-02-02
- 初始版本
- 分析延迟根因
- 设计异步化方案
