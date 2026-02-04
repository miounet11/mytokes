import json
import logging
from functools import lru_cache
from typing import Union

logger = logging.getLogger("ai_history_manager_api")

# Token 估算缓存 - 避免对相同文本重复计算
@lru_cache(maxsize=2048)
def _estimate_tokens_cached(text_hash: int, text_len: int, chinese_ratio_pct: int) -> int:
    """基于文本特征的 token 估算（带缓存）"""
    chinese_chars = int(text_len * chinese_ratio_pct / 100)
    other_chars = text_len - chinese_chars

    # 中文约 1.5 字符/token，其他约 4 字符/token
    chinese_tokens = chinese_chars / 1.5
    other_tokens = other_chars / 4

    return int(chinese_tokens + other_tokens)

def estimate_tokens(text: str) -> int:
    """估算文本的 token 数量（优化版，带缓存）"""
    if not text:
        return 0

    text_len = len(text)

    # 短文本直接计算，避免缓存开销
    if text_len < 100:
        chinese_chars = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
        other_chars = text_len - chinese_chars
        return int(chinese_chars / 1.5 + other_chars / 4)

    # 统计中文字符数并计算占比
    chinese_chars = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    chinese_ratio_pct = int(chinese_chars * 100 / text_len) if text_len > 0 else 0

    # 使用文本哈希作为缓存键
    text_hash = hash(text)

    return _estimate_tokens_cached(text_hash, text_len, chinese_ratio_pct)

def estimate_messages_tokens(messages: list, system: Union[str, list] = "") -> int:
    """估算消息列表的总 token 数"""
    total = 0

    # system prompt
    if system:
        if isinstance(system, str):
            total += estimate_tokens(system)
        elif isinstance(system, list):
            for item in system:
                if isinstance(item, dict):
                    total += estimate_tokens(item.get("text", ""))
                elif isinstance(item, str):
                    total += estimate_tokens(item)

    # messages
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total += estimate_tokens(content)
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, dict):
                    if item.get("type") == "text":
                        total += estimate_tokens(item.get("text", ""))
                    elif item.get("type") == "tool_use":
                        total += estimate_tokens(json.dumps(item.get("input", {})))
                    elif item.get("type") == "tool_result":
                        result = item.get("content", "")
                        if isinstance(result, str):
                            total += estimate_tokens(result)
                        elif isinstance(result, list):
                            for r in result:
                                if isinstance(r, dict):
                                    total += estimate_tokens(r.get("text", ""))
                elif isinstance(item, str):
                    total += estimate_tokens(item)

        # 每条消息额外开销（role, formatting等）
        total += 4

    return total
