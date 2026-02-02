#!/usr/bin/env python3
"""首字延迟测试脚本

测试代理服务器的首字响应时间（TTFT - Time To First Token）
"""

import asyncio
import time
import httpx
import json
import sys

# 测试配置
PROXY_URL = "http://localhost:8100/v1/messages"
DIRECT_URL = "https://api.kiro.ai/v1/messages"  # 直连对比

# 从环境或配置获取 API Key
import os
API_KEY = os.environ.get("KIRO_API_KEY", "dba22273-65d3-4dc1-8ce9-182f680b2bf5")

# 测试用例
TEST_CASES = [
    {
        "name": "简单问候",
        "messages": [{"role": "user", "content": "你好"}],
        "model": "claude-sonnet-4-5-20250929",
    },
    {
        "name": "短问题",
        "messages": [{"role": "user", "content": "1+1等于多少？"}],
        "model": "claude-sonnet-4-5-20250929",
    },
    {
        "name": "中等对话",
        "messages": [
            {"role": "user", "content": "请解释什么是递归"},
            {"role": "assistant", "content": "递归是一种编程技术，指函数调用自身来解决问题。"},
            {"role": "user", "content": "能给个例子吗？"},
        ],
        "model": "claude-sonnet-4-5-20250929",
    },
]


async def measure_ttft(url: str, request_body: dict, headers: dict) -> tuple[float, str]:
    """测量首字延迟

    Returns:
        (ttft_ms, first_chunk) - 首字延迟（毫秒）和第一个数据块
    """
    start_time = time.perf_counter()
    first_chunk = ""
    ttft = 0.0

    async with httpx.AsyncClient(timeout=60.0) as client:
        async with client.stream("POST", url, json=request_body, headers=headers) as response:
            async for chunk in response.aiter_text():
                if chunk.strip():
                    ttft = (time.perf_counter() - start_time) * 1000  # 转换为毫秒
                    first_chunk = chunk[:100]  # 只取前 100 字符
                    break

    return ttft, first_chunk


async def run_test(test_case: dict, num_runs: int = 3) -> dict:
    """运行单个测试用例"""
    print(f"\n{'='*60}")
    print(f"测试: {test_case['name']}")
    print(f"消息数: {len(test_case['messages'])}")
    print(f"{'='*60}")

    request_body = {
        "model": test_case["model"],
        "messages": test_case["messages"],
        "stream": True,
        "max_tokens": 100,
    }

    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
        "anthropic-version": "2023-06-01",
    }

    results = {
        "name": test_case["name"],
        "proxy_ttft": [],
        "errors": [],
    }

    # 测试代理服务器
    print(f"\n📡 测试代理服务器 ({PROXY_URL})")
    for i in range(num_runs):
        try:
            ttft, first_chunk = await measure_ttft(PROXY_URL, request_body, headers)
            results["proxy_ttft"].append(ttft)
            print(f"  运行 {i+1}: {ttft:.0f}ms")
            if i == 0:
                print(f"  首块: {first_chunk[:50]}...")
        except Exception as e:
            results["errors"].append(f"代理测试失败: {e}")
            print(f"  运行 {i+1}: ❌ 错误 - {e}")

        # 短暂等待避免限流
        await asyncio.sleep(0.5)

    # 计算统计
    if results["proxy_ttft"]:
        avg = sum(results["proxy_ttft"]) / len(results["proxy_ttft"])
        min_val = min(results["proxy_ttft"])
        max_val = max(results["proxy_ttft"])
        print(f"\n📊 代理统计: 平均={avg:.0f}ms, 最小={min_val:.0f}ms, 最大={max_val:.0f}ms")

    return results


async def main():
    print("\n" + "="*60)
    print("🚀 首字延迟测试 (TTFT - Time To First Token)")
    print("="*60)

    if not API_KEY:
        print("\n⚠️ 警告: 未设置 KIRO_API_KEY 环境变量")
        print("请设置: export KIRO_API_KEY=your_api_key")
        return

    all_results = []

    for test_case in TEST_CASES:
        try:
            result = await run_test(test_case, num_runs=3)
            all_results.append(result)
        except Exception as e:
            print(f"\n❌ 测试 '{test_case['name']}' 失败: {e}")

    # 汇总报告
    print("\n" + "="*60)
    print("📋 测试汇总")
    print("="*60)

    for result in all_results:
        name = result["name"]
        if result["proxy_ttft"]:
            avg = sum(result["proxy_ttft"]) / len(result["proxy_ttft"])
            print(f"  {name}: {avg:.0f}ms (平均)")
        else:
            print(f"  {name}: ❌ 无数据")

    # 总体评估
    all_ttft = []
    for r in all_results:
        all_ttft.extend(r["proxy_ttft"])

    if all_ttft:
        overall_avg = sum(all_ttft) / len(all_ttft)
        print(f"\n🎯 总体平均首字延迟: {overall_avg:.0f}ms")

        if overall_avg < 500:
            print("✅ 优秀 - 首字延迟 < 500ms")
        elif overall_avg < 1000:
            print("⚠️ 良好 - 首字延迟 < 1000ms")
        else:
            print("❌ 需优化 - 首字延迟 > 1000ms")


if __name__ == "__main__":
    asyncio.run(main())
