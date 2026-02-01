#!/usr/bin/env python3
"""测试 Kiro 转换器"""

import json
from kiro_converter import (
    convert_anthropic_to_kiro,
    fix_history_alternation,
    parse_assistant_content,
    parse_user_tool_results,
)


def test_simple_message():
    """测试简单消息转换"""
    print("\n=== 测试 1: 简单消息 ===")

    anthropic_request = {
        "model": "claude-sonnet-4",
        "max_tokens": 1024,
        "messages": [
            {"role": "user", "content": "Hello, how are you?"}
        ]
    }

    kiro_request = convert_anthropic_to_kiro(anthropic_request)
    print(json.dumps(kiro_request, indent=2, ensure_ascii=False))

    # 验证
    assert kiro_request["modelId"] == "claude-sonnet-4"
    assert kiro_request["conversationState"]["currentMessage"]["userInputMessage"]["content"] == "Hello, how are you?"
    assert kiro_request["conversationState"]["history"] == []
    print("✓ 通过")


def test_tool_call():
    """测试工具调用转换"""
    print("\n=== 测试 2: 工具调用 ===")

    anthropic_request = {
        "model": "claude-opus-4",
        "max_tokens": 2048,
        "messages": [
            {"role": "user", "content": "Read the file /tmp/test.txt"},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Let me read that file for you."},
                    {
                        "type": "tool_use",
                        "id": "toolu_123",
                        "name": "Read",
                        "input": {"file_path": "/tmp/test.txt"}
                    }
                ]
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_123",
                        "content": "File content: Hello World"
                    }
                ]
            },
            {"role": "assistant", "content": "The file contains: Hello World"},
            {"role": "user", "content": "What's in the file?"}
        ],
        "tools": [
            {
                "name": "Read",
                "description": "Read a file from the filesystem",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string", "description": "Path to the file"}
                    },
                    "required": ["file_path"]
                }
            }
        ]
    }

    kiro_request = convert_anthropic_to_kiro(anthropic_request)
    print(json.dumps(kiro_request, indent=2, ensure_ascii=False))

    # 验证
    assert kiro_request["modelId"] == "claude-opus-4"
    assert len(kiro_request["conversationState"]["history"]) == 4

    # 验证历史消息交替
    history = kiro_request["conversationState"]["history"]
    assert "userInputMessage" in history[0]
    assert "assistantResponseMessage" in history[1]
    assert "toolUses" in history[1]["assistantResponseMessage"]
    assert "userInputMessage" in history[2]
    assert "toolResults" in history[2]["userInputMessage"]["userInputMessageContext"]
    assert "assistantResponseMessage" in history[3]

    # 验证工具配置
    assert "toolConfig" in kiro_request["conversationState"]["currentMessage"]["userInputMessage"]
    tools = kiro_request["conversationState"]["currentMessage"]["userInputMessage"]["toolConfig"]["tools"]
    assert len(tools) == 1
    assert tools[0]["toolSpecification"]["name"] == "Read"

    print("✓ 通过")


def test_history_alternation_fix():
    """测试历史消息交替修复"""
    print("\n=== 测试 3: 历史消息交替修复 ===")

    # 场景 1: 连续两条 user
    history = [
        {"userInputMessage": {"content": "First user message"}},
        {"userInputMessage": {"content": "Second user message"}},
    ]

    fixed = fix_history_alternation(history)
    print("\n场景 1: 连续两条 user")
    print(json.dumps(fixed, indent=2, ensure_ascii=False))

    # 应该插入一个 assistant 在中间，并在结尾添加一个 assistant
    assert len(fixed) == 4
    assert "userInputMessage" in fixed[0]
    assert "assistantResponseMessage" in fixed[1]  # 插入的占位消息
    assert "userInputMessage" in fixed[2]
    assert "assistantResponseMessage" in fixed[3]  # 结尾的占位消息
    print("✓ 通过")

    # 场景 2: 连续两条 assistant
    history = [
        {"assistantResponseMessage": {"content": "First assistant message"}},
        {"assistantResponseMessage": {"content": "Second assistant message"}},
    ]

    fixed = fix_history_alternation(history)
    print("\n场景 2: 连续两条 assistant")
    print(json.dumps(fixed, indent=2, ensure_ascii=False))

    assert len(fixed) == 3  # 2 原始 + 1 插入
    assert "assistantResponseMessage" in fixed[0]
    assert "userInputMessage" in fixed[1]  # 插入的占位消息
    assert "assistantResponseMessage" in fixed[2]
    # 不需要结尾占位，因为已经是 assistant 结尾
    print("✓ 通过")

    # 场景 3: toolUses 但没有 toolResults
    history = [
        {
            "assistantResponseMessage": {
                "content": "Let me use a tool",
                "toolUses": [{"toolUseId": "123", "name": "Read", "input": {}}]
            }
        },
        {"userInputMessage": {"content": "Continue"}},  # 没有 toolResults
    ]

    fixed = fix_history_alternation(history)
    print("\n场景 3: toolUses 但没有 toolResults")
    print(json.dumps(fixed, indent=2, ensure_ascii=False))

    # 应该清除 toolUses
    assert "toolUses" not in fixed[0]["assistantResponseMessage"]
    print("✓ 通过")

    # 场景 4: 没有 toolUses 但有 toolResults
    history = [
        {"assistantResponseMessage": {"content": "No tools"}},  # 没有 toolUses
        {
            "userInputMessage": {
                "content": "Result",
                "userInputMessageContext": {
                    "toolResults": [{"toolUseId": "123", "content": "result"}]
                }
            }
        },
    ]

    fixed = fix_history_alternation(history)
    print("\n场景 4: 没有 toolUses 但有 toolResults")
    print(json.dumps(fixed, indent=2, ensure_ascii=False))

    # 应该清除 toolResults
    assert "userInputMessageContext" not in fixed[1]["userInputMessage"]
    print("✓ 通过")


def test_parse_assistant_content():
    """测试 assistant 内容解析"""
    print("\n=== 测试 4: Assistant 内容解析 ===")

    # 场景 1: 纯文本
    content = "Hello, world!"
    text, tools = parse_assistant_content(content)
    assert text == "Hello, world!"
    assert tools == []
    print("✓ 场景 1 通过")

    # 场景 2: 文本 + 工具调用
    content = [
        {"type": "text", "text": "Let me help you."},
        {
            "type": "tool_use",
            "id": "toolu_123",
            "name": "Read",
            "input": {"file_path": "/tmp/test.txt"}
        }
    ]
    text, tools = parse_assistant_content(content)
    assert text == "Let me help you."
    assert len(tools) == 1
    assert tools[0]["name"] == "Read"
    assert tools[0]["toolUseId"] == "toolu_123"
    print("✓ 场景 2 通过")

    # 场景 3: 多个工具调用
    content = [
        {"type": "text", "text": "Processing..."},
        {"type": "tool_use", "id": "tool1", "name": "Read", "input": {}},
        {"type": "tool_use", "id": "tool2", "name": "Write", "input": {}},
    ]
    text, tools = parse_assistant_content(content)
    assert len(tools) == 2
    print("✓ 场景 3 通过")


def test_parse_user_tool_results():
    """测试 user 工具结果解析"""
    print("\n=== 测试 5: User 工具结果解析 ===")

    # 场景 1: 单个工具结果
    content = [
        {
            "type": "tool_result",
            "tool_use_id": "toolu_123",
            "content": "File content here"
        }
    ]
    results = parse_user_tool_results(content)
    assert results is not None
    assert len(results) == 1
    assert results[0]["toolUseId"] == "toolu_123"
    assert results[0]["content"] == "File content here"
    assert results[0]["status"] == "success"
    print("✓ 场景 1 通过")

    # 场景 2: 错误结果
    content = [
        {
            "type": "tool_result",
            "tool_use_id": "toolu_456",
            "content": "Error: File not found",
            "is_error": True
        }
    ]
    results = parse_user_tool_results(content)
    assert results[0]["status"] == "error"
    print("✓ 场景 2 通过")

    # 场景 3: 列表格式的内容
    content = [
        {
            "type": "tool_result",
            "tool_use_id": "toolu_789",
            "content": [
                {"type": "text", "text": "Line 1"},
                {"type": "text", "text": "Line 2"}
            ]
        }
    ]
    results = parse_user_tool_results(content)
    assert "Line 1" in results[0]["content"]
    assert "Line 2" in results[0]["content"]
    print("✓ 场景 3 通过")


def test_complex_conversation():
    """测试复杂对话场景"""
    print("\n=== 测试 6: 复杂对话 ===")

    anthropic_request = {
        "model": "claude-opus-4-5-20251101",
        "max_tokens": 4096,
        "system": "You are a helpful assistant.",
        "messages": [
            {"role": "user", "content": "List files in /tmp"},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "I'll list the files."},
                    {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls /tmp"}}
                ]
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": "file1.txt\nfile2.txt"}
                ]
            },
            {"role": "assistant", "content": "Found 2 files. Let me read the first one."},
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "t2", "name": "Read", "input": {"file_path": "/tmp/file1.txt"}}
                ]
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t2", "content": "Content of file1"}
                ]
            },
            {"role": "user", "content": "What did you find?"}
        ],
        "tools": [
            {"name": "Bash", "description": "Run bash command", "input_schema": {"type": "object", "properties": {}}},
            {"name": "Read", "description": "Read file", "input_schema": {"type": "object", "properties": {}}}
        ]
    }

    kiro_request = convert_anthropic_to_kiro(anthropic_request)
    print(json.dumps(kiro_request, indent=2, ensure_ascii=False))

    # 验证
    assert kiro_request["modelId"] == "claude-opus-4.5"
    assert "systemPrompt" in kiro_request["conversationState"]

    history = kiro_request["conversationState"]["history"]
    print(f"\n历史消息数量: {len(history)}")

    # 验证交替
    for i, msg in enumerate(history):
        is_user = "userInputMessage" in msg
        is_assistant = "assistantResponseMessage" in msg
        role = "user" if is_user else "assistant"
        print(f"  [{i}] {role}")

        if i > 0:
            prev_is_user = "userInputMessage" in history[i-1]
            # 不应该有连续相同角色
            assert is_user != prev_is_user, f"消息 {i} 和 {i-1} 角色相同"

    print("✓ 通过")


if __name__ == "__main__":
    print("开始测试 Kiro 转换器...")

    try:
        test_simple_message()
        test_tool_call()
        test_history_alternation_fix()
        test_parse_assistant_content()
        test_parse_user_tool_results()
        test_complex_conversation()

        print("\n" + "="*50)
        print("✓ 所有测试通过！")
        print("="*50)
    except AssertionError as e:
        print(f"\n✗ 测试失败: {e}")
        import traceback
        traceback.print_exc()
    except Exception as e:
        print(f"\n✗ 错误: {e}")
        import traceback
        traceback.print_exc()
