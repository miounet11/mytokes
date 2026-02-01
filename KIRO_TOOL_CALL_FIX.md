# Kiro API 工具调用格式修复方案

## 问题诊断

### 当前架构问题

你的 `api_server.py` 使用了**内联文本格式**来处理工具调用：

```python
# 当前做法 (api_server.py:1227)
if item_type == "tool_use":
    tool_name = item.get("name", "unknown")
    tool_input = item.get("input", {})
    input_str = json.dumps(tool_input, ensure_ascii=False)
    text_parts.append(f"[Calling tool: {tool_name}]\nInput: {input_str}")
```

这会生成类似这样的文本：
```
[Calling tool: Read]
Input: {"file_path": "/path/to/file"}
```

**但 Kiro API 需要的是结构化格式**（参考 KiroProxy 的 `converters.py:99-142`）：

```python
# Kiro API 期望的格式
{
    "assistantResponseMessage": {
        "content": "Let me read that file.",
        "toolUses": [
            {
                "toolUseId": "toolu_xxx",
                "name": "Read",
                "input": {"file_path": "/path/to/file"}
            }
        ]
    }
}
```

### 核心问题

1. **消息格式不匹配**：你将 Anthropic 格式转换为 OpenAI 格式，但 Kiro 网关期望的是 **Kiro 原生格式**（类似 Anthropic 但有差异）

2. **工具调用丢失结构**：内联文本格式会导致：
   - Kiro API 无法识别工具调用
   - 工具结果无法正确配对
   - 历史消息交替验证失败

3. **缺少历史消息修复**：KiroProxy 有 `fix_history_alternation()` 函数（converters.py:145-250）来确保：
   - 消息必须 user → assistant → user → assistant 交替
   - toolUses 必须与 toolResults 配对
   - 你的代码缺少这个关键逻辑

---

## 解决方案

### 方案 A：直接调用 Kiro API（推荐）

**不经过 Kiro 网关的 OpenAI 兼容层**，直接调用 Kiro 原生 API。

#### 优点
- 完全控制请求格式
- 避免多次格式转换
- 性能更好

#### 实现步骤

1. **创建 Kiro 格式转换器**（参考 KiroProxy 的 `converters.py`）：

```python
# kiro_converter.py

def convert_anthropic_to_kiro(anthropic_body: dict) -> dict:
    """将 Anthropic 请求转换为 Kiro 原生格式"""
    messages = anthropic_body.get("messages", [])
    system = anthropic_body.get("system", "")
    tools = anthropic_body.get("tools", [])

    # 提取最后一条用户消息
    user_content = ""
    history = []

    for i, msg in enumerate(messages):
        role = msg.get("role")
        content = msg.get("content", "")

        if i == len(messages) - 1 and role == "user":
            # 最后一条用户消息
            user_content = extract_text_content(content)
        else:
            # 历史消息
            if role == "user":
                history.append({
                    "userInputMessage": {
                        "content": extract_text_content(content)
                    }
                })
            elif role == "assistant":
                # 解析工具调用
                text_content, tool_uses = parse_assistant_content(content)

                assistant_msg = {
                    "assistantResponseMessage": {
                        "content": text_content
                    }
                }

                if tool_uses:
                    assistant_msg["assistantResponseMessage"]["toolUses"] = tool_uses

                history.append(assistant_msg)

    # 修复历史消息交替
    history = fix_history_alternation(history)

    # 转换工具定义
    kiro_tools = None
    if tools:
        kiro_tools = []
        for tool in tools:
            kiro_tools.append({
                "toolSpecification": {
                    "name": tool.get("name"),
                    "description": tool.get("description", "")[:500],
                    "inputSchema": {
                        "json": tool.get("input_schema", {})
                    }
                }
            })

    # 构建 Kiro 请求
    kiro_request = {
        "conversationState": {
            "currentMessage": {
                "userInputMessage": {
                    "content": user_content
                }
            },
            "history": history
        },
        "modelId": map_model_name(anthropic_body.get("model", "claude-sonnet-4")),
        "inferenceConfig": {
            "maxTokens": anthropic_body.get("max_tokens", 8192),
            "temperature": anthropic_body.get("temperature", 1.0),
            "topP": anthropic_body.get("top_p", 1.0)
        }
    }

    if kiro_tools:
        kiro_request["conversationState"]["currentMessage"]["userInputMessage"]["toolConfig"] = {
            "tools": kiro_tools
        }

    return kiro_request


def fix_history_alternation(history: list) -> list:
    """修复历史消息交替和工具调用配对

    关键规则：
    1. 消息必须 user → assistant → user → assistant 交替
    2. 如果 assistant 有 toolUses，下一条 user 必须有 toolResults
    3. 如果 assistant 没有 toolUses，下一条 user 不能有 toolResults
    """
    if not history:
        return []

    fixed = []

    for i, msg in enumerate(history):
        is_user = "userInputMessage" in msg
        is_assistant = "assistantResponseMessage" in msg

        # 检查交替
        if fixed:
            last_is_user = "userInputMessage" in fixed[-1]

            if is_user and last_is_user:
                # 连续两条 user，插入占位 assistant
                fixed.append({
                    "assistantResponseMessage": {
                        "content": "I understand."
                    }
                })
            elif is_assistant and not last_is_user:
                # 连续两条 assistant，插入占位 user
                fixed.append({
                    "userInputMessage": {
                        "content": "Please continue."
                    }
                })

        fixed.append(msg)

    # 验证 toolUses/toolResults 配对
    for i in range(len(fixed) - 1):
        current = fixed[i]
        next_msg = fixed[i + 1]

        if "assistantResponseMessage" in current:
            assistant = current["assistantResponseMessage"]
            has_tool_uses = "toolUses" in assistant and assistant["toolUses"]

            if "userInputMessage" in next_msg:
                user = next_msg["userInputMessage"]
                has_tool_results = "userInputMessageContext" in user and "toolResults" in user["userInputMessageContext"]

                # 修复不配对的情况
                if has_tool_uses and not has_tool_results:
                    # assistant 有 toolUses 但 user 没有 toolResults
                    # 清除 toolUses（避免 Kiro API 报错）
                    assistant.pop("toolUses", None)
                elif not has_tool_uses and has_tool_results:
                    # assistant 没有 toolUses 但 user 有 toolResults
                    # 清除 toolResults
                    user.pop("userInputMessageContext", None)

    return fixed


def parse_assistant_content(content) -> tuple[str, list]:
    """解析 assistant 消息，提取文本和工具调用

    Returns:
        (text_content, tool_uses)
    """
    if isinstance(content, str):
        return content, []

    if isinstance(content, list):
        text_parts = []
        tool_uses = []

        for item in content:
            if isinstance(item, dict):
                item_type = item.get("type")

                if item_type == "text":
                    text_parts.append(item.get("text", ""))
                elif item_type == "tool_use":
                    tool_uses.append({
                        "toolUseId": item.get("id", f"toolu_{uuid.uuid4().hex[:12]}"),
                        "name": item.get("name"),
                        "input": item.get("input", {})
                    })

        return "\n".join(text_parts), tool_uses

    return str(content), []
```

2. **修改 API 端点直接调用 Kiro API**：

```python
# api_server.py

@app.post("/v1/messages")
async def handle_anthropic_messages(request: Request):
    """处理 Anthropic /v1/messages 请求"""
    body = await request.json()

    # 转换为 Kiro 格式
    kiro_request = convert_anthropic_to_kiro(body)

    # 直接调用 Kiro API（不经过网关）
    KIRO_API_URL = "https://api.kiro.ai/v1/converse"  # Kiro 原生端点

    headers = {
        "Authorization": f"Bearer {KIRO_API_KEY}",
        "Content-Type": "application/json"
    }

    if body.get("stream", False):
        return await handle_kiro_stream(kiro_request, headers, body)
    else:
        return await handle_kiro_non_stream(kiro_request, headers, body)
```

---

### 方案 B：修复现有 OpenAI 格式转换（次选）

如果必须使用 Kiro 网关的 OpenAI 兼容层，需要：

1. **保留工具调用的结构化信息**（不要转换为内联文本）
2. **在发送前验证消息交替**
3. **处理工具结果的配对**

但这个方案有局限性，因为 Kiro 网关的 OpenAI 兼容层可能不完全支持所有 Anthropic 特性。

---

## 推荐行动

1. **立即采用方案 A**：直接调用 Kiro API
2. **复用 KiroProxy 的转换逻辑**：
   - 复制 `converters.py` 中的关键函数
   - 复制 `fix_history_alternation()` 逻辑
3. **移除内联文本格式**：不再使用 `[Calling tool: xxx]` 格式
4. **添加历史消息验证**：确保交替和配对正确

---

## 测试验证

修复后，测试以下场景：

1. **单次工具调用**：
   ```json
   {
     "messages": [
       {"role": "user", "content": "Read /tmp/test.txt"},
       {"role": "assistant", "content": [{"type": "tool_use", "name": "Read", "input": {...}}]},
       {"role": "user", "content": [{"type": "tool_result", "content": "..."}]}
     ]
   }
   ```

2. **多次工具调用**：验证历史消息交替

3. **工具调用 + 文本混合**：验证内容提取

4. **长对话历史**：验证截断和摘要

---

## 参考文件

- KiroProxy 转换逻辑：`/tmp/KiroProxy/kiro_proxy/converters.py`
- KiroProxy 历史修复：`converters.py:145-250`
- Kiro API 处理：`/tmp/KiroProxy/kiro_proxy/handlers/anthropic.py`
