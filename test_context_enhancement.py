#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试用户输入预处理增强功能
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch
from api_server import extract_project_context, enhance_user_message


async def test_context_extraction():
    """测试上下文提取"""
    print("\n=== 测试 1: 上下文提取 ===")

    # 模拟对话历史
    messages = [
        {"role": "user", "content": "我想用 Python 和 FastAPI 开发一个 RESTful API"},
        {"role": "assistant", "content": "好的，我可以帮你。首先需要安装 FastAPI..."},
        {"role": "user", "content": "如何添加用户认证？"},
        {"role": "assistant", "content": "可以使用 JWT token..."},
        {"role": "user", "content": "帮我写一个登录接口"},
    ]

    # 模拟 API 响应
    mock_response = {
        "language": "Python",
        "framework": "FastAPI",
        "features": ["RESTful API", "用户认证", "JWT token"],
        "last_topics": ["登录接口"]
    }

    with patch('api_server.http_client') as mock_client:
        # 创建一个正确的 mock response
        mock_json_response = {"choices": [{"message": {"content": json.dumps(mock_response, ensure_ascii=False)}}]}
        mock_response_obj = AsyncMock()
        mock_response_obj.status_code = 200
        # json() 是同步方法，不是 async
        mock_response_obj.json = MagicMock(return_value=mock_json_response)

        mock_client.post = AsyncMock(return_value=mock_response_obj)

        context = await extract_project_context(messages, "test-session-001")

    print(f"\n提取的上下文:")
    print(json.dumps(context, ensure_ascii=False, indent=2))

    return context


async def test_message_enhancement():
    """测试消息增强"""
    print("\n=== 测试 2: 消息增强 ===")

    # 模拟项目上下文
    mock_context = {
        "language": "Python",
        "framework": "FastAPI",
        "features": ["RESTful API", "用户认证", "JWT token"],
        "last_topics": ["登录接口", "用户认证"]
    }

    # 模拟已有的对话历史
    base_messages = [
        {"role": "user", "content": "我在开发一个 Python FastAPI 项目"},
        {"role": "assistant", "content": "好的，FastAPI 是一个现代化的异步 Web 框架..."},
    ]

    # 测试不同的用户输入
    test_inputs = [
        "帮我优化这个函数",
        "添加错误处理",
        "如何测试这个接口？",
        "写一个数据库模型"
    ]

    # 设置 session context
    from api_server import update_session_context
    update_session_context("test-session-001", mock_context, 0)

    for user_input in test_inputs:
        # 构建完整的消息列表
        messages = base_messages + [{"role": "user", "content": user_input}]

        # 调用增强函数
        enhanced_messages = await enhance_user_message(messages, "test-session-001")

        print(f"\n原始输入: {user_input}")
        print(f"增强后: {enhanced_messages[-1]['content']}")
        print("-" * 60)


async def test_integration():
    """测试完整流程"""
    print("\n=== 测试 3: 完整流程 ===")

    # 1. 提取上下文
    messages = [
        {"role": "user", "content": "我在开发一个 Vue3 + TypeScript 的前端项目"},
        {"role": "assistant", "content": "好的，Vue3 配合 TypeScript 是很好的选择..."},
        {"role": "user", "content": "如何使用 Pinia 做状态管理？"},
        {"role": "assistant", "content": "Pinia 是 Vue3 推荐的状态管理库..."},
    ]

    # 模拟 API 响应
    mock_context_response = {
        "language": "TypeScript",
        "framework": "Vue3",
        "features": ["Pinia 状态管理"],
        "last_topics": ["状态管理"]
    }

    with patch('api_server.http_client') as mock_client:
        # 创建一个正确的 mock response
        mock_json_response = {"choices": [{"message": {"content": json.dumps(mock_context_response, ensure_ascii=False)}}]}
        mock_response_obj = AsyncMock()
        mock_response_obj.status_code = 200
        # json() 是同步方法，不是 async
        mock_response_obj.json = MagicMock(return_value=mock_json_response)

        mock_client.post = AsyncMock(return_value=mock_response_obj)

        context = await extract_project_context(messages, "test-session-002")

        print(f"\n步骤 1 - 提取的上下文:")
        print(json.dumps(context, ensure_ascii=False, indent=2))

        # 2. 增强新消息（在同一个 mock 作用域内）
        new_message = [{"role": "user", "content": "帮我写一个用户信息的 store"}]
        enhanced = await enhance_user_message(new_message, "test-session-002")

        print(f"\n步骤 2 - 消息增强:")
        print(f"原始: {new_message[0]['content']}")
        print(f"增强: {enhanced}")


async def main():
    """主测试函数"""
    print("\n" + "=" * 80)
    print("用户输入预处理增强功能 - 单元测试")
    print("=" * 80)

    try:
        await test_context_extraction()
        await test_message_enhancement()
        await test_integration()

        print("\n" + "=" * 80)
        print("✅ 所有测试完成")
        print("=" * 80 + "\n")

    except Exception as e:
        print(f"\n❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())
