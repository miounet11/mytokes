import re
import json
import uuid
import asyncio
import logging
from typing import AsyncIterator
import httpx
from fastapi.responses import StreamingResponse, JSONResponse

from app.core.config import (
    KIRO_PROXY_URL, STREAM_TEXT_CHUNK_SIZE, STREAM_TOOL_JSON_CHUNK_SIZE,
    ASYNC_SUMMARY_CONFIG, logger
)
from app.utils.token_utils import estimate_tokens, estimate_messages_tokens
from app.services.converter import (
    parse_inline_tool_blocks, expand_thinking_blocks,
    iter_text_chunks, convert_openai_to_anthropic
)
from app.utils.hallucination_detection import detect_hallucinated_tool_result

# 获取全局 HTTP 客户端的函数（将在 main 中注入或从专门的 service 导入）
_http_client_getter = None

def set_http_client_getter(getter):
    global _http_client_getter
    _http_client_getter = getter

def get_http_client():
    if _http_client_getter:
        return _http_client_getter()
    raise RuntimeError("HTTP client getter not set")

async def handle_anthropic_stream_via_openai(
    openai_body: dict,
    headers: dict,
    request_id: str,
    model: str,
    cache_info: dict = None,
) -> StreamingResponse:
    """处理 Anthropic 流式请求 - 真正的流式传输（高并发优化）"""
    cache_info = cache_info or {"hit": False, "original_tokens": 0, "cached_tokens": 0, "saved_tokens": 0}

    # 预估输入 token 数
    estimated_input_tokens = estimate_messages_tokens(
        openai_body.get("messages", []),
        openai_body.get("system", "")
    )

    # 缓存计费模拟
    cache_read_tokens = 0
    if cache_info.get("hit") and ASYNC_SUMMARY_CONFIG.get("simulate_cache_billing", True):
        saved_tokens = cache_info.get("saved_tokens", 0)
        if saved_tokens > 0:
            cache_read_tokens = saved_tokens
            logger.info(f"[{request_id}] 💰 缓存计费: cache_read={cache_read_tokens}")

    async def generate() -> AsyncIterator[bytes]:
        # 状态变量
        block_index = 0
        text_block_started = False
        output_tokens = 0
        finish_reason = "end_turn"
        accumulated_text = ""  # 累积的文本内容
        buffered_text = ""     # 缓冲的文本（检测到内联工具调用时使用）
        buffering_mode = False # 是否处于缓冲模式

        # 工具调用累积器（原生 tool_calls）
        tool_call_acc: dict[str, dict] = {}

        try:
            # 发送 Anthropic message_start
            actual_input_tokens = max(0, estimated_input_tokens - cache_read_tokens)
            msg_start = {
                "type": "message_start",
                "message": {
                    "id": f"msg_{request_id}",
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": model,
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {
                        "input_tokens": actual_input_tokens,
                        "output_tokens": 0,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": cache_read_tokens,
                    }
                }
            }
            yield f"data: {json.dumps(msg_start)}\n\n".encode()

            # 真正的流式请求
            client = get_http_client()
            async with client.stream(
                "POST",
                KIRO_PROXY_URL,
                json=openai_body,
                headers=headers,
            ) as response:
                if response.status_code != 200:
                    error_text = await response.aread()
                    error_str = error_text.decode()[:500]
                    logger.error(f"[{request_id}] API Error {response.status_code}: {error_str[:200]}")

                    yield f'data: {{"type":"content_block_start","index":0,"content_block":{{"type":"text","text":""}}}}\n\n'.encode()
                    error_msg = f"[API Error {response.status_code}] {error_str[:200]}"
                    yield f'data: {{"type":"content_block_delta","index":0,"delta":{{"type":"text_delta","text":{json.dumps(error_msg)}}}}}\n\n'.encode()
                    yield f'data: {{"type":"content_block_stop","index":0}}\n\n'.encode()
                    yield f'data: {{"type":"message_delta","delta":{{"stop_reason":"end_turn","stop_sequence":null}},"usage":{{"output_tokens":10}}}}\n\n'.encode()
                    yield f'data: {{"type":"message_stop"}}\n\n'.encode()
                    return

                buffer = ""
                async for chunk in response.aiter_text():
                    buffer += chunk
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        line = line.strip()
                        if not line or not line.startswith("data:"):
                            continue

                        data_str = line[5:].strip()
                        if data_str == "[DONE]":
                            continue

                        try:
                            data = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue

                        usage = data.get("usage")
                        if usage:
                            output_tokens = usage.get("completion_tokens", output_tokens)

                        choice = data.get("choices", [{}])[0]
                        delta = choice.get("delta", {})
                        fr = choice.get("finish_reason")

                        if fr:
                            if fr == "tool_calls":
                                finish_reason = "tool_use"
                            elif fr in ("stop", "length"):
                                finish_reason = "end_turn"

                        content = delta.get("content", "")
                        if content:
                            if not buffering_mode:
                                temp_text = accumulated_text + content
                                if "[Calling tool:" in temp_text:
                                    buffering_mode = True
                                    start_idx = temp_text.find("[Calling tool:")
                                    buffered_text = temp_text[start_idx:]
                                    logger.info(f"[{request_id}] 检测到内联工具调用，切换到缓冲模式")
                                    if text_block_started:
                                        yield f'data: {{"type":"content_block_stop","index":{block_index}}}\n\n'.encode()
                                        block_index += 1
                                        text_block_started = False
                                    accumulated_text += content
                                    continue
                                
                                accumulated_text += content
                                if not text_block_started:
                                    yield f'data: {{"type":"content_block_start","index":{block_index},"content_block":{{"type":"text","text":""}}}}\n\n'.encode()
                                    text_block_started = True

                                delta_event = {
                                    "type": "content_block_delta",
                                    "index": block_index,
                                    "delta": {"type": "text_delta", "text": content}
                                }
                                yield f"data: {json.dumps(delta_event)}\n\n".encode()
                            else:
                                accumulated_text += content
                                buffered_text += content

                        delta_tool_calls = delta.get("tool_calls", []) or []
                        for tc in delta_tool_calls:
                            index = tc.get("index", 0)
                            call_id = tc.get("id")
                            key = call_id or f"index_{index}"
                            if key not in tool_call_acc:
                                tool_call_acc[key] = {
                                    "id": call_id or f"toolu_{uuid.uuid4().hex[:12]}",
                                    "name": None,
                                    "arguments": "",
                                }
                            entry = tool_call_acc[key]
                            if call_id: entry["id"] = call_id
                            func = tc.get("function", {}) or {}
                            if func.get("name"): entry["name"] = func.get("name")
                            if func.get("arguments"): entry["arguments"] += func.get("arguments")

            # 流结束处理
            if buffering_mode:
                # 幻觉检测
                has_hallucination, cleaned_text, reason = detect_hallucinated_tool_result(buffered_text, request_id)
                if has_hallucination:
                    logger.warning(f"[{request_id}] 缓冲模式检测到幻觉，清理后解析: {reason}")
                    buffered_text = cleaned_text

                # 缓冲模式：解析内联工具调用
                logger.info(f"[{request_id}] 解析缓冲的内联工具调用，长度={len(buffered_text)}")
                blocks = parse_inline_tool_blocks(buffered_text)
                blocks = expand_thinking_blocks(blocks)
                for block in blocks:
                    if block.get("type") == "text":
                        text_value = block.get("text", "")
                        if text_value and text_value.strip():
                            yield f'data: {{"type":"content_block_start","index":{block_index},"content_block":{{"type":"text","text":""}}}}\n\n'.encode()
                            for text_chunk in iter_text_chunks(text_value, STREAM_TEXT_CHUNK_SIZE):
                                delta_event = {"type": "content_block_delta", "index": block_index, "delta": {"type": "text_delta", "text": text_chunk}}
                                yield f"data: {json.dumps(delta_event)}\n\n".encode()
                            yield f'data: {{"type":"content_block_stop","index":{block_index}}}\n\n'.encode()
                            block_index += 1
                    elif block.get("type") == "tool_use":
                        finish_reason = "tool_use"
                        tool_start = {"type": "content_block_start", "index": block_index, "content_block": {"type": "tool_use", "id": block["id"], "name": block["name"], "input": {}}}
                        yield f"data: {json.dumps(tool_start)}\n\n".encode()
                        tool_json = json.dumps(block.get("input", {}), ensure_ascii=False)
                        for tool_chunk in iter_text_chunks(tool_json, STREAM_TOOL_JSON_CHUNK_SIZE):
                            delta_event = {"type": "content_block_delta", "index": block_index, "delta": {"type": "input_json_delta", "partial_json": tool_chunk}}
                            yield f"data: {json.dumps(delta_event)}\n\n".encode()
                        yield f'data: {{"type":"content_block_stop","index":{block_index}}}\n\n'.encode()
                        block_index += 1
            else:
                if text_block_started:
                    yield f'data: {{"type":"content_block_stop","index":{block_index}}}\n\n'.encode()
                    block_index += 1
                for key, entry in tool_call_acc.items():
                    if entry["name"]:
                        finish_reason = "tool_use"
                        try: tool_input = json.loads(entry["arguments"]) if entry["arguments"] else {}
                        except json.JSONDecodeError: tool_input = {"_raw": entry["arguments"][:2000], "_parse_error": "Invalid JSON"}
                        tool_start = {"type": "content_block_start", "index": block_index, "content_block": {"type": "tool_use", "id": entry["id"], "name": entry["name"], "input": {}}}
                        yield f"data: {json.dumps(tool_start)}\n\n".encode()
                        tool_json = json.dumps(tool_input, ensure_ascii=False)
                        for tool_chunk in iter_text_chunks(tool_json, STREAM_TOOL_JSON_CHUNK_SIZE):
                            delta_event = {"type": "content_block_delta", "index": block_index, "delta": {"type": "input_json_delta", "partial_json": tool_chunk}}
                            yield f"data: {json.dumps(delta_event)}\n\n".encode()
                        yield f'data: {{"type":"content_block_stop","index":{block_index}}}\n\n'.encode()
                        block_index += 1

            if block_index == 0:
                yield f'data: {{"type":"content_block_start","index":0,"content_block":{{"type":"text","text":""}}}}\n\n'.encode()
                yield f'data: {{"type":"content_block_stop","index":0}}\n\n'.encode()

            if output_tokens == 0: output_tokens = estimate_tokens(accumulated_text)
            yield f'data: {{"type":"message_delta","delta":{{"stop_reason":"{finish_reason}","stop_sequence":null}},"usage":{{"output_tokens":{output_tokens},"cache_creation_input_tokens":0,"cache_read_input_tokens":{cache_read_tokens}}}}}\n\n'.encode()
            yield f'data: {{"type":"message_stop"}}\n\n'.encode()
            logger.info(f"[{request_id}] ✅ 流式完成: text_len={len(accumulated_text)}, buffered={buffering_mode}")

        except Exception as e:
            logger.error(f"[{request_id}] 异常: {type(e).__name__}: {e}")
            yield f'data: {{"type":"error","error":{{"type":"api_error","message":{json.dumps(str(e))}}}}}\n\n'.encode()

    return StreamingResponse(generate(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"})

async def handle_anthropic_non_stream_via_openai(
    openai_body: dict,
    headers: dict,
    request_id: str,
    model: str,
    cache_info: dict = None,
) -> JSONResponse:
    """处理 Anthropic 非流式请求"""
    cache_info = cache_info or {"hit": False, "original_tokens": 0, "cached_tokens": 0, "saved_tokens": 0}
    try:
        client = get_http_client()
        response = await client.post(KIRO_PROXY_URL, json=openai_body, headers=headers)
        if response.status_code != 200:
            error_str = response.text
            logger.error(f"[{request_id}] OpenAI API Error {response.status_code}: {error_str[:200]}")
            return JSONResponse(status_code=response.status_code, content={"type": "error", "error": {"type": "api_error", "message": error_str[:500]}})
        
        openai_response = response.json()
        anthropic_response = convert_openai_to_anthropic(openai_response, model, request_id)
        if cache_info.get("hit") and ASYNC_SUMMARY_CONFIG.get("simulate_cache_billing", True):
            saved_tokens = cache_info.get("saved_tokens", 0)
            if saved_tokens > 0 and "usage" in anthropic_response:
                original_input = anthropic_response["usage"].get("input_tokens", 0)
                anthropic_response["usage"]["cache_read_input_tokens"] = saved_tokens
                anthropic_response["usage"]["input_tokens"] = max(0, original_input - saved_tokens)
        return JSONResponse(content=anthropic_response)
    except Exception as e:
        logger.error(f"[{request_id}] 请求异常: {e}")
        return JSONResponse(status_code=500, content={"type": "error", "error": {"type": "api_error", "message": str(e)}})
