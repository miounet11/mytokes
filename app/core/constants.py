import re

# ==================== 预编译正则表达式 ====================

# 用于清理 assistant 内容
_RE_THINKING_TAG = re.compile(r'<thinking>(.*?)</thinking>', re.IGNORECASE | re.DOTALL)
_RE_THINKING_UNCLOSED = re.compile(r'<thinking>.*$', re.DOTALL)
_RE_THINKING_UNOPEN = re.compile(r'^.*</thinking>', re.DOTALL)
_RE_REDACTED_THINKING = re.compile(r'<redacted_thinking>.*?</redacted_thinking>', re.DOTALL)
_RE_SIGNATURE_TAG = re.compile(r'<signature>.*?</signature>', re.DOTALL)

# 用于解析工具调用
_RE_TOOL_CALL = re.compile(r'\[Calling tool:\s*([^\]]+)\]')
_RE_INPUT_PREFIX = re.compile(r'^[\s]*Input:\s*')
_RE_MARKDOWN_START = re.compile(r'```(?:json)?\s*')
_RE_MARKDOWN_END = re.compile(r'\s*```')

# 用于 JSON 修复
_RE_TRAILING_COMMA_OBJ = re.compile(r',\s*}')
_RE_TRAILING_COMMA_ARR = re.compile(r',\s*]')

# 用于合并响应时的清理
_RE_CONTINUATION_INTRO = [
    re.compile(r"^Continuing from.*?:", re.IGNORECASE | re.DOTALL),
    re.compile(r"^Here is the rest of the response:", re.IGNORECASE),
    re.compile(r"^Continuing the JSON:", re.IGNORECASE),
    re.compile(r"^```json\s*"),
    re.compile(r"^```\s*"),
]

# 用于检测下一个标记
_RE_NEXT_MARKER = re.compile(r'\[Calling tool:|\[Tool Result\]|\[Tool Error\]')

# 用于解析 XML 格式的工具调用 (Kiro 返回格式)
_RE_XML_TOOL_CALL = re.compile(r'<([A-Z][a-zA-Z0-9_]*)>([\s\S]*?)</\1>')
# 匹配 XML 参数 <param_name>value</param_name>
_RE_XML_PARAM = re.compile(r'<([a-z_][a-z0-9_]*)>([\s\S]*?)</\1>', re.IGNORECASE)

# 用于文件路径匹配
_RE_FILE_PATH = re.compile(r'[/\\][\w\-\.]+\.(py|js|ts|jsx|tsx|go|rs|java|cpp|c|h|md|yaml|yml|json|toml)')
