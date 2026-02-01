#!/usr/bin/env python3
"""测试 Kiro API 返回格式，查看 reasoning_content/thinking 字段"""

import httpx
import json
import asyncio

KIRO_PROXY_BASE = "http://127.0.0.1:8000"
KIRO_PROXY_URL = f"{KIRO_PROXY_BASE}/kiro/v1/chat/completions"
KIRO_API_KEY = "dba22273-65d3-4dc1-8ce9-182f680b2bf5"

async def test_stream_response():
    """测试流式响应，打印所有字段"""

    request_body = {
        "model": "claude-sonnet-4",
        "messages": [
            {"role": "user", "content": "What is 2+2? Think step by step."}
        ],
        "stream": True,
        "max_tokens": 1000,
    }

    headers = {
        "Authorization": f"Bearer {KIRO_API_KEY}",
        "Content-Type": "application/json",
    }

    print("=" * 60)
    print("Testing Kiro API Stream Response")
    print("=" * 60)

    async with httpx.AsyncClient(timeout=60.0) as client:
        async with client.stream(
            "POST",
            KIRO_PROXY_URL,
            json=request_body,
            headers=headers,
        ) as response:
            print(f"Status: {response.status_code}")
            print(f"Headers: {dict(response.headers)}")
            print("\n--- Stream Events ---\n")

            buffer = ""
            event_count = 0

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

                            # 打印完整的 JSON 结构（前 10 个事件）
                            if event_count <= 10:
                                print(f"\nEvent #{event_count}:")
                                print(json.dumps(data, indent=2, ensure_ascii=False))
                            elif event_count == 11:
                                print("\n... (showing summary for remaining events) ...")

                            # 检查所有可能包含 thinking/reasoning 的字段
                            choice = data.get("choices", [{}])[0]
                            delta = choice.get("delta", {})

                            # 打印 delta 中的所有键
                            if delta and event_count <= 20:
                                keys = list(delta.keys())
                                if keys and keys != ['content'] and keys != ['role']:
                                    print(f"  Delta keys: {keys}")

                            # 检查特定字段
                            for field in ['reasoning_content', 'reasoning', 'thinking',
                                         'thought', 'internal_thoughts', 'chain_of_thought']:
                                if field in delta:
                                    print(f"\n*** Found {field} in delta: {delta[field][:100]}...")
                                if field in choice:
                                    print(f"\n*** Found {field} in choice: {choice[field][:100]}...")
                                if field in data:
                                    print(f"\n*** Found {field} in data: {data[field][:100]}...")

                        except json.JSONDecodeError as e:
                            print(f"JSON Error: {e}")
                            print(f"Raw: {data_str[:200]}")
                    else:
                        print(f"Non-data line: {line}")

            print(f"\n\nTotal events: {event_count}")


async def test_non_stream_response():
    """测试非流式响应，打印完整结构"""

    request_body = {
        "model": "claude-sonnet-4",
        "messages": [
            {"role": "user", "content": "What is 2+2? Think step by step."}
        ],
        "stream": False,
        "max_tokens": 1000,
    }

    headers = {
        "Authorization": f"Bearer {KIRO_API_KEY}",
        "Content-Type": "application/json",
    }

    print("\n" + "=" * 60)
    print("Testing Kiro API Non-Stream Response")
    print("=" * 60)

    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            KIRO_PROXY_URL,
            json=request_body,
            headers=headers,
        )

        print(f"Status: {response.status_code}")

        try:
            data = response.json()
            print("\nFull Response Structure:")
            print(json.dumps(data, indent=2, ensure_ascii=False))

            # 检查 message 中的所有字段
            choice = data.get("choices", [{}])[0]
            message = choice.get("message", {})

            print(f"\nMessage keys: {list(message.keys())}")

            # 检查特定字段
            for field in ['reasoning_content', 'reasoning', 'thinking',
                         'thought', 'internal_thoughts', 'chain_of_thought']:
                if field in message:
                    val = message[field]
                    if isinstance(val, str):
                        print(f"\n*** Found {field}: {val[:200]}...")
                    else:
                        print(f"\n*** Found {field}: {val}")

        except Exception as e:
            print(f"Error: {e}")
            print(f"Raw: {response.text[:500]}")


if __name__ == "__main__":
    asyncio.run(test_stream_response())
    asyncio.run(test_non_stream_response())
