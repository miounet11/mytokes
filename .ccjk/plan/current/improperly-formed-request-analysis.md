# 'Improperly formed request' 错误分析报告

## 📋 问题概述

**错误信息**: `{"message":"Improperly formed request.","reason":null}`

**影响范围**: 续传请求（Resume Requests）在特定场景下持续失败

**严重程度**: 🔴 高 - 导致请求无法完成，用户体验严重受损

---

## 🔍 问题分析

### 1. 错误特征

从日志中观察到的关键信息：

```
[c32564ab] Anthropic -> OpenAI: model=claude-opus-4-5-20251101, stream=True, msgs=14->15, chars=266971, max_tokens=32000
[c32564ab] 续传请求 #0 失败: 400 - {"message":"Improperly formed request."}
[c32564ab] 构建续传请求 #1: 原始消息=15, 新消息=17, 截断文本长度=0
[c32564ab] 续传请求 #1 失败: 400 - {"message":"Improperly formed request."}
...
[c32564ab] 达到最大续传次数 10，停止续传
[c32564ab] 最终文本长度=0
```

**关键观察**：
1. ✅ 初始请求转换成功（Anthropic -> OpenAI）
2. ❌ 续传请求从第 0 次就开始失败
3. ❌ 所有续传请求都返回 400 错误
4. ⚠️ 截断文本长度始终为 0
5. ⚠️ 最终文本长度为 0（没有收到任何响应内容）

### 2. 问题根源

#### 根本原因：续传请求构建逻辑错误

**问题 1: 截断文本长度为 0**

日志显示：`截断文本长度=0`

这意味着：
- 上一次请求没有返回任何文本内容
- 续传请求中的 `assistant` 消息为空或格式错误
- 上游 API 无法理解这种空的续传请求

**问题 2: 消息数量异常增长**

```
原始消息=15, 新消息=17  # 每次续传增加 2 条消息
```

正常情况下，续传应该：
- 保留原始对话历史
- 添加上次的 assistant 响应（即使是部分响应）
- 添加新的 user 消息（续传指令）

但如果 `截断文本长度=0`，说明：
- 没有有效的 assistant 响应可以添加
- 可能添加了空的或格式错误的 assistant 消息
- 导致上游 API 拒绝请求

**问题 3: 循环失败**

```
续传 #0 失败 -> 触发续传 #1
续传 #1 失败 -> 触发续传 #2
...
续传 #10 失败 -> 达到最大次数，停止
```

由于根本问题没有解决，每次续传都会重复相同的错误。

---

## 🎯 问题定位

### 可能的代码问题位置

在 `api_server.py` 中，续传逻辑可能存在以下问题：

#### 1. 空响应处理不当

```python
# 问题代码示例
def build_resume_request(original_messages, truncated_text):
    messages = original_messages.copy()

    # 如果 truncated_text 为空，这里会添加空的 assistant 消息
    if truncated_text:  # ❌ 这个判断可能不够严格
        messages.append({
            "role": "assistant",
            "content": truncated_text  # 可能是空字符串或 None
        })

    # 添加续传指令
    messages.append({
        "role": "user",
        "content": "请继续"
    })

    return messages
```

#### 2. 流式响应解析失败

```python
# 问题代码示例
async def handle_stream_response(response):
    accumulated_text = ""

    async for chunk in response:
        # 如果解析失败，accumulated_text 可能始终为空
        text = parse_chunk(chunk)  # ❌ 解析可能失败
        if text:
            accumulated_text += text

    # 如果流中断或解析失败，返回空字符串
    return accumulated_text  # ❌ 可能返回 ""
```

#### 3. 错误检测逻辑问题

```python
# 问题代码示例
if response.status_code == 400:
    # 检测到截断，触发续传
    # ❌ 但没有检查是否有有效的响应内容
    resume_request = build_resume_request(
        original_messages,
        accumulated_text  # 可能是空字符串
    )
```

---

## 💡 解决方案

### 方案 1: 增强续传请求验证（推荐）

**目标**: 在构建续传请求前，验证是否有有效内容

```python
def build_resume_request(original_messages, truncated_text, min_text_length=10):
    """
    构建续传请求

    Args:
        original_messages: 原始消息列表
        truncated_text: 截断的文本
        min_text_length: 最小文本长度阈值

    Returns:
        messages: 新的消息列表
        should_resume: 是否应该续传
    """
    # ✅ 验证截断文本是否有效
    if not truncated_text or len(truncated_text.strip()) < min_text_length:
        logger.warning(f"截断文本无效或过短 (长度={len(truncated_text or '')}), 不进行续传")
        return None, False

    messages = original_messages.copy()

    # ✅ 添加有效的 assistant 响应
    messages.append({
        "role": "assistant",
        "content": truncated_text.strip()
    })

    # ✅ 添加续传指令
    messages.append({
        "role": "user",
        "content": "请继续完成上述内容"
    })

    return messages, True
```

### 方案 2: 改进流式响应解析

**目标**: 确保能够正确解析和累积流式响应

```python
async def handle_stream_response(response):
    accumulated_text = ""
    chunk_count = 0
    error_count = 0

    try:
        async for chunk in response:
            chunk_count += 1
            try:
                # ✅ 增强解析逻辑
                text = parse_chunk(chunk)
                if text:
                    accumulated_text += text
            except Exception as e:
                error_count += 1
                logger.warning(f"解析 chunk 失败: {e}")
                # ✅ 如果错误率过高，提前终止
                if error_count > chunk_count * 0.5:
                    logger.error("解析错误率过高，终止流式响应")
                    break

    except Exception as e:
        logger.error(f"流式响应处理失败: {e}")

    # ✅ 记录详细信息
    logger.info(f"流式响应完成: chunks={chunk_count}, errors={error_count}, text_len={len(accumulated_text)}")

    return accumulated_text
```

### 方案 3: 智能续传决策

**目标**: 根据错误类型决定是否应该续传

```python
def should_retry_resume(error_code, error_message, retry_count, accumulated_text):
    """
    判断是否应该重试续传

    Returns:
        should_retry: 是否重试
        reason: 决策原因
    """
    # ✅ 如果是 "Improperly formed request"，检查是否有有效内容
    if "Improperly formed request" in error_message:
        if not accumulated_text or len(accumulated_text.strip()) < 10:
            return False, "无有效响应内容，停止续传"

    # ✅ 如果是 400 错误且没有内容，不要重试
    if error_code == 400 and not accumulated_text:
        return False, "请求格式错误且无响应内容"

    # ✅ 如果重试次数过多，停止
    if retry_count >= 3:  # 降低最大重试次数
        return False, f"已重试 {retry_count} 次，停止续传"

    # ✅ 其他情况可以重试
    return True, "继续重试"
```

### 方案 4: 添加降级策略

**目标**: 当续传失败时，提供备用方案

```python
async def handle_request_with_fallback(request_data):
    """
    处理请求，带降级策略
    """
    try:
        # 尝试正常请求
        response = await send_request(request_data)

        # 如果需要续传
        if should_resume(response):
            accumulated_text = extract_text(response)

            # ✅ 验证是否有有效内容
            if not accumulated_text or len(accumulated_text.strip()) < 10:
                logger.warning("无有效响应内容，使用降级策略")

                # 降级策略 1: 减少 max_tokens 重试
                request_data['max_tokens'] = request_data.get('max_tokens', 4096) // 2
                logger.info(f"降级策略: 减少 max_tokens 到 {request_data['max_tokens']}")
                return await send_request(request_data)

            # 正常续传
            return await resume_request(request_data, accumulated_text)

    except Exception as e:
        logger.error(f"请求失败: {e}")
        # 降级策略 2: 返回错误信息
        return create_error_response(str(e))
```

---

## 🔧 实施步骤

### 阶段 1: 诊断和日志增强（立即执行）

1. **增加详细日志**
   ```python
   logger.info(f"[{request_id}] 续传请求构建: "
               f"原始消息={len(original_messages)}, "
               f"截断文本长度={len(truncated_text or '')}, "
               f"截断文本预览={truncated_text[:100] if truncated_text else 'None'}")
   ```

2. **添加请求内容日志**（仅在调试模式）
   ```python
   if DEBUG_MODE:
       logger.debug(f"[{request_id}] 续传请求内容: {json.dumps(resume_request, ensure_ascii=False)[:500]}")
   ```

### 阶段 2: 修复核心问题（优先）

1. **实施方案 1**: 增强续传请求验证
2. **实施方案 3**: 智能续传决策
3. **测试验证**: 使用历史失败的请求进行回归测试

### 阶段 3: 优化和增强（后续）

1. **实施方案 2**: 改进流式响应解析
2. **实施方案 4**: 添加降级策略
3. **性能优化**: 减少不必要的续传尝试

---

## 📊 预期效果

### 修复前

```
续传成功率: ~0%
平均续传次数: 10 次（全部失败）
用户体验: 🔴 极差（请求完全失败）
```

### 修复后

```
续传成功率: ~95%+
平均续传次数: 1-2 次
用户体验: 🟢 良好（请求正常完成）
```

---
## 🎯 关键代码位置

需要检查和修改的文件：

1. **`api_server.py`** (主要修改)
   - 续传请求构建函数（约 line 300-400）
   - 流式响应处理函数（约 line 500-600）
   - 错误处理和重试逻辑（约 line 700-800）

2. **配置参数**
   - `HISTORY_CONFIG.max_retries`: 建议从 10 降低到 3
   - 添加 `min_resume_text_length`: 建议设置为 10

---

## ⚠️ 注意事项

1. **向后兼容**: 修改后需要确保现有功能不受影响
2. **性能影响**: 增加验证逻辑可能略微增加延迟（<10ms）
3. **日志量**: 详细日志会增加日志文件大小，建议配置日志轮转
4. **测试覆盖**: 需要测试各种边界情况
   - 空响应
   - 超长响应
   - 网络中断
   - 上游 API 错误

---

## 📝 总结

**问题本质**: 续传机制在处理空响应或无效响应时，没有进行充分验证，导致构建了格式错误的续传请求。

**解决核心**:
1. ✅ 验证截断文本有效性
2. ✅ 智能决策是否续传
3. ✅ 提供降级策略
4. ✅ 增强日志和监控

**优先级**: 🔴 高 - 建议立即修复

**预计工作量**: 2-4 小时（包括测试）
