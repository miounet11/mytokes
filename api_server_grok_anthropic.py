#!/usr/bin/env python3
"""
Grok to Anthropic API Proxy Server

This server provides an Anthropic-compatible API that proxies requests to Grok.
It converts between Anthropic's message format and OpenAI's chat completion format.

Usage:
    export ANTHROPIC_BASE_URL=http://127.0.0.1:8300
    export ANTHROPIC_API_KEY=any-value
    claude
"""

import json
import uuid
import logging
import os
from typing import Optional, Dict, Any, List, AsyncGenerator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware

# ============================================================================
# Logging Configuration
# ============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# ============================================================================
# Configuration
# ============================================================================

# Grok API Configuration
GROK_API_BASE = os.getenv("GROK_API_BASE", "http://127.0.0.1:8000/grok/v1")
GROK_API_KEY = os.getenv("GROK_API_KEY", "dba22273-65d3-4dc1-8ce9-182f680b2bf5")

# Model Mapping: Anthropic model names -> Grok model names
MODEL_MAPPING = {
    # Claude to Grok mapping
    "claude-opus-4-5-20251101": "grok-4-1-thinking-1129",
    "claude-sonnet-4-5-20250929": "grok-4",
    "claude-3-5-sonnet-20241022": "grok-4",
    "claude-3-opus-20240229": "grok-4-1-thinking-1129",
    "claude-3-sonnet-20240229": "grok-4",
    "claude-3-haiku-20240307": "auto",
    "claude-haiku-4-5-20251001": "auto",
    # Direct Grok model names (passthrough)
    "grok-4": "grok-4",
    "grok-4-1-thinking-1129": "grok-4-1-thinking-1129",
    "grok-3-beta": "grok-3-beta",
    "auto": "auto",
}

DEFAULT_GROK_MODEL = "grok-4"

# HTTP Client Configuration
HTTP_TIMEOUT = 300.0

# ============================================================================
# Application Setup
# ============================================================================

http_client: Optional[httpx.AsyncClient] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifespan."""
    global http_client
    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(HTTP_TIMEOUT, connect=30.0),
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
    )
    logger.info("🚀 Grok-Anthropic Proxy started")
    yield
    if http_client:
        await http_client.aclose()
    logger.info("👋 Grok-Anthropic Proxy stopped")


app = FastAPI(
    title="Grok-Anthropic Proxy",
    description="Anthropic API compatible proxy for Grok",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================================================
# Helper Functions
# ============================================================================


def get_grok_model(anthropic_model: str) -> str:
    """Map Anthropic model name to Grok model name."""
    return MODEL_MAPPING.get(anthropic_model, DEFAULT_GROK_MODEL)


def generate_message_id() -> str:
    """Generate a unique message ID."""
    return f"msg_{uuid.uuid4().hex[:24]}"


# ============================================================================
# Format Conversion: Anthropic -> OpenAI (Grok)
# ============================================================================


def convert_anthropic_to_openai(anthropic_request: Dict[str, Any]) -> Dict[str, Any]:
    """
    Convert Anthropic message format to OpenAI chat completion format.

    Anthropic format:
    {
        "model": "claude-...",
        "max_tokens": 1024,
        "system": "You are...",
        "messages": [
            {"role": "user", "content": "Hello"}
        ]
    }

    OpenAI format:
    {
        "model": "grok-...",
        "max_tokens": 1024,
        "messages": [
            {"role": "system", "content": "You are..."},
            {"role": "user", "content": "Hello"}
        ]
    }
    """
    openai_messages = []

    # Handle system prompt
    system_content = anthropic_request.get("system", "")
    if system_content:
        if isinstance(system_content, list):
            # System can be a list of content blocks
            text_parts = []
            for block in system_content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
                elif isinstance(block, str):
                    text_parts.append(block)
            system_content = "\n".join(text_parts)
        openai_messages.append({"role": "system", "content": system_content})

    # Convert messages
    for msg in anthropic_request.get("messages", []):
        role = msg.get("role", "user")
        content = msg.get("content", "")

        # Handle content blocks
        if isinstance(content, list):
            converted_content = convert_content_blocks_to_openai(content)
            if converted_content:
                openai_messages.append({"role": role, "content": converted_content})
        elif isinstance(content, str):
            openai_messages.append({"role": role, "content": content})

    # Build OpenAI request
    openai_request = {
        "model": get_grok_model(anthropic_request.get("model", "")),
        "messages": openai_messages,
        "stream": anthropic_request.get("stream", False),
    }

    # Optional parameters
    if "max_tokens" in anthropic_request:
        openai_request["max_tokens"] = anthropic_request["max_tokens"]
    if "temperature" in anthropic_request:
        openai_request["temperature"] = anthropic_request["temperature"]
    if "top_p" in anthropic_request:
        openai_request["top_p"] = anthropic_request["top_p"]
    if "stop_sequences" in anthropic_request:
        openai_request["stop"] = anthropic_request["stop_sequences"]

    # Convert tools if present
    if "tools" in anthropic_request:
        openai_request["tools"] = convert_tools_to_openai(anthropic_request["tools"])

    return openai_request


def convert_content_blocks_to_openai(content_blocks: List[Dict]) -> Any:
    """
    Convert Anthropic content blocks to OpenAI format.

    Handles:
    - text blocks
    - tool_use blocks
    - tool_result blocks
    - image blocks
    """
    # Check if it's simple text only
    text_only = all(
        block.get("type") == "text"
        for block in content_blocks
        if isinstance(block, dict)
    )

    if text_only:
        # Return concatenated text
        texts = []
        for block in content_blocks:
            if isinstance(block, dict) and block.get("type") == "text":
                texts.append(block.get("text", ""))
        return "\n".join(texts)

    # Handle mixed content (text + images)
    openai_content = []
    for block in content_blocks:
        if not isinstance(block, dict):
            continue

        block_type = block.get("type")

        if block_type == "text":
            openai_content.append({
                "type": "text",
                "text": block.get("text", "")
            })
        elif block_type == "image":
            source = block.get("source", {})
            if source.get("type") == "base64":
                media_type = source.get("media_type", "image/png")
                data = source.get("data", "")
                openai_content.append({
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{media_type};base64,{data}"
                    }
                })
        elif block_type == "tool_use":
            # Tool use is handled separately in the message structure
            pass
        elif block_type == "tool_result":
            # Tool result is handled separately
            pass

    return openai_content if openai_content else ""


def convert_tools_to_openai(anthropic_tools: List[Dict]) -> List[Dict]:
    """
    Convert Anthropic tools format to OpenAI tools format.

    Anthropic:
    {
        "name": "get_weather",
        "description": "Get weather",
        "input_schema": {...}
    }

    OpenAI:
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get weather",
            "parameters": {...}
        }
    }
    """
    openai_tools = []
    for tool in anthropic_tools:
        openai_tools.append({
            "type": "function",
            "function": {
                "name": tool.get("name", ""),
                "description": tool.get("description", ""),
                "parameters": tool.get("input_schema", {})
            }
        })
    return openai_tools


# ============================================================================
# Format Conversion: OpenAI (Grok) -> Anthropic
# ============================================================================


def convert_openai_to_anthropic(
    openai_response: Dict[str, Any],
    original_model: str
) -> Dict[str, Any]:
    """
    Convert OpenAI chat completion response to Anthropic message format.

    OpenAI format:
    {
        "id": "chatcmpl-...",
        "choices": [{
            "message": {
                "role": "assistant",
                "content": "Hello!"
            },
            "finish_reason": "stop"
        }],
        "usage": {...}
    }

    Anthropic format:
    {
        "id": "msg_...",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": "Hello!"}],
        "model": "claude-...",
        "stop_reason": "end_turn",
        "usage": {...}
    }
    """
    choice = openai_response.get("choices", [{}])[0]
    message = choice.get("message", {})

    # Convert content
    content_blocks = []

    # Handle text content
    text_content = message.get("content")
    if text_content:
        content_blocks.append({
            "type": "text",
            "text": text_content
        })

    # Handle tool calls
    tool_calls = message.get("tool_calls", [])
    for tool_call in tool_calls:
        function = tool_call.get("function", {})
        try:
            arguments = json.loads(function.get("arguments", "{}"))
        except json.JSONDecodeError:
            arguments = {}

        content_blocks.append({
            "type": "tool_use",
            "id": tool_call.get("id", f"toolu_{uuid.uuid4().hex[:24]}"),
            "name": function.get("name", ""),
            "input": arguments
        })

    # Convert finish reason
    finish_reason = choice.get("finish_reason", "stop")
    stop_reason_map = {
        "stop": "end_turn",
        "length": "max_tokens",
        "tool_calls": "tool_use",
        "content_filter": "end_turn",
    }
    stop_reason = stop_reason_map.get(finish_reason, "end_turn")

    # Convert usage
    openai_usage = openai_response.get("usage", {})
    usage = {
        "input_tokens": openai_usage.get("prompt_tokens", 0),
        "output_tokens": openai_usage.get("completion_tokens", 0),
    }

    return {
        "id": generate_message_id(),
        "type": "message",
        "role": "assistant",
        "model": original_model,
        "content": content_blocks,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": usage,
    }


# ============================================================================
# Streaming Conversion
# ============================================================================


async def stream_openai_to_anthropic(
    response: httpx.Response,
    original_model: str,
    message_id: str
) -> AsyncGenerator[str, None]:
    """
    Convert OpenAI streaming response to Anthropic SSE format.

    OpenAI SSE:
    data: {"choices": [{"delta": {"content": "Hello"}}]}

    Anthropic SSE:
    event: message_start
    data: {"type": "message_start", "message": {...}}

    event: content_block_start
    data: {"type": "content_block_start", "index": 0, "content_block": {...}}

    event: content_block_delta
    data: {"type": "content_block_delta", "index": 0, "delta": {...}}
    """

    # Send message_start
    message_start = {
        "type": "message_start",
        "message": {
            "id": message_id,
            "type": "message",
            "role": "assistant",
            "model": original_model,
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }
    }
    yield f"event: message_start\ndata: {json.dumps(message_start)}\n\n"

    # Track state
    content_block_started = False
    current_tool_index = -1
    tool_calls_buffer: Dict[int, Dict] = {}
    input_tokens = 0
    output_tokens = 0

    async for line in response.aiter_lines():
        if not line.startswith("data: "):
            continue

        data_str = line[6:].strip()
        if data_str == "[DONE]":
            break

        try:
            data = json.loads(data_str)
        except json.JSONDecodeError:
            continue

        choice = data.get("choices", [{}])[0]
        delta = choice.get("delta", {})
        finish_reason = choice.get("finish_reason")

        # Track usage if provided
        if "usage" in data:
            usage = data["usage"]
            input_tokens = usage.get("prompt_tokens", input_tokens)
            output_tokens = usage.get("completion_tokens", output_tokens)

        # Handle text content
        if "content" in delta and delta["content"]:
            if not content_block_started:
                # Start content block
                block_start = {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""}
                }
                yield f"event: content_block_start\ndata: {json.dumps(block_start)}\n\n"
                content_block_started = True

            # Send delta
            block_delta = {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": delta["content"]}
            }
            yield f"event: content_block_delta\ndata: {json.dumps(block_delta)}\n\n"

        # Handle tool calls
        if "tool_calls" in delta:
            for tool_call in delta["tool_calls"]:
                idx = tool_call.get("index", 0)

                if idx not in tool_calls_buffer:
                    # New tool call
                    tool_calls_buffer[idx] = {
                        "id": tool_call.get("id", f"toolu_{uuid.uuid4().hex[:24]}"),
                        "name": "",
                        "arguments": ""
                    }
                    current_tool_index = idx

                    # Close text block if open
                    if content_block_started:
                        block_stop = {
                            "type": "content_block_stop",
                            "index": 0
                        }
                        yield f"event: content_block_stop\ndata: {json.dumps(block_stop)}\n\n"
                        content_block_started = False

                # Update tool call data
                if "function" in tool_call:
                    func = tool_call["function"]
                    if "name" in func:
                        tool_calls_buffer[idx]["name"] = func["name"]
                        # Start tool use block
                        block_start = {
                            "type": "content_block_start",
                            "index": idx + 1,
                            "content_block": {
                                "type": "tool_use",
                                "id": tool_calls_buffer[idx]["id"],
                                "name": func["name"],
                                "input": {}
                            }
                        }
                        yield f"event: content_block_start\ndata: {json.dumps(block_start)}\n\n"

                    if "arguments" in func:
                        tool_calls_buffer[idx]["arguments"] += func["arguments"]
                        # Send input delta
                        block_delta = {
                            "type": "content_block_delta",
                            "index": idx + 1,
                            "delta": {
                                "type": "input_json_delta",
                                "partial_json": func["arguments"]
                            }
                        }
                        yield f"event: content_block_delta\ndata: {json.dumps(block_delta)}\n\n"

        # Handle finish
        if finish_reason:
            # Close any open blocks
            if content_block_started:
                block_stop = {"type": "content_block_stop", "index": 0}
                yield f"event: content_block_stop\ndata: {json.dumps(block_stop)}\n\n"

            for idx in tool_calls_buffer:
                block_stop = {"type": "content_block_stop", "index": idx + 1}
                yield f"event: content_block_stop\ndata: {json.dumps(block_stop)}\n\n"

            # Map stop reason
            stop_reason_map = {
                "stop": "end_turn",
                "length": "max_tokens",
                "tool_calls": "tool_use",
            }
            stop_reason = stop_reason_map.get(finish_reason, "end_turn")

            # Send message_delta with stop reason
            message_delta = {
                "type": "message_delta",
                "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                "usage": {"output_tokens": output_tokens}
            }
            yield f"event: message_delta\ndata: {json.dumps(message_delta)}\n\n"

    # Send message_stop
    yield f"event: message_stop\ndata: {{\"type\": \"message_stop\"}}\n\n"


# ============================================================================
# API Endpoints
# ============================================================================


@app.get("/")
async def root():
    """Health check endpoint."""
    return {
        "status": "ok",
        "service": "Grok-Anthropic Proxy",
        "version": "1.0.0",
    }


@app.get("/v1/models")
async def list_models():
    """List available models in Anthropic format."""
    return {
        "data": [
            {"id": "claude-opus-4-5-20251101", "object": "model"},
            {"id": "claude-sonnet-4-5-20250929", "object": "model"},
            {"id": "claude-haiku-4-5-20251001", "object": "model"},
            {"id": "grok-4", "object": "model"},
            {"id": "grok-4-1-thinking-1129", "object": "model"},
        ]
    }


@app.post("/v1/messages")
async def messages_endpoint(request: Request):
    """
    Anthropic Messages API endpoint.

    Accepts Anthropic format requests and proxies to Grok API.
    """
    try:
        # Parse request body
        body = await request.json()
        original_model = body.get("model", "")
        is_streaming = body.get("stream", False)

        logger.info(f"Request: model={original_model}, stream={is_streaming}")

        # Get API key
        api_key = (
            request.headers.get("x-api-key") or
            request.headers.get("authorization", "").replace("Bearer ", "") or
            GROK_API_KEY
        )

        if not api_key:
            raise HTTPException(status_code=401, detail="API key required")

        # Convert to OpenAI format
        openai_request = convert_anthropic_to_openai(body)
        grok_model = openai_request["model"]

        logger.info(f"Mapped model: {original_model} -> {grok_model}")

        # Prepare request
        grok_url = f"{GROK_API_BASE}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }

        if is_streaming:
            # Streaming response
            async def generate():
                message_id = generate_message_id()
                async with http_client.stream(
                    "POST",
                    grok_url,
                    json=openai_request,
                    headers=headers,
                ) as response:
                    if response.status_code != 200:
                        error_text = await response.aread()
                        logger.error(f"Grok API error: {error_text}")
                        error_event = {
                            "type": "error",
                            "error": {
                                "type": "api_error",
                                "message": error_text.decode()
                            }
                        }
                        yield f"event: error\ndata: {json.dumps(error_event)}\n\n"
                        return

                    async for chunk in stream_openai_to_anthropic(
                        response, original_model, message_id
                    ):
                        yield chunk

            return StreamingResponse(
                generate(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                }
            )
        else:
            # Non-streaming response
            response = await http_client.post(
                grok_url,
                json=openai_request,
                headers=headers,
            )

            if response.status_code != 200:
                logger.error(f"Grok API error: {response.text}")
                raise HTTPException(
                    status_code=response.status_code,
                    detail=f"Grok API error: {response.text}"
                )

            # Convert OpenAI response to Anthropic format
            openai_response = response.json()
            anthropic_response = convert_openai_to_anthropic(
                openai_response, original_model
            )
            return anthropic_response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error processing request: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# Main Entry Point
# ============================================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "api_server_grok_anthropic:app",
        host="0.0.0.0",
        port=8300,
        reload=True,
        log_level="info",
    )