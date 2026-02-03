# Tools 文本注入方案备份

> 备份时间: 2026-02-03
> 状态: 已归档 (准备升级为原生 tools 支持)

## 概述

当前实现将 Anthropic tools 定义转换为文本指令注入到 system prompt 中，而不是直接传递给 Kiro 网关。

## 核心代码位置

`api_server.py` 第 1634-1709 行

## 实现逻辑

### 1. 工具定义注入 (第 1634-1651 行)

```python
# ==================== 工具定义注入系统提示 ====================
# 网关不支持 OpenAI tool_calls，将工具定义注入系统提示
# 模型通过 [Calling tool: xxx] 格式调用工具，响应时自动解析
anthropic_tools = anthropic_body.get("tools", [])
if anthropic_tools:
    tool_instruction = build_tool_instruction(anthropic_tools)
    # 找到 system 消息并追加工具指令
    for msg in openai_body["messages"]:
        if msg.get("role") == "system":
            msg["content"] = msg["content"] + "\n\n" + tool_instruction
            break
    else:
        # 没有 system 消息，创建一个
        openai_body["messages"].insert(0, {
            "role": "system",
            "content": tool_instruction
        })
```

### 2. 工具指令构建函数 (第 1655-1709 行)

```python
def build_tool_instruction(tools: list) -> str:
    """将 Anthropic tools 转换为系统提示中的工具指令文本

    这样模型即使没有 OpenAI tools 参数也知道如何调用工具。
    """
    lines = [
        "# Tool Call Format",
        "",
        "You have access to the following tools. To call a tool, output EXACTLY this format:",
        "",
        "[Calling tool: tool_name]",
        "Input: {\"param\": \"value\"}",
        "",
        "IMPORTANT RULES:",
        "- You MUST use the exact format above to call tools",
        "- The Input MUST be valid JSON on a single line",
        "- You can call multiple tools in sequence",
        "- After each tool call, you will receive the result as [Tool Result]",
        "- NEVER show tool calls as code blocks or plain text - ALWAYS use [Calling tool: ...] format",
        "",
        "## Available Tools",
        "",
    ]

    for tool in tools:
        name = tool.get("name", "unknown")
        desc = tool.get("description", "")
        schema = tool.get("input_schema", {})

        lines.append(f"### {name}")
        if desc:
            # 截断过长描述
            if len(desc) > TOOL_DESC_MAX_CHARS:
                desc = desc[:TOOL_DESC_MAX_CHARS] + "..."
            lines.append(desc)

        # 添加参数信息
        props = schema.get("properties", {}) or {}
        required = schema.get("required") or []
        if props:
            lines.append("Parameters:")
            for pname, pschema in props.items():
                ptype = pschema.get("type", "any")
                pdesc = pschema.get("description", "")
                req_mark = " (required)" if pname in required else ""
                if pdesc:
                    # 截断参数描述
                    if len(pdesc) > TOOL_PARAM_DESC_MAX_CHARS:
                        pdesc = pdesc[:TOOL_PARAM_DESC_MAX_CHARS] + "..."
                    lines.append(f"  - {pname}: {ptype}{req_mark} - {pdesc}")
                else:
                    lines.append(f"  - {pname}: {ptype}{req_mark}")
        lines.append("")

    return "\n".join(lines)
```

### 3. 工具调用解析 (正则表达式)

```python
# 用于解析工具调用
_RE_TOOL_CALL = re.compile(r'\[Calling tool:\s*([^\]]+)\]')
_RE_INPUT_PREFIX = re.compile(r'^[\s]*Input:\s*')
```

## 工作流程

```
Claude Code CLI
    ↓
发送 Anthropic 格式请求 (包含 tools 数组)
    ↓
ai-history-manager
    ↓
convert_anthropic_to_openai()
    ↓
build_tool_instruction() → 生成文本指令
    ↓
注入到 system prompt
    ↓
发送到 Kiro (不带 tools 参数)
    ↓
模型返回文本: "[Calling tool: Read]\nInput: {...}"
    ↓
parse_inline_tool_calls() → 正则解析
    ↓
转换为 Anthropic tool_use 格式
    ↓
返回给 Claude Code CLI
```

## 缺点

1. **Token 消耗高**: tools 定义作为文本占用 system prompt 空间
2. **解析不稳定**: 依赖模型严格遵循 `[Calling tool:]` 格式
3. **JSON 解析复杂**: 需要处理各种边界情况 (换行、转义等)
4. **多工具调用困难**: 需要手动拼接和解析
5. **流式处理复杂**: 需要缓冲和检测完整的工具调用块

## 相关配置

```python
TOOL_DESC_MAX_CHARS = int(os.getenv("TOOL_DESC_MAX_CHARS", "8000"))
TOOL_PARAM_DESC_MAX_CHARS = int(os.getenv("TOOL_PARAM_DESC_MAX_CHARS", "4000"))
```

## 升级原因

Kiro 网关现已支持原生 OpenAI tools 格式，可以直接传递 tools 参数并获得结构化的 tool_calls 响应，无需文本注入和正则解析。
