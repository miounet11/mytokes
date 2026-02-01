# Kiro 转换器集成指南

## 概述

本指南说明如何将 `kiro_converter.py` 集成到 `api_server.py` 中，实现正确的 Kiro API 工具调用。

## 核心问题回顾

### 当前问题

你的 `api_server.py` 使用**内联文本格式**处理工具调用：

```python
# 错误做法 (api_server.py:1227)
text_parts.append(f"[Calling tool: {tool_name}]\nInput: {input_str}")
```

这导致：
- ❌ Kiro API 无法识别工具调用
- ❌ 工具结果无法正确配对
- ❌ 历史消息交替验证失败

### 解决方案

使用 `kiro_converter.py` 直接转换为 Kiro 原生格式：

```python
# 正确做法
from kiro_converter import convert_anthropic_to_kiro

kiro_request = convert_anthropic_to_kiro(anthropic_body)
# 直接调用 Kiro API
```

---

## 集成步骤

### 步骤 1: 修改 API 端点

在 `api_server.py` 中修改 `/v1/messages` 端点：

```python
import httpx
from kiro_converter import convert_anthropic_to_kiro, convert_kiro_response_to_anthropic

# Kiro API 配置
KIRO_API_URL = "https://api.kiro.ai/v1/converse"  # Kiro 原生端点
KIRO_STREAM_URL = "https://api.kiro.ai/v1/converse-stream"  # 流式端点

@app.post("/v1/messages")
async def handle_anthropic_messages(request: Request):
    """处理 Anthropic /v1/messages 请求"""
    try:
        body = await request.json()
        request_id = f"req_{uuid.uuid4().hex[:12]}"

        logger.info(f"[{request_id}] 收到 Anthropic 请求")

        # 转换为 Kiro 格式
        kiro_request = convert_anthropic_to_kiro(body)

        logger.info(f"[{request_id}] 已转换为 Kiro 格式")
        logger.debug(f"[{request_id}] Kiro 请求: {json.dumps(kiro_request, ensure_ascii=False)[:500]}...")

        # 准备请求头
        headers = {
            "Authorization": f"Bearer {KIRO_API_KEY}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }

        # 判断是否流式
        is_stream = body.get("stream", False)

        if is_stream:
            return await handle_kiro_stream(kiro_request, headers, body, request_id)
        else:
            return await handle_kiro_non_stream(kiro_request, headers, body, request_id)

    except Exception as e:
        logger.error(f"[{request_id}] 请求处理失败: {e}")
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "type": "internal_error",
                    "message": str(e)
                }
            }
        )
```

### 步骤 2: 实现非流式处理

```python
async def handle_kiro_non_stream(
    kiro_request: dict,
    headers: dict,
    original_body: dict,
    request_id: str
) -> JSONResponse:
    """处理非流式 Kiro 请求"""

    async with httpx.AsyncClient(timeout=120.0) as client:
        try:
            response = await client.post(
                KIRO_API_URL,
                json=kiro_request,
                headers=headers
            )
            response.raise_for_status()

            kiro_response = response.json()
            logger.info(f"[{request_id}] Kiro 响应成功")

            # 解析 Kiro 响应
            output = kiro_response.get("output", {})
            message = output.get("message", {})

            # 提取内容
            content_blocks = []

            # 提取文本
            text = message.get("content", [{}])[0].get("text", "")
            if text:
                content_blocks.append({
                    "type": "text",
                    "text": text
                })

            # 提取工具调用
            tool_uses = message.get("toolUses", [])
            for tool_use in tool_uses:
                content_blocks.append({
                    "type": "tool_use",
                    "id": tool_use.get("toolUseId"),
                    "name": tool_use.get("name"),
                    "input": tool_use.get("input", {})
                })

            # 提取 token 使用情况
            usage = kiro_response.get("usage", {})
            input_tokens = usage.get("inputTokens", 0)
            output_tokens = usage.get("outputTokens", 0)

            # 构建 Anthropic 响应
            anthropic_response = {
                "id": f"msg_{uuid.uuid4().hex}",
                "type": "message",
                "role": "assistant",
                "content": content_blocks,
                "model": original_body.get("model", "claude-sonnet-4"),
                "stop_reason": kiro_response.get("stopReason", "end_turn"),
                "stop_sequence": None,
                "usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens
                }
            }

            return JSONResponse(content=anthropic_response)

        except httpx.HTTPStatusError as e:
            logger.error(f"[{request_id}] Kiro API 错误: {e.response.status_code}")
            logger.error(f"[{request_id}] 错误详情: {e.response.text}")

            return JSONResponse(
                status_code=e.response.status_code,
                content={
                    "error": {
                        "type": "api_error",
                        "message": e.response.text
                    }
                }
            )
        except Exception as e:
            logger.error(f"[{request_id}] 处理失败: {e}")
            return JSONResponse(
                status_code=500,
                content={
                    "error": {
                        "type": "internal_error",
                        "message": str(e)
                    }
                }
            )
```

### 步骤 3: 实现流式处理

```python
async def handle_kiro_stream(
    kiro_request: dict,
    headers: dict,
    original_body: dict,
    request_id: str
) -> StreamingResponse:
    """处理流式 Kiro 请求"""

    async def generate_stream():
        async with httpx.AsyncClient(timeout=120.0) as client:
            try:
                async with client.stream(
                    "POST",
                    KIRO_STREAM_URL,
                    json=kiro_request,
                    headers=headers
                ) as response:
                    response.raise_for_status()

                    # 发送初始事件
                    yield f"event: message_start\n"
                    yield f"data: {{\"type\":\"message_start\",\"message\":{{\"id\":\"msg_{uuid.uuid4().hex}\",\"type\":\"message\",\"role\":\"assistant\",\"content\":[],\"model\":\"{original_body.get('model', 'claude-sonnet-4')}\",\"stop_reason\":null,\"stop_sequence\":null,\"usage\":{{\"input_tokens\":0,\"output_tokens\":0}}}}}}\n\n"

                    # 发送内容开始
                    yield f"event: content_block_start\n"
                    yield f"data: {{\"type\":\"content_block_start\",\"index\":0,\"content_block\":{{\"type\":\"text\",\"text\":\"\"}}}}\n\n"

                    accumulated_text = ""
                    tool_uses = []

                    async for line in response.aiter_lines():
                        if not line or line.startswith(":"):
                            continue

                        if line.startswith("data: "):
                            data_str = line[6:]
                            try:
                                chunk = json.loads(data_str)

                                # 处理文本增量
                                if "contentBlockDelta" in chunk:
                                    delta = chunk["contentBlockDelta"].get("delta", {})
                                    if "text" in delta:
                                        text_delta = delta["text"]
                                        accumulated_text += text_delta

                                        # 发送文本增量
                                        yield f"event: content_block_delta\n"
                                        yield f"data: {{\"type\":\"content_block_delta\",\"index\":0,\"delta\":{{\"type\":\"text_delta\",\"text\":{json.dumps(text_delta)}}}}}\n\n"

                                # 处理工具调用
                                elif "toolUse" in chunk:
                                    tool_use = chunk["toolUse"]
                                    tool_uses.append(tool_use)

                                    # 发送工具调用块
                                    tool_index = len(tool_uses)
                                    yield f"event: content_block_start\n"
                                    yield f"data: {{\"type\":\"content_block_start\",\"index\":{tool_index},\"content_block\":{{\"type\":\"tool_use\",\"id\":\"{tool_use.get('toolUseId')}\",\"name\":\"{tool_use.get('name')}\",\"input\":{{}}}}}}\n\n"

                                    yield f"event: content_block_stop\n"
                                    yield f"data: {{\"type\":\"content_block_stop\",\"index\":{tool_index}}}\n\n"

                            except json.JSONDecodeError:
                                continue

                    # 发送内容结束
                    yield f"event: content_block_stop\n"
                    yield f"data: {{\"type\":\"content_block_stop\",\"index\":0}}\n\n"

                    # 发送消息结束
                    yield f"event: message_delta\n"
                    yield f"data: {{\"type\":\"message_delta\",\"delta\":{{\"stop_reason\":\"end_turn\",\"stop_sequence\":null}},\"usage\":{{\"output_tokens\":0}}}}\n\n"

                    yield f"event: message_stop\n"
                    yield f"data: {{\"type\":\"message_stop\"}}\n\n"

            except Exception as e:
                logger.error(f"[{request_id}] 流式处理失败: {e}")
                yield f"event: error\n"
                yield f"data: {{\"type\":\"error\",\"error\":{{\"type\":\"internal_error\",\"message\":\"{str(e)}\"}}}}\n\n"

    return StreamingResponse(
        generate_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )
```

---

## 关键改进点

### 1. 移除内联文本格式

**删除** `api_server.py` 中的这些代码：

```python
# 删除这些 (api_server.py:1220-1227)
if item_type == "tool_use":
    tool_name = item.get("name", "unknown")
    tool_input = item.get("input", {})
    input_str = json.dumps(tool_input, ensure_ascii=False)
    text_parts.append(f"[Calling tool: {tool_name}]\nInput: {input_str}")

# 删除这些 (api_server.py:1228-1259)
if item_type == "tool_result":
    # ... 内联文本处理
```

### 2. 保留结构化格式

使用 `kiro_converter.py` 保持工具调用的结构化信息：

```python
# ✓ 正确：保留结构
tool_uses = [
    {
        "toolUseId": "toolu_123",
        "name": "Read",
        "input": {"file_path": "/tmp/test.txt"}
    }
]

# ✗ 错误：转换为文本
text = "[Calling tool: Read]\nInput: {\"file_path\": \"/tmp/test.txt\"}"
```

### 3. 自动修复历史消息

`fix_history_alternation()` 会自动：
- 确保消息交替（user → assistant → user → assistant）
- 验证 toolUses 和 toolResults 配对
- 插入占位消息修复不连续的对话

---

## 测试验证

### 测试 1: 简单对话

```bash
curl -X POST http://localhost:8000/v1/messages \
  -H "Content-Type: application/json" \
  -H "x-api-key: your-key" \
  -d '{
    "model": "claude-sonnet-4",
    "max_tokens": 1024,
    "messages": [
      {"role": "user", "content": "Hello!"}
    ]
  }'
```

### 测试 2: 工具调用

```bash
curl -X POST http://localhost:8000/v1/messages \
  -H "Content-Type: application/json" \
  -H "x-api-key: your-key" \
  -d '{
    "model": "claude-opus-4",
    "max_tokens": 2048,
    "messages": [
      {"role": "user", "content": "Read /tmp/test.txt"}
    ],
    "tools": [
      {
        "name": "Read",
        "description": "Read a file",
        "input_schema": {
          "type": "object",
          "properties": {
            "file_path": {"type": "string"}
          },
          "required": ["file_path"]
        }
      }
    ]
  }'
```

### 测试 3: 流式响应

```bash
curl -X POST http://localhost:8000/v1/messages \
  -H "Content-Type: application/json" \
  -H "x-api-key: your-key" \
  -d '{
    "model": "claude-sonnet-4",
    "max_tokens": 1024,
    "messages": [
      {"role": "user", "content": "Tell me a story"}
    ],
    "stream": true
  }'
```

---

## 常见问题

### Q1: 为什么不能使用 Kiro 网关的 OpenAI 兼容层？

**A:** Kiro 网关的 OpenAI 兼容层有限制：
- 不完全支持 Anthropic 的工具调用格式
- 缺少历史消息交替验证
- 工具结果配对可能失败

直接调用 Kiro 原生 API 可以完全控制请求格式，避免这些问题。

### Q2: 如何处理长对话历史？

**A:** `kiro_converter.py` 会自动处理，但你可以在调用前截断：

```python
# 在 api_server.py 中
if len(body["messages"]) > 50:
    body["messages"] = body["messages"][-50:]  # 只保留最近 50 条
```

### Q3: 如何调试转换问题？

**A:** 启用详细日志：

```python
import logging
logging.basicConfig(level=logging.DEBUG)

# 在转换后打印
logger.debug(f"Kiro 请求: {json.dumps(kiro_request, indent=2, ensure_ascii=False)}")
```

---

## 下一步

1. **备份现有代码**：
   ```bash
   cp api_server.py api_server.py.backup
   ```

2. **集成转换器**：按照上述步骤修改 `api_server.py`

3. **运行测试**：
   ```bash
   python3 test_kiro_converter.py
   ```

4. **启动服务器**：
   ```bash
   python3 api_server.py
   ```

5. **测试端到端**：使用上述 curl 命令测试

---

## 总结

通过使用 `kiro_converter.py`，你可以：

✅ **正确处理工具调用**：保留结构化格式
✅ **自动修复历史消息**：确保交替和配对
✅ **完全兼容 Kiro API**：直接使用原生格式
✅ **简化代码逻辑**：移除复杂的内联文本解析

这将彻底解决你当前遇到的工具调用问题。
