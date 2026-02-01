#!/usr/bin/env python3
"""测试 XML 格式工具调用解析"""

import sys
sys.path.insert(0, '/www/wwwroot/ai-history-manager')

from api_server import parse_xml_tool_blocks, parse_inline_tool_blocks
import json

# 测试用例 1: 简单的 XML 工具调用
test1 = """
I'll read the file for you.

<Read>
<file_path>/etc/hostname</file_path>
</Read>
"""

# 测试用例 2: 带多个参数的 XML 工具调用
test2 = """
Let me search for the pattern.

<Grep>
<pattern>def main</pattern>
<path>/www/wwwroot/ai-history-manager</path>
<glob>*.py</glob>
</Grep>
"""

# 测试用例 3: 多个工具调用
test3 = """
I'll check both files.

<Read>
<file_path>/etc/hostname</file_path>
</Read>

Now let me check another file.

<Read>
<file_path>/etc/hosts</file_path>
</Read>
"""

# 测试用例 4: 混合格式（应该优先使用 [Calling tool:] 格式）
test4 = """
I'll read the file.

[Calling tool: Read]
Input: {"file_path": "/etc/hostname"}
"""

# 测试用例 5: Bash 工具调用
test5 = """
Let me run a command.

<Bash>
<command>ls -la /tmp</command>
<description>List files in /tmp</description>
</Bash>
"""

def test_parse(name, text):
    print(f"\n{'='*60}")
    print(f"Test: {name}")
    print(f"{'='*60}")
    print(f"Input text:\n{text[:200]}..." if len(text) > 200 else f"Input text:\n{text}")

    # 测试 parse_xml_tool_blocks
    xml_blocks = parse_xml_tool_blocks(text)
    print(f"\nXML parse result ({len(xml_blocks)} blocks):")
    for i, block in enumerate(xml_blocks):
        if block['type'] == 'tool_use':
            print(f"  [{i}] tool_use: {block['name']}")
            print(f"       input: {json.dumps(block['input'], ensure_ascii=False)[:100]}")
        else:
            print(f"  [{i}] text: {block['text'][:50]}..." if len(block.get('text', '')) > 50 else f"  [{i}] text: {block.get('text', '')}")

    # 测试 parse_inline_tool_blocks（应该自动检测格式）
    inline_blocks = parse_inline_tool_blocks(text)
    print(f"\nInline parse result ({len(inline_blocks)} blocks):")
    for i, block in enumerate(inline_blocks):
        if block['type'] == 'tool_use':
            print(f"  [{i}] tool_use: {block['name']}")
            print(f"       input: {json.dumps(block['input'], ensure_ascii=False)[:100]}")
        else:
            print(f"  [{i}] text: {block['text'][:50]}..." if len(block.get('text', '')) > 50 else f"  [{i}] text: {block.get('text', '')}")

if __name__ == '__main__':
    test_parse("Simple XML tool call", test1)
    test_parse("XML with multiple params", test2)
    test_parse("Multiple XML tool calls", test3)
    test_parse("[Calling tool:] format (should use this)", test4)
    test_parse("Bash XML tool call", test5)

    print("\n" + "="*60)
    print("All tests completed!")
    print("="*60)
