# Kiro 工具调用修复 - 快速参考

## 🎯 核心问题

```python
# ❌ 错误：api_server.py:1227
text_parts.append(f"[Calling tool: {tool_name}]\nInput: {input_str}")
```

**问题**：将结构化工具调用转换为文本，导致 Kiro API 无法识别。

---

## ✅ 解决方案

使用 `kiro_converter.py` 直接转换为 Kiro 原生格式。

---

## 📦 已创建的文件

| 文件 | 用途 |
|------|------|
| `kiro_converter.py` | 核心转换器（Anthropic → Kiro） |
| `test_kiro_converter.py` | 测试套件（✅ 全部通过） |
| `INTEGRATION_GUIDE.md` | 详细集成步骤 |
| `KIRO_TOOL_CALL_FIX.md` | 技术分析文档 |
| `SOLUTION_SUMMARY.md` | 完整解决方案总结 |
| `QUICK_REFERENCE.md` | 本文件（快速参考） |

---

## 🚀 快速集成（3 步）

### 1. 导入转换器

```python
# api_server.py 顶部添加
from kiro_converter import convert_anthropic_to_kiro
```

### 2. 修改端点

```python
@app.post("/v1/messages")
async def handle_anthropic_messages(request: Request):
    body = await request.json()

    # 转换为 Kiro 格式
    kiro_request = convert_anthropic_to_kiro(body)

    # 调用 Kiro API
    KIRO_API_URL = "https://api.kiro.ai/v1/converse"
    headers = {"Authorization": f"Bearer {KIRO_API_KEY}"}

    # 发送请求并处理响应...
```

### 3. 删除旧代码

删除 `api_server.py` 中的内联文本格式代码（行 1220-1259）。

---

## 🧪 测试

```bash
# 运行单元测试
python3 test_kiro_converter.py

# 启动服务器
python3 api_server.py

# 测试工具调用
curl -X POST http://localhost:8000/v1/messages \
  -H "Content-Type: application/json" \
  -H "x-api-key: your-key" \
  -d '{
    "model": "claude-opus-4",
    "max_tokens": 2048,
    "messages": [{"role": "user", "content": "Read /tmp/test.txt"}],
    "tools": [{"name": "Read", "description": "Read file", "input_schema": {...}}]
  }'
```

---

## 📋 关键函数

### `convert_anthropic_to_kiro(body)`

将 Anthropic 请求转换为 Kiro 格式。

**输入**：
```python
{
  "model": "claude-opus-4",
  "messages": [...],
  "tools": [...]
}
```

**输出**：
```python
{
  "conversationState": {
    "currentMessage": {...},
    "history": [...]
  },
  "modelId": "claude-opus-4",
  "inferenceConfig": {...}
}
```

### `fix_history_alternation(history)`

修复历史消息交替和工具配对。

**自动处理**：
- 连续相同角色 → 插入占位消息
- toolUses 无 toolResults → 清除 toolUses
- 无 toolUses 有 toolResults → 清除 toolResults
- 确保以 assistant 结尾

---

## 🔍 格式对比

### 错误格式（修复前）

```json
{
  "role": "assistant",
  "content": "[Calling tool: Read]\nInput: {\"file_path\": \"/tmp/test.txt\"}"
}
```

### 正确格式（修复后）

```json
{
  "assistantResponseMessage": {
    "content": "Let me read that file.",
    "toolUses": [
      {
        "toolUseId": "toolu_123",
        "name": "Read",
        "input": {"file_path": "/tmp/test.txt"}
      }
    ]
  }
}
```

---

## 🎨 API 端点

| 端点 | 用途 |
|------|------|
| `https://api.kiro.ai/v1/converse` | 非流式请求 |
| `https://api.kiro.ai/v1/converse-stream` | 流式请求 |

---

## 📊 测试结果

```
✓ 测试 1: 简单消息
✓ 测试 2: 工具调用
✓ 测试 3: 历史消息交替修复（4 种场景）
✓ 测试 4: Assistant 内容解析
✓ 测试 5: User 工具结果解析
✓ 测试 6: 复杂对话

所有测试通过！
```

---

## 💡 关键要点

1. **不要使用内联文本格式**：保留结构化的工具调用
2. **直接调用 Kiro 原生 API**：绕过 OpenAI 兼容层
3. **自动修复历史消息**：使用 `fix_history_alternation()`
4. **验证工具配对**：确保 toolUses 和 toolResults 匹配

---

## 📚 详细文档

- **集成步骤**：阅读 `INTEGRATION_GUIDE.md`
- **技术分析**：阅读 `KIRO_TOOL_CALL_FIX.md`
- **完整方案**：阅读 `SOLUTION_SUMMARY.md`

---

## ✅ 验证清单

- [ ] 导入 `kiro_converter.py`
- [ ] 修改 `/v1/messages` 端点
- [ ] 删除内联文本格式代码
- [ ] 运行 `test_kiro_converter.py`
- [ ] 测试简单对话
- [ ] 测试工具调用
- [ ] 测试流式响应
- [ ] 检查日志输出

---

## 🆘 故障排查

### 问题：工具调用仍然不工作

**检查**：
1. 是否正确导入 `kiro_converter.py`？
2. 是否删除了旧的内联文本格式代码？
3. 是否使用了正确的 Kiro API 端点？
4. 查看日志中的 Kiro 请求格式

### 问题：历史消息验证失败

**检查**：
1. 是否调用了 `fix_history_alternation()`？
2. 查看日志中的警告信息
3. 运行 `test_kiro_converter.py` 测试 3

### 问题：流式响应异常

**检查**：
1. 是否使用了正确的流式端点？
2. 是否正确处理了 SSE 事件？
3. 查看 `INTEGRATION_GUIDE.md` 中的流式实现

---

## 🎉 预期结果

修复后，你应该看到：

✅ Kiro API 正确识别工具调用
✅ 工具被执行并返回结果
✅ 历史消息交替正确
✅ 多轮对话正常工作
✅ 流式响应正常

---

**这是一个完整、经过测试、可直接使用的解决方案！**
