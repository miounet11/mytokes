"""结构分析工具

提供历史消息结构分析和格式化功能。
"""

from typing import Any


def extract_text(content: Any) -> str:
    """从消息内容中提取纯文本

    支持多种消息格式：
    - 字符串
    - 列表（Anthropic 风格）
    - 字典（包含 text 或 content 字段）

    Args:
        content: 消息内容

    Returns:
        提取的纯文本
    """
    if isinstance(content, str):
        return content

    if isinstance(content, list):
        texts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                texts.append(item.get("text", ""))
            elif isinstance(item, str):
                texts.append(item)
            else:
                texts.append(extract_text(item))
        return "\n".join(filter(None, texts))

    if isinstance(content, dict):
        # 优先检查 text 字段
        if "text" in content and isinstance(content.get("text"), str):
            return content["text"]
        # 其次检查 content 字段
        if "content" in content:
            return extract_text(content["content"])

    return str(content) if content else ""


def format_history_for_summary(history: list[dict], max_chars_per_message: int = 500) -> str:
    """格式化历史消息用于生成摘要

    Args:
        history: 历史消息列表
        max_chars_per_message: 每条消息最大字符数

    Returns:
        格式化后的文本
    """
    lines = []

    for msg in history:
        role = "unknown"
        content = ""

        # Kiro 格式
        if "userInputMessage" in msg:
            role = "user"
            content = msg.get("userInputMessage", {}).get("content", "")
        elif "assistantResponseMessage" in msg:
            role = "assistant"
            content = msg.get("assistantResponseMessage", {}).get("content", "")
        # OpenAI/Anthropic 格式
        else:
            role = msg.get("role", "unknown")
            content = extract_text(msg.get("content", ""))

        # 截断过长的单条消息
        if len(content) > max_chars_per_message:
            content = content[:max_chars_per_message] + "..."

        lines.append(f"[{role}]: {content}")

    return "\n".join(lines)


def _get_entry_kind(msg: dict) -> str:
    """提取消息类型标识

    Args:
        msg: 消息字典

    Returns:
        类型标识：U=用户, A=助手, ?=未知
    """
    # Kiro 格式
    if "userInputMessage" in msg:
        return "U"
    if "assistantResponseMessage" in msg:
        return "A"

    # OpenAI/Anthropic 格式
    role = msg.get("role")
    if role == "user":
        return "U"
    if role == "assistant":
        return "A"

    return "?"


def summarize_history_structure(history: list[dict], max_items: int = 12) -> str:
    """生成历史结构摘要（用于调试）

    Args:
        history: 历史消息列表
        max_items: 序列最大显示长度

    Returns:
        结构摘要字符串
    """
    if not history:
        return "len=0"

    kinds = [_get_entry_kind(msg) for msg in history]

    # 统计各类型数量
    counts = {"U": 0, "A": 0, "?": 0}
    for k in kinds:
        counts[k] = counts.get(k, 0) + 1

    # 检查是否交替
    alternating = True
    for i in range(1, len(kinds)):
        if kinds[i] == kinds[i - 1] or kinds[i] == "?" or kinds[i - 1] == "?":
            alternating = False
            break

    # 统计工具调用
    tool_uses = 0
    tool_results = 0

    for msg in history:
        if "assistantResponseMessage" in msg:
            tool_uses += len(msg["assistantResponseMessage"].get("toolUses", []) or [])
        if "userInputMessage" in msg:
            ctx = msg["userInputMessage"].get("userInputMessageContext", {})
            tool_results += len(ctx.get("toolResults", []) or [])

    # 生成序列字符串
    if len(kinds) <= max_items:
        seq = "".join(kinds)
    else:
        head_len = max_items // 2
        tail_len = max_items - head_len
        seq = f"{''.join(kinds[:head_len])}...{''.join(kinds[-tail_len:])}"

    return (
        f"len={len(history)} seq={seq} alt={'yes' if alternating else 'no'} "
        f"U={counts['U']} A={counts['A']} ?={counts['?']} "
        f"tool_uses={tool_uses} tool_results={tool_results}"
    )


def validate_history_alternation(history: list[dict]) -> tuple[bool, list[str]]:
    """验证历史消息交替性

    检查：
    1. 用户/助手消息是否交替
    2. toolUses 和 toolResults 是否配对

    Args:
        history: 历史消息列表

    Returns:
        (是否有效, 问题列表)
    """
    issues = []
    kinds = [_get_entry_kind(msg) for msg in history]

    # 检查交替
    for i in range(1, len(kinds)):
        if kinds[i] == kinds[i - 1] and kinds[i] != "?":
            issues.append(f"Position {i}: consecutive {kinds[i]} messages")

    # 检查 toolUses/toolResults 配对
    pending_tool_uses = set()

    for i, msg in enumerate(history):
        if "assistantResponseMessage" in msg:
            for tu in msg["assistantResponseMessage"].get("toolUses", []) or []:
                tu_id = tu.get("toolUseId")
                if tu_id:
                    pending_tool_uses.add(tu_id)

        if "userInputMessage" in msg:
            ctx = msg["userInputMessage"].get("userInputMessageContext", {})
            for tr in ctx.get("toolResults", []) or []:
                tr_id = tr.get("toolUseId")
                if tr_id:
                    if tr_id in pending_tool_uses:
                        pending_tool_uses.discard(tr_id)
                    else:
                        issues.append(f"Position {i}: orphan toolResult {tr_id}")

    if pending_tool_uses:
        issues.append(f"Unmatched toolUses: {pending_tool_uses}")

    return len(issues) == 0, issues
