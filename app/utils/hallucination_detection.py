"""幻觉检测模块 - 检测并清理 AI 生成的虚假工具结果"""
import re
import logging

logger = logging.getLogger(__name__)

# 预编译正则表达式
# 模式1: 检测工具调用后紧跟虚假 Tool Result
HALLUCINATION_PATTERN = re.compile(
    r"""\[Calling\s+tool:\s*([^\]]+)\]\s*"""  # 工具调用
    r"""Input:\s*\{[^}]*\}\s*"""              # Input JSON
    r"""\[Tool\s+Result\]""",                  # 虚假结果标记
    re.DOTALL
)

# 模式2: 检测末尾不完整的工具调用
INCOMPLETE_PATTERN = re.compile(
    r"""\[Calling\s+tool:\s*([^\]]+)\]\s*$""",
    re.MULTILINE
)


def detect_hallucinated_tool_result(text: str, request_id: str) -> tuple[bool, str, str]:
    """检测幻觉工具结果
    
    检测模式: AI 生成工具调用后立即生成虚假的 Tool Result
    正常流程: 工具调用 -> 系统执行 -> 返回真实结果
    幻觉流程: 工具调用 -> AI 自己生成假结果
    
    Returns:
        (has_hallucination, cleaned_text, reason)
    """
    # 检测幻觉模式
    match = HALLUCINATION_PATTERN.search(text)
    if match:
        tool_name = match.group(1).strip()
        start_pos = match.start()
        cleaned = text[:start_pos].rstrip()
        logger.warning(f"[{request_id}] 检测到幻觉工具结果: tool={tool_name}, pos={start_pos}")
        return True, cleaned, f"检测到幻觉工具结果: {tool_name}"
    
    # 检测末尾不完整的工具调用
    last_500 = text[-500:] if len(text) > 500 else text
    match2 = INCOMPLETE_PATTERN.search(last_500)
    if match2:
        tool_name = match2.group(1).strip()
        full_match = INCOMPLETE_PATTERN.search(text)
        if full_match and full_match.start() > len(text) - 600:
            cleaned = text[:full_match.start()].rstrip()
            logger.info(f"[{request_id}] 清理不完整工具调用: {tool_name}")
            return False, cleaned, f"清理不完整工具调用: {tool_name}"
    
    return False, text, "无幻觉"
