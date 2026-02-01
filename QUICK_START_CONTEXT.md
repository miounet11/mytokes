# 快速开始：用户输入预处理增强

## 5 分钟快速上手

### 1. 功能已自动启用

无需任何配置，功能已集成到 API 服务器中。

### 2. 工作原理

```
用户输入简短消息 → 系统自动添加项目上下文 → AI 获得更准确的理解
```

### 3. 实际效果对比

#### 没有上下文增强

```
用户: "优化这个函数"
AI: "请提供函数代码，我需要知道是什么编程语言..."
```

#### 有上下文增强

```
用户: "优化这个函数"
系统增强: "<project_context>{language: Python, framework: FastAPI}</project_context> 优化这个函数"
AI: "好的，我会用 FastAPI 的异步最佳实践来优化这个函数..."
```

### 4. 何时触发

✅ **会触发增强**：
- "优化函数" (8 字符)
- "添加测试" (4 字符)
- "修复 bug" (7 字符)
- "写个接口" (4 字符)

❌ **不会触发增强**：
- "请帮我优化这个用户登录函数的性能" (16 字符，但已足够清晰)
- 包含代码块的消息
- 详细的需求描述

### 5. 查看日志

```bash
tail -f logs/api_server.log | grep "上下文"
```

你会看到：
```
[session-001] 🔄 触发上下文提取（用户消息数: 5）
[abc123] ✅ 上下文提取成功: 121 chars
[session-001] 🎯 上下文增强完成: 8 -> 207 chars
```

### 6. 测试功能

```bash
python3 test_context_enhancement.py
```

预期输出：
```
✅ 所有测试完成
```

### 7. 调整配置（可选）

编辑 `api_server.py`：

```python
# 修改触发频率（默认每 5 条消息）
if user_message_count % 10 == 0:  # 改为每 10 条

# 修改长度阈值（默认 20 字符）
if len(user_content) < 30:  # 改为 30 字符
```

### 8. 常见问题

**Q: 会增加延迟吗？**
A: 几乎不会。上下文提取有缓存，只在必要时触发。

**Q: 会增加 token 消耗吗？**
A: 会略微增加（约 50-100 tokens），但换来的是更准确的回答，减少来回澄清。

**Q: 可以禁用吗？**
A: 可以。在 `api_server.py` 中注释掉相关代码即可。

**Q: 支持哪些语言？**
A: 所有编程语言，系统会自动识别。

### 9. 监控指标

关键指标：
- 上下文提取成功率
- 平均上下文长度
- 增强消息比例
- 缓存命中率

查看统计：
```bash
grep "上下文" logs/api_server.log | wc -l  # 总提取次数
grep "增强完成" logs/api_server.log | wc -l  # 总增强次数
```

### 10. 下一步

- 📖 阅读完整文档：`CONTEXT_ENHANCEMENT.md`
- 🧪 运行测试：`python3 test_context_enhancement.py`
- 📊 查看日志：`tail -f logs/api_server.log`
- 🔧 自定义配置：编辑 `api_server.py`

## 需要帮助？

查看详细文档：`CONTEXT_ENHANCEMENT.md`
