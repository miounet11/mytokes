#!/usr/bin/env python3
"""并发请求测试脚本 - 验证多请求是否被并行处理"""

import asyncio
import time
import httpx
import json

API_URL = "http://127.0.0.1:8100/v1/messages"
API_KEY = "test-key"  # 任意值

async def send_request(client: httpx.AsyncClient, req_id: int) -> dict:
    """发送单个请求"""
    start = time.time()

    body = {
        "model": "claude-sonnet-4",
        "messages": [{"role": "user", "content": f"Say 'Request {req_id} received' in one line."}],
        "max_tokens": 50,
        "stream": False,
    }

    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }

    try:
        response = await client.post(API_URL, json=body, headers=headers, timeout=60)
        duration = time.time() - start
        return {
            "id": req_id,
            "status": response.status_code,
            "duration": duration,
            "success": response.status_code == 200,
        }
    except Exception as e:
        duration = time.time() - start
        return {
            "id": req_id,
            "status": 0,
            "duration": duration,
            "success": False,
            "error": str(e),
        }

async def test_concurrent(num_requests: int = 10):
    """测试并发请求"""
    print(f"\n{'='*60}")
    print(f"并发测试: 同时发送 {num_requests} 个请求")
    print(f"{'='*60}\n")

    # 使用独立连接
    async with httpx.AsyncClient(http2=False, timeout=120) as client:
        start_time = time.time()

        # 并发发送所有请求
        tasks = [send_request(client, i) for i in range(num_requests)]
        results = await asyncio.gather(*tasks)

        total_time = time.time() - start_time

    # 分析结果
    successful = [r for r in results if r["success"]]
    failed = [r for r in results if not r["success"]]

    print("结果:")
    for r in sorted(results, key=lambda x: x["duration"]):
        status = "✓" if r["success"] else "✗"
        print(f"  {status} 请求 {r['id']:2d}: {r['duration']:.2f}s (状态: {r['status']})")

    print(f"\n统计:")
    print(f"  总耗时: {total_time:.2f}s")
    print(f"  成功: {len(successful)}/{num_requests}")
    print(f"  平均响应时间: {sum(r['duration'] for r in results)/len(results):.2f}s")

    if len(successful) > 1:
        durations = [r['duration'] for r in successful]
        # 如果是串行处理，总时间应该接近所有请求时间之和
        # 如果是并行处理，总时间应该接近最长请求时间
        sum_durations = sum(durations)
        max_duration = max(durations)

        parallelism = sum_durations / total_time if total_time > 0 else 1
        print(f"\n并行度分析:")
        print(f"  请求时间总和: {sum_durations:.2f}s")
        print(f"  实际总耗时: {total_time:.2f}s")
        print(f"  估算并行度: {parallelism:.1f}x")

        if parallelism > 1.5:
            print(f"\n✅ 请求被并行处理！并行度约 {parallelism:.1f}x")
        else:
            print(f"\n⚠️  请求可能被串行处理，需要进一步调查")

    return results

if __name__ == "__main__":
    print("AI History Manager 并发测试")
    print("="*60)

    # 运行测试 - 10 个并发请求
    asyncio.run(test_concurrent(10))
