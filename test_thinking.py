#!/usr/bin/env python3
"""测试 Extended Thinking 模拟功能"""

import httpx
import json
import asyncio

API_BASE = "http://127.0.0.1:8100"
API_KEY = "test-key"

async def test_anthropic_thinking_stream():
    """测试 Anthropic API 流式响应的 thinking 功能"""

    request_body = {
        "model": "claude-sonnet-4",
        "max_tokens": 16000,
        "thinking": {
            "type": "enabled",
            "budget_tokens": 10000
        },
        "messages": [
            {"role": "user", "content": "What is 15 * 23? Think step by step."}
        ],
        "stream": True
    }

    headers = {
        "x-api-key": API_KEY,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }

    print("=" * 60)
    print("Testing Anthropic API Stream with Thinking")
    print("=" * 60)

    async with httpx.AsyncClient(timeout=120.0) as client:
        async with client.stream(
            "POST",
            f"{API_BASE}/v1/messages",
            json=request_body,
            headers=headers,
        ) as response:
            print(f"Status: {response.status_code}")
            print("\n--- Stream Events ---\n")

            buffer = ""
            event_count = 0
            thinking_found = False
            text_found = False

            async for chunk in response.aiter_text():
                buffer += chunk
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = line.strip()

                    if not line:
                        continue

                    if line.startswith("data:"):
                        data_str = line[5:].strip()

                        if data_str == "[DONE]":
                            print("\n[DONE]")
                            continue

                        try:
                            data = json.loads(data_str)
                            event_count += 1
                            event_type = data.get("type", "unknown")

                            # 检查 thinking 相关事件
                            if "thinking" in str(data):
                                thinking_found = True
                                print(f"\n🧠 Event #{event_count} ({event_type}):")
                                print(json.dumps(data, indent=2, ensure_ascii=False)[:500])

                            # 检查 text 相关事件
                            if event_type == "content_block_start":
                                block = data.get("content_block", {})
                                block_type = block.get("type")
                                print(f"\n📦 Content Block Start: type={block_type}")
                                if block_type == "thinking":
                                    thinking_found = True
                                elif block_type == "text":
                                    text_found = True

                            if event_type == "content_block_delta":
                                delta = data.get("delta", {})
                                delta_type = delta.get("type")
                                if delta_type == "thinking_delta":
                                    thinking = delta.get("thinking", "")[:100]
                                    print(f"  🧠 Thinking: {thinking}...")
                                elif delta_type == "text_delta":
                                    text = delta.get("text", "")[:100]
                                    print(f"  📝 Text: {text}...")

                            if event_type == "message_stop":
                                print(f"\n✅ Message Stop")

                        except json.JSONDecodeError as e:
                            print(f"JSON Error: {e}")

            print(f"\n\nTotal events: {event_count}")
            print(f"Thinking found: {thinking_found}")
            print(f"Text found: {text_found}")


async def test_anthropic_thinking_non_stream():
    """测试 Anthropic API 非流式响应的 thinking 功能"""

    request_body = {
        "model": "claude-sonnet-4",
        "max_tokens": 16000,
        "thinking": {
            "type": "enabled",
            "budget_tokens": 10000
        },
        "messages": [
            {"role": "user", "content": "What is 15 * 23? Think step by step."}
        ],
        "stream": False
    }

    headers = {
        "x-api-key": API_KEY,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }

    print("\n" + "=" * 60)
    print("Testing Anthropic API Non-Stream with Thinking")
    print("=" * 60)

    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.post(
            f"{API_BASE}/v1/messages",
            json=request_body,
            headers=headers,
        )

        print(f"Status: {response.status_code}")

        try:
            data = response.json()
            print("\nResponse:")
            print(json.dumps(data, indent=2, ensure_ascii=False)[:2000])

            # 检查 content blocks
            content = data.get("content", [])
            print(f"\nContent blocks: {len(content)}")

            for i, block in enumerate(content):
                block_type = block.get("type")
                print(f"  Block {i}: type={block_type}")
                if block_type == "thinking":
                    thinking = block.get("thinking", "")[:200]
                    print(f"    🧠 Thinking: {thinking}...")
                elif block_type == "text":
                    text = block.get("text", "")[:200]
                    print(f"    📝 Text: {text}...")

        except Exception as e:
            print(f"Error: {e}")
            print(f"Raw: {response.text[:500]}")


if __name__ == "__main__":
    asyncio.run(test_anthropic_thinking_stream())
    asyncio.run(test_anthropic_thinking_non_stream())
