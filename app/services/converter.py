import json
import uuid
import logging
import re
from typing import Optional, Union, Tuple
from app.core.config import (
    ANTHROPIC_CLEAN_SYSTEM_ENABLED, ANTHROPIC_MAX_SINGLE_CONTENT,
    ANTHROPIC_MAX_MESSAGES, ANTHROPIC_TRUNCATE_ENABLED,
    ANTHROPIC_TOOL_INPUT_MAX_CHARS, ANTHROPIC_TOOL_RESULT_MAX_CHARS,
    ANTHROPIC_CLEAN_ASSISTANT_ENABLED, ANTHROPIC_EMPTY_ASSISTANT_PLACEHOLDER,
    ANTHROPIC_MERGE_SAME_ROLE_ENABLED, ANTHROPIC_ENSURE_USER_ENDING,
    ANTHROPIC_MAX_TOTAL_CHARS, NATIVE_TOOLS_ENABLED,
    TOOL_DESC_MAX_CHARS, TOOL_PARAM_DESC_MAX_CHARS
)
from app.core.constants import (
    _RE_THINKING_TAG, _RE_THINKING_UNCLOSED, _RE_THINKING_UNOPEN,
    _RE_REDACTED_THINKING, _RE_SIGNATURE_TAG, _RE_TRAILING_COMMA_OBJ,
    _RE_TRAILING_COMMA_ARR, _RE_MARKDOWN_START, _RE_MARKDOWN_END,
    _RE_XML_TOOL_CALL, _RE_XML_PARAM, _RE_TOOL_CALL, _RE_INPUT_PREFIX,
    _RE_NEXT_MARKER
)

logger = logging.getLogger("ai_history_manager_api")

def extract_content_item(item: dict) -> str:
    """提取单个 content item 的文本表示"""
    item_type = item.get("type", "")

    if item_type == "text":
        return item.get("text", "")
    elif item_type == "image":
        source = item.get("source", {})
        if source.get("type") == "base64":
            media_type = source.get("media_type", "image")
            return f"[Image: {media_type}]"
        elif source.get("type") == "url":
            url = source.get("url", "")
            return f"[Image: {url[:50]}...]" if len(url) > 50 else f"[Image: {url}]"
        return "[Image]"
    elif item_type == "document":
        source = item.get("source", {})
        doc_type = source.get("media_type", "document")
        doc_name = item.get("name", "document")
        if "text" in item:
            return f"[Document: {doc_name}]\n{item.get('text', '')}"
        if "content" in item:
            doc_content = item.get("content", "")
            if isinstance(doc_content, str):
                return f"[Document: {doc_name}]\n{doc_content}"
        return f"[Document: {doc_name} ({doc_type})]"
    elif item_type == "file":
        file_name = item.get("name", item.get("filename", "file"))
        file_type = item.get("media_type", "")
        file_content = item.get("content", "")
        if file_content:
            if isinstance(file_content, str):
                return f"[File: {file_name}]\n{file_content}"
            elif isinstance(file_content, list):
                content_text = "\n".join(
                    extract_content_item(c) if isinstance(c, dict) else str(c)
                    for c in file_content
                )
                return f"[File: {file_name}]\n{content_text}"
        return f"[File: {file_name}]" + (f" ({file_type})" if file_type else "")
    elif item_type == "tool_result":
        tool_content = item.get("content", "")
        is_error = item.get("is_error", False)
        if isinstance(tool_content, list):
            tool_content = "\n".join(
                extract_content_item(c) if isinstance(c, dict) else str(c)
                for c in tool_content
            )
        elif isinstance(tool_content, dict):
            tool_content = extract_content_item(tool_content)
        prefix = "[Tool Error]" if is_error else "[Tool Result]"
        return f"{prefix}\n{tool_content}" if tool_content else prefix
    elif item_type == "thinking":
        return ""
    elif item_type == "redacted_thinking":
        return ""
    elif item_type == "signature":
        return ""
    elif item_type == "code_execution_result":
        output = item.get("output", "")
        return_code = item.get("return_code", 0)
        if return_code != 0:
            return f"[Code Execution Error (exit={return_code})]\n{output}"
        return f"[Code Execution Result]\n{output}" if output else ""
    elif item_type == "citation":
        cited_text = item.get("cited_text", "")
        source_name = item.get("source", {}).get("name", "source")
        return f"[Citation from {source_name}]: {cited_text}" if cited_text else ""
    elif item_type == "video":
        source = item.get("source", {})
        return f"[Video: {source.get('url', 'embedded')}]"
    elif item_type == "audio":
        source = item.get("source", {})
        return f"[Audio: {source.get('url', 'embedded')}]"
    else:
        if "text" in item:
            return item.get("text", "")
        if "content" in item:
            content = item.get("content", "")
            if isinstance(content, str):
                return content
        return f"[{item_type}]" if item_type else ""

def clean_system_content(content: str) -> str:
    """清理 system 消息内容"""
    if not content:
        return content
    lines = content.split('\n')
    cleaned_lines = []
    for line in lines:
        if ':' in line:
            key = line.split(':')[0].strip().lower()
            if key.startswith('x-') or key in [
                'content-type', 'authorization', 'user-agent',
                'accept', 'cache-control', 'cookie'
            ]:
                continue
        cleaned_lines.append(line)
    return '\n'.join(cleaned_lines).strip()

def clean_assistant_content(content: str) -> str:
    """清理 assistant 消息内容"""
    if not content:
        return content
    content = content.replace("(no content)", "").strip()
    content = _RE_THINKING_TAG.sub(r'\1', content)
    content = _RE_THINKING_UNCLOSED.sub('', content)
    content = _RE_THINKING_UNOPEN.sub('', content)
    content = _RE_REDACTED_THINKING.sub('', content)
    content = _RE_SIGNATURE_TAG.sub('', content)
    return content.strip() if content.strip() else " "

def convert_anthropic_to_openai(anthropic_body: dict) -> dict:
    """将 Anthropic 请求转换为 OpenAI 格式"""
    MAX_MESSAGES = ANTHROPIC_MAX_MESSAGES
    MAX_TOTAL_CHARS = ANTHROPIC_MAX_TOTAL_CHARS
    MAX_SINGLE_CONTENT = ANTHROPIC_MAX_SINGLE_CONTENT

    messages = []
    system = anthropic_body.get("system", "")
    if system:
        if isinstance(system, str):
            system_content = clean_system_content(system) if ANTHROPIC_CLEAN_SYSTEM_ENABLED else system
        elif isinstance(system, list):
            system_parts = []
            for item in system:
                if isinstance(item, dict):
                    extracted = extract_content_item(item)
                    if extracted:
                        system_parts.append(extracted)
                else:
                    system_parts.append(str(item))
            raw_system = "\n".join(filter(None, system_parts))
            system_content = clean_system_content(raw_system) if ANTHROPIC_CLEAN_SYSTEM_ENABLED else raw_system
        else:
            raw_system = str(system)
            system_content = clean_system_content(raw_system) if ANTHROPIC_CLEAN_SYSTEM_ENABLED else raw_system

        if system_content.strip():
            if len(system_content) > MAX_SINGLE_CONTENT:
                system_content = system_content[:MAX_SINGLE_CONTENT] + "\n...[truncated]"
            messages.append({"role": "system", "content": system_content})

    raw_messages = anthropic_body.get("messages", [])
    if ANTHROPIC_TRUNCATE_ENABLED and len(raw_messages) > MAX_MESSAGES:
        raw_messages = raw_messages[-MAX_MESSAGES:]

    converted_messages = []
    for msg in raw_messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if isinstance(content, list):
            text_parts = []
            for item in content:
                if isinstance(item, dict):
                    item_type = item.get("type", "")
                    if item_type == "tool_use":
                        tool_name = item.get("name", "unknown")
                        tool_input = item.get("input", {})
                        input_str = json.dumps(tool_input, ensure_ascii=False)
                        if ANTHROPIC_TRUNCATE_ENABLED and len(input_str) > ANTHROPIC_TOOL_INPUT_MAX_CHARS:
                            input_str = input_str[:ANTHROPIC_TOOL_INPUT_MAX_CHARS] + "...[truncated]"
                        text_parts.append(f"[Calling tool: {tool_name}]\nInput: {input_str}")
                    elif item_type == "tool_result":
                        tool_content = item.get("content", "")
                        is_error = item.get("is_error", False)
                        if isinstance(tool_content, list):
                            parts = []
                            for c in tool_content:
                                if isinstance(c, dict):
                                    if c.get("type") == "text":
                                        parts.append(c.get("text", ""))
                                    else:
                                        extracted = extract_content_item(c)
                                        if extracted:
                                            if extracted.startswith(("[Tool Result]\n", "[Tool Error]\n")):
                                                extracted = extracted.split("\n", 1)[1]
                                            parts.append(extracted)
                                else:
                                    parts.append(str(c))
                            tool_content = "\n".join(filter(None, parts))
                        elif isinstance(tool_content, dict):
                            tool_content = extract_content_item(tool_content)
                            if isinstance(tool_content, str) and tool_content.startswith(("[Tool Result]\n", "[Tool Error]\n")):
                                tool_content = tool_content.split("\n", 1)[1]
                        if not tool_content:
                            tool_content = "Error" if is_error else "OK"
                        prefix = "[Tool Error]" if is_error else "[Tool Result]"
                        if ANTHROPIC_TRUNCATE_ENABLED and len(tool_content) > ANTHROPIC_TOOL_RESULT_MAX_CHARS:
                            tool_content = tool_content[:ANTHROPIC_TOOL_RESULT_MAX_CHARS] + "\n...[truncated]"
                        text_parts.append(f"{prefix}\n{tool_content}")
                    elif item_type == "thinking":
                        pass
                    else:
                        extracted = extract_content_item(item)
                        if extracted:
                            text_parts.append(extracted)
                else:
                    text_parts.append(str(item))
            content = "\n".join(filter(None, text_parts))
            if role == "assistant" and ANTHROPIC_CLEAN_ASSISTANT_ENABLED:
                content = clean_assistant_content(content)
            if ANTHROPIC_TRUNCATE_ENABLED and len(content) > MAX_SINGLE_CONTENT:
                content = content[:MAX_SINGLE_CONTENT] + "\n...[truncated]"
            if content.strip():
                converted_messages.append({"role": role, "content": content})
            elif role == "assistant":
                converted_messages.append({"role": "assistant", "content": ANTHROPIC_EMPTY_ASSISTANT_PLACEHOLDER})
        else:
            if role == "assistant" and ANTHROPIC_CLEAN_ASSISTANT_ENABLED:
                content = clean_assistant_content(content)
            if ANTHROPIC_TRUNCATE_ENABLED and len(content) > MAX_SINGLE_CONTENT:
                content = content[:MAX_SINGLE_CONTENT] + "\n...[truncated]"
            if content.strip():
                converted_messages.append({"role": role, "content": content})

    if ANTHROPIC_MERGE_SAME_ROLE_ENABLED:
        merged_messages = []
        for msg in converted_messages:
            role = msg.get("role")
            if merged_messages and merged_messages[-1].get("role") == role:
                merged_messages[-1]["content"] += "\n" + msg.get("content", "")
            else:
                merged_messages.append(msg.copy())
        final_messages = merged_messages
    else:
        final_messages = converted_messages

    messages.extend(final_messages)
    if not messages:
        messages.append({"role": "user", "content": "Hello"})
    if len(messages) == 1 and messages[0]["role"] == "system":
        messages.append({"role": "user", "content": "Hello"})
    if ANTHROPIC_ENSURE_USER_ENDING and messages and messages[-1].get("role") == "tool":
        messages.append({"role": "user", "content": "Please continue based on the tool results above."})
    if ANTHROPIC_ENSURE_USER_ENDING and messages and messages[-1].get("role") == "assistant":
        messages.append({"role": "user", "content": "Please continue."})

    if ANTHROPIC_TRUNCATE_ENABLED:
        total_chars = sum(len(m.get("content", "")) for m in messages)
        while total_chars > MAX_TOTAL_CHARS and len(messages) > 2:
            if messages[0].get("role") == "system":
                if len(messages) > 2:
                    messages.pop(1)
            else:
                messages.pop(0)
            total_chars = sum(len(m.get("content", "")) for m in messages)

    openai_body = {
        "model": anthropic_body.get("model", "claude-sonnet-4"),
        "messages": messages,
        "stream": anthropic_body.get("stream", False),
    }
    if anthropic_body.get("stream", False):
        openai_body["stream_options"] = {"include_usage": True}
    if "max_tokens" in anthropic_body:
        openai_body["max_tokens"] = anthropic_body["max_tokens"]
    if "temperature" in anthropic_body:
        openai_body["temperature"] = anthropic_body["temperature"]
    if "top_p" in anthropic_body:
        openai_body["top_p"] = anthropic_body["top_p"]
    if "stop_sequences" in anthropic_body:
        openai_body["stop"] = anthropic_body["stop_sequences"]

    anthropic_tools = anthropic_body.get("tools", [])
    if anthropic_tools:
        if NATIVE_TOOLS_ENABLED:
            openai_body["tools"] = convert_anthropic_tools_to_openai(anthropic_tools)
            if "tool_choice" in anthropic_body:
                openai_tool_choice = convert_anthropic_tool_choice_to_openai(anthropic_body["tool_choice"])
                if openai_tool_choice:
                    openai_body["tool_choice"] = openai_tool_choice
        else:
            tool_instruction = build_tool_instruction(anthropic_tools)
            for msg in openai_body["messages"]:
                if msg.get("role") == "system":
                    msg["content"] = msg["content"] + "\n\n" + tool_instruction
                    break
            else:
                openai_body["messages"].insert(0, {"role": "system", "content": tool_instruction})
    return openai_body

def convert_anthropic_tools_to_openai(anthropic_tools: list) -> list:
    openai_tools = []
    for tool in anthropic_tools:
        openai_tool = {
            "type": "function",
            "function": {
                "name": tool.get("name", ""),
                "description": tool.get("description", ""),
                "parameters": tool.get("input_schema", {})
            }
        }
        openai_tools.append(openai_tool)
    return openai_tools

def convert_anthropic_tool_choice_to_openai(tool_choice) -> Optional[Union[str, dict]]:
    if not tool_choice:
        return None
    tc_type = tool_choice.get("type", "")
    if tc_type == "auto":
        return "auto"
    elif tc_type == "any":
        return "required"
    elif tc_type == "tool":
        return {"type": "function", "function": {"name": tool_choice.get("name", "")}}
    return None

def build_tool_instruction(tools: list) -> str:
    lines = [
        "# Tool Call Format", "",
        "You have access to the following tools. To call a tool, output EXACTLY this format:", "",
        "[Calling tool: tool_name]", "Input: {\"param\": \"value\"}", "",
        "IMPORTANT RULES:",
        "- You MUST use the exact format above to call tools",
        "- The Input MUST be valid JSON on a single line",
        "- You can call multiple tools in sequence",
        "- After each tool call, you will receive the result as [Tool Result]",
        "- NEVER show tool calls as code blocks or plain text - ALWAYS use [Calling tool: ...] format", "",
        "## Available Tools", ""
    ]
    for tool in tools:
        name = tool.get("name", "unknown")
        desc = tool.get("description", "")
        schema = tool.get("input_schema", {})
        lines.append(f"### {name}")
        if desc:
            if len(desc) > TOOL_DESC_MAX_CHARS:
                desc = desc[:TOOL_DESC_MAX_CHARS] + "..."
            lines.append(desc)
        props = schema.get("properties", {}) or {}
        required = schema.get("required") or []
        if props:
            lines.append("Parameters:")
            for pname, pschema in props.items():
                ptype = pschema.get("type", "any")
                pdesc = pschema.get("description", "")
                req_mark = " (required)" if pname in required else ""
                if pdesc:
                    if len(pdesc) > TOOL_PARAM_DESC_MAX_CHARS:
                        pdesc = pdesc[:TOOL_PARAM_DESC_MAX_CHARS] + "..."
                    lines.append(f"  - {pname}: {ptype}{req_mark} - {pdesc}")
                else:
                    lines.append(f"  - {pname}: {ptype}{req_mark}")
        lines.append("")
    return "\n".join(lines)

def convert_openai_to_anthropic(openai_response: dict, model: str, request_id: str) -> dict:
    choice = openai_response.get("choices", [{}])[0]
    message = choice.get("message", {})
    content = message.get("content", "")
    finish_reason = choice.get("finish_reason", "stop")
    content_blocks = []
    stop_reason = "end_turn"
    tool_calls = message.get("tool_calls", [])
    if tool_calls:
        if content:
            blocks = expand_thinking_blocks([{"type": "text", "text": content}])
            for block in blocks:
                if block.get("type") == "text":
                    text_value = block.get("text", "")
                    if text_value and text_value.strip(): content_blocks.append({"type": "text", "text": text_value})
                elif block.get("type") == "thinking":
                    content_blocks.append({"type": "thinking", "thinking": block.get("thinking", "")})
        content_blocks.extend(tool_calls_to_blocks(tool_calls))
        stop_reason = "tool_use"
    elif content:
        blocks = parse_inline_tool_blocks(content)
        blocks = expand_thinking_blocks(blocks)
        for block in blocks:
            if block.get("type") == "text":
                text_value = block.get("text", "")
                if text_value: content_blocks.append({"type": "text", "text": text_value})
            elif block.get("type") == "thinking":
                content_blocks.append({"type": "thinking", "thinking": block.get("thinking", "")})
            elif block.get("type") == "tool_use":
                content_blocks.append(block)
                stop_reason = "tool_use"
    if not content_blocks: content_blocks = [{"type": "text", "text": ""}]
    if finish_reason == "tool_calls": stop_reason = "tool_use"
    elif finish_reason == "length": stop_reason = "end_turn"
    elif finish_reason == "stop" and stop_reason != "tool_use": stop_reason = "end_turn"
    return {
        "id": f"msg_{request_id}", "type": "message", "role": "assistant",
        "content": content_blocks, "model": model, "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": openai_response.get("usage", {}).get("prompt_tokens", 0),
            "output_tokens": openai_response.get("usage", {}).get("completion_tokens", 0),
            "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
        }
    }

def escape_json_string_newlines(json_str: str) -> str:
    result = []
    in_string = False
    escape = False
    i = 0
    while i < len(json_str):
        c = json_str[i]
        if escape:
            result.append(c)
            escape = False
            i += 1
            continue
        if c == '\\':
            result.append(c)
            escape = True
            i += 1
            continue
        if c == '"':
            in_string = not in_string
            result.append(c)
            i += 1
            continue
        if in_string:
            if c == '\n': result.append('\\n')
            elif c == '\r': result.append('\\r')
            elif c == '\t': result.append('\\t')
            elif ord(c) < 32: result.append(f'\\u{ord(c):04x}')
            else: result.append(c)
        else:
            result.append(c)
        i += 1
    return ''.join(result)

def _try_parse_json(json_str: str, end_pos: int) -> tuple[dict, int]:
    try:
        return json.loads(json_str), end_pos
    except json.JSONDecodeError:
        pass
    return _try_repair_json(json_str, end_pos)

def _try_repair_json(json_str: str, end_pos: int) -> tuple[dict, int]:
    try:
        fixed = _RE_TRAILING_COMMA_OBJ.sub('}', json_str)
        fixed = _RE_TRAILING_COMMA_ARR.sub(']', fixed)
        return json.loads(fixed), end_pos
    except json.JSONDecodeError:
        pass
    try:
        fixed = escape_json_string_newlines(json_str)
        return json.loads(fixed), end_pos
    except json.JSONDecodeError:
        pass
    try:
        fixed = escape_json_string_newlines(json_str)
        fixed = _RE_TRAILING_COMMA_OBJ.sub('}', fixed)
        fixed = _RE_TRAILING_COMMA_ARR.sub(']', fixed)
        return json.loads(fixed), end_pos
    except json.JSONDecodeError:
        pass
    try:
        quote_count = json_str.count('"') - json_str.count('\\"')
        if quote_count % 2 == 1:
            fixed = json_str.rstrip()
            if not fixed.endswith('"'): fixed = fixed + '"'
            open_braces = fixed.count('{') - fixed.count('}')
            if open_braces > 0: fixed = fixed + '}' * open_braces
            return json.loads(fixed), end_pos
    except json.JSONDecodeError:
        pass
    try:
        decoder = json.JSONDecoder()
        obj, idx = decoder.raw_decode(json_str)
        return obj, end_pos
    except json.JSONDecodeError:
        pass
    raise json.JSONDecodeError("Failed to parse JSON after all recovery attempts", json_str, 0)

def extract_json_from_position(text: str, start: int) -> tuple[dict, int]:
    pos = start
    while pos < len(text) and text[pos] in ' \t\n\r': pos += 1
    markdown_match = _RE_MARKDOWN_START.match(text[pos:])
    is_markdown_wrapped = False
    if markdown_match:
        is_markdown_wrapped = True
        pos += markdown_match.end()
        while pos < len(text) and text[pos] in ' \t\n\r': pos += 1
    if pos >= len(text) or text[pos] != '{':
        raise ValueError(f"No JSON object found at position {start}")
    depth = 0
    in_string = False
    escape = False
    json_start = pos
    while pos < len(text):
        c = text[pos]
        if escape:
            escape = False
            pos += 1
            continue
        if c == '\\' and in_string:
            escape = True
            pos += 1
            continue
        if c == '"' and not escape:
            in_string = not in_string
            pos += 1
            continue
        if in_string:
            pos += 1
            continue
        if c == '{': depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                json_str = text[json_start:pos + 1]
                parsed_json, _ = _try_parse_json(json_str, pos + 1)
                end_pos = pos + 1
                if is_markdown_wrapped:
                    remaining = text[end_pos:]
                    end_match = _RE_MARKDOWN_END.search(remaining)
                    if end_match: end_pos += end_match.end()
                return parsed_json, end_pos
        pos += 1
    incomplete_json = text[json_start:]
    if depth > 0:
        repaired_json = incomplete_json
        if in_string: repaired_json += '"'
        repaired_json += '}' * depth
        try:
            parsed_json, _ = _try_parse_json(repaired_json, len(text))
            logger.warning(f"JSON was incomplete (depth={depth}), auto-repaired successfully")
            return parsed_json, len(text)
        except Exception: pass
    for i in range(len(text) - 1, json_start, -1):
        if text[i] == '}':
            try:
                candidate = text[json_start:i+1]
                parsed_json, _ = _try_parse_json(candidate, i + 1)
                return parsed_json, i + 1
            except Exception: continue
    raise ValueError("Incomplete or malformed JSON object")

def iter_text_chunks(text: str, chunk_size: int):
    """将文本分块，用于流式输出"""
    if chunk_size <= 0:
        yield text
        return
    for i in range(0, len(text), chunk_size):
        yield text[i:i + chunk_size]

def split_thinking_blocks(text: str) -> list[dict]:
    if not text: return []
    lower = text.lower()
    open_pos = lower.rfind("<thinking>")
    close_pos = lower.rfind("</thinking>")
    if open_pos != -1 and (close_pos == -1 or close_pos < open_pos):
        prefix = text[:open_pos]
        thinking = text[open_pos + len("<thinking>"):]
        blocks = []
        if prefix and prefix.strip(): blocks.append({"type": "text", "text": prefix})
        if thinking and thinking.strip(): blocks.append({"type": "thinking", "thinking": thinking})
        return blocks
    blocks = []
    pattern = re.compile(r"<thinking>(.*?)</thinking>", re.IGNORECASE | re.DOTALL)
    last_end = 0
    for match in pattern.finditer(text):
        if match.start() > last_end:
            prefix = text[last_end:match.start()]
            if prefix and prefix.strip(): blocks.append({"type": "text", "text": prefix})
        thinking_text = match.group(1)
        if thinking_text and thinking_text.strip():
            blocks.append({"type": "thinking", "thinking": thinking_text})
        last_end = match.end()
    if last_end < len(text):
        suffix = text[last_end:]
        if suffix and suffix.strip(): blocks.append({"type": "text", "text": suffix})
    return blocks

def expand_thinking_blocks(blocks: list[dict]) -> list[dict]:
    expanded = []
    for block in blocks:
        if block.get("type") == "text":
            text_value = block.get("text", "")
            split_blocks = split_thinking_blocks(text_value)
            expanded.extend(split_blocks or [])
        else:
            expanded.append(block)
    return expanded

def tool_calls_to_blocks(tool_calls: list) -> list[dict]:
    blocks = []
    for tc in tool_calls or []:
        func = tc.get("function", {}) or {}
        name = func.get("name") or tc.get("name") or "unknown"
        args_str = func.get("arguments") or tc.get("arguments") or ""
        tool_id = tc.get("id") or f"toolu_{uuid.uuid4().hex[:12]}"
        if not args_str: parsed_input = {}
        else:
            try: parsed_input = json.loads(args_str)
            except json.JSONDecodeError:
                try: parsed_input = _try_parse_json(args_str, len(args_str))[0]
                except Exception as e: parsed_input = {"_raw": args_str, "_parse_error": str(e)}
        blocks.append({"type": "tool_use", "id": tool_id, "name": name, "input": parsed_input})
    return blocks

def parse_xml_tool_params(xml_content: str) -> dict:
    params = {}
    for match in _RE_XML_PARAM.finditer(xml_content):
        param_name = match.group(1)
        param_value = match.group(2).strip()
        try: params[param_name] = json.loads(param_value)
        except (json.JSONDecodeError, ValueError): params[param_name] = param_value
    return params

def parse_xml_tool_blocks(text: str) -> list[dict]:
    blocks = []
    last_end = 0
    for match in _RE_XML_TOOL_CALL.finditer(text):
        before_text = text[last_end:match.start()]
        if before_text and before_text.strip(): blocks.append({"type": "text", "text": before_text})
        tool_name = match.group(1)
        xml_content = match.group(2)
        params = parse_xml_tool_params(xml_content)
        blocks.append({"type": "tool_use", "id": f"toolu_{uuid.uuid4().hex[:12]}", "name": tool_name, "input": params})
        last_end = match.end()
    if last_end < len(text):
        remaining = text[last_end:]
        if remaining and remaining.strip(): blocks.append({"type": "text", "text": remaining})
    return blocks

def parse_inline_tool_blocks(text: str) -> list[dict]:
    blocks = []
    last_end = 0
    pos = 0
    while pos < len(text):
        match = _RE_TOOL_CALL.search(text[pos:])
        if not match: break
        match_start = pos + match.start()
        match_end = pos + match.end()
        before_text = text[last_end:match_start]
        if before_text and before_text.strip(): blocks.append({"type": "text", "text": before_text})
        tool_name = match.group(1).strip()
        after_match = text[match_end:]
        input_match = _RE_INPUT_PREFIX.match(after_match)
        if input_match:
            json_start_pos = match_end + input_match.end()
            try:
                input_json, json_end_pos = extract_json_from_position(text, json_start_pos)
                blocks.append({"type": "tool_use", "id": f"toolu_{uuid.uuid4().hex[:12]}", "name": tool_name, "input": input_json})
                last_end = json_end_pos
                pos = json_end_pos
                continue
            except Exception as e:
                logger.warning(f"JSON parse failed for tool {tool_name} at pos {json_start_pos}: {e}")
                next_marker = _RE_NEXT_MARKER.search(after_match[input_match.end():])
                if next_marker: raw_text = after_match[input_match.end():input_match.end() + next_marker.start()].strip()
                else: raw_text = after_match[input_match.end():].strip()
                try:
                    input_json, _ = _try_parse_json(raw_text, 0)
                    blocks.append({"type": "tool_use", "id": f"toolu_{uuid.uuid4().hex[:12]}", "name": tool_name, "input": input_json})
                    last_end = match_end + input_match.end() + len(raw_text)
                    pos = last_end
                    continue
                except Exception as e:
                    blocks.append({"type": "tool_use", "id": f"toolu_{uuid.uuid4().hex[:12]}", "name": tool_name, "input": {"_raw": raw_text[:2000], "_parse_error": str(e)}})
                    last_end = match_end + input_match.end() + len(raw_text)
                    pos = last_end
                    continue
        marker_text = text[match_start:match_end]
        if marker_text and marker_text.strip(): blocks.append({"type": "text", "text": marker_text})
        last_end = match_end
        pos = match_end
    if last_end < len(text):
        remaining = text[last_end:]
        if remaining and remaining.strip(): blocks.append({"type": "text", "text": remaining})
    has_tool_use = any(b.get("type") == "tool_use" for b in blocks)
    if not has_tool_use and _RE_XML_TOOL_CALL.search(text):
        return parse_xml_tool_blocks(text)
    return blocks

def parse_inline_tool_calls(text: str) -> tuple[list, str]:
    blocks = parse_inline_tool_blocks(text)
    tool_uses = [b for b in blocks if b.get("type") == "tool_use"]
    remaining_parts = []
    for block in blocks:
        if block.get("type") == "text":
            text_part = block.get("text", "").strip()
            if text_part: remaining_parts.append(text_part)
    remaining_text = "\n".join(remaining_parts)
    return tool_uses, remaining_text
