# 用户输入预处理增强功能

## 功能概述

这个功能通过智能分析对话历史，自动为简短的用户输入补充项目上下文信息，让 AI 助手能够更准确地理解用户意图并提供更相关的回答。

## 核心特性

### 1. 智能上下文提取

- **触发条件**：每 5 条用户消息自动触发一次上下文提取
- **提取内容**：
  - 编程语言（language）
  - 框架/技术栈（framework）
  - 项目特性（features）
  - 最近讨论的主题（last_topics）

### 2. 消息自动增强

- **触发条件**：用户输入少于 20 个字符时自动增强
- **增强方式**：在用户消息前添加项目上下文
- **格式**：使用 XML 标签包裹，便于 AI 解析

### 3. 会话级缓存

- 每个会话独立维护上下文缓存
- 避免重复提取，提高响应速度
- 自动更新机制确保上下文时效性

## 技术实现

### 核心函数

#### `extract_project_context(messages, session_id)`

从对话历史中提取项目上下文信息。

**参数**：
- `messages`: 对话历史列表
- `session_id`: 会话 ID

**返回**：JSON 格式的上下文信息

**示例**：
```python
context = await extract_project_context(messages, "session-001")
# 返回: {"language": "Python", "framework": "FastAPI", ...}
```

#### `enhance_user_message(messages, session_id)`

为简短的用户消息添加项目上下文。

**参数**：
- `messages`: 当前消息列表
- `session_id`: 会话 ID

**返回**：增强后的消息列表

**示例**：
```python
# 输入
messages = [{"role": "user", "content": "优化这个函数"}]

# 输出
[{
  "role": "user",
  "content": """<project_context>
{"language": "Python", "framework": "FastAPI"}
</project_context>

<user_request>
优化这个函数
</user_request>"""
}]
```

### 集成点

在 `/v1/chat/completions` 端点中自动应用：

```python
# 1. 检查是否需要提取上下文
if should_extract_context(session_id, messages):
    await extract_project_context(messages, session_id)

# 2. 增强用户消息
messages = await enhance_user_message(messages, session_id)

# 3. 继续正常的 API 处理流程
```

## 使用场景

### 场景 1：代码优化请求

**用户输入**："优化这个函数"

**系统增强**：
```
<project_context>
{"language": "Python", "framework": "FastAPI", "features": ["异步处理", "JWT认证"]}
</project_context>

<user_request>
优化这个函数
</user_request>
```

**效果**：AI 知道要用 Python/FastAPI 的最佳实践来优化

### 场景 2：功能添加

**用户输入**："添加错误处理"

**系统增强**：
```
<project_context>
{"language": "TypeScript", "framework": "Vue3", "last_topics": ["用户登录", "表单验证"]}
</project_context>

<user_request>
添加错误处理
</user_request>
```

**效果**：AI 知道要为 Vue3 组件添加 TypeScript 类型安全的错误处理

### 场景 3：测试相关

**用户输入**："写个测试"

**系统增强**：
```
<project_context>
{"language": "JavaScript", "framework": "React", "features": ["Jest", "React Testing Library"]}
</project_context>

<user_request>
写个测试
</user_request>
```

**效果**：AI 知道要用 Jest 和 RTL 编写 React 组件测试

## 性能优化

### 1. 缓存机制

- 使用 `session_context_cache` 字典缓存每个会话的上下文
- 避免频繁调用 LLM 提取上下文
- 每 5 条消息更新一次，保持上下文新鲜度

### 2. 条件触发

- 只对简短消息（< 20 字符）进行增强
- 长消息通常已包含足够上下文，无需增强
- 减少不必要的处理开销

### 3. 异步处理

- 所有上下文提取和增强操作都是异步的
- 不阻塞主请求流程
- 提高整体响应速度

## 配置选项

### 环境变量

```bash
# 上下文提取间隔（消息数）
CONTEXT_EXTRACT_INTERVAL=5

# 触发增强的消息长度阈值
SHORT_MESSAGE_THRESHOLD=20

# 上下文缓存过期时间（秒）
CONTEXT_CACHE_TTL=3600
```

### 代码配置

在 `api_server.py` 中修改：

```python
# 调整提取间隔
if user_message_count % 10 == 0:  # 改为每 10 条消息
    await extract_project_context(messages, session_id)

# 调整长度阈值
if len(user_content) < 30:  # 改为 30 字符
    messages = await enhance_user_message(messages, session_id)
```

## 测试

运行测试套件：

```bash
python3 test_context_enhancement.py
```

测试覆盖：
- ✅ 上下文提取功能
- ✅ 消息增强功能
- ✅ 完整集成流程
- ✅ 缓存机制
- ✅ 异常处理

## 日志示例

```
2026-02-01 16:29:43 - INFO - [session-001] 🔄 触发上下文提取（用户消息数: 5）
2026-02-01 16:29:43 - INFO - [abc123] ✅ 上下文提取成功: 121 chars
2026-02-01 16:29:43 - INFO - [session-001] 🎯 上下文增强完成: 8 -> 207 chars
```

## 未来改进

### 短期

- [ ] 添加上下文质量评分机制
- [ ] 支持自定义提取提示词
- [ ] 添加上下文过期自动刷新

### 长期

- [ ] 支持多模态上下文（代码片段、图片等）
- [ ] 基于用户反馈的上下文优化
- [ ] 跨会话的项目上下文共享
- [ ] 上下文压缩和摘要功能

## 相关文件

- `api_server.py`: 主实现文件
- `test_context_enhancement.py`: 测试文件
- `CONTEXT_ENHANCEMENT.md`: 本文档

## 贡献者

- 初始实现：2026-02-01
- 测试完善：2026-02-01
