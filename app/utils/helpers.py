import json
import logging
from typing import Optional, Union
from app.utils.token_utils import estimate_tokens

logger = logging.getLogger("ai_history_manager_api")

def count_tokens_logic(body: dict) -> int:
    """Token 计数逻辑"""
    total_chars = 0

    # 计算 system
    system = body.get("system", "")
    if isinstance(system, str):
        total_chars += len(system)
    elif isinstance(system, list):
        for item in system:
            if isinstance(item, dict) and "text" in item:
                total_chars += len(item["text"])

    # 计算 messages
    for msg in body.get("messages", []):
        content = msg.get("content", "")
        if isinstance(content, str):
            total_chars += len(content)
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, dict):
                    if item.get("type") == "text":
                        total_chars += len(item.get("text", ""))

    # 计算 tools
    tools = body.get("tools", [])
    for tool in tools:
        total_chars += len(json.dumps(tool))

    return total_chars // 4
