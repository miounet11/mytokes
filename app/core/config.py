import os
import logging
from ai_history_manager import HistoryConfig, TruncateStrategy

# ==================== 基础配置 ====================

# Kiro 代理地址 (tokens 网关, 使用内网地址)
KIRO_PROXY_BASE = os.getenv("KIRO_PROXY_BASE", "http://127.0.0.1:8000")
KIRO_PROXY_URL = f"{KIRO_PROXY_BASE}/kiro/v1/chat/completions"
KIRO_MODELS_URL = f"{KIRO_PROXY_BASE}/kiro/v1/models"
KIRO_API_KEY = os.getenv("KIRO_API_KEY", "dba22273-65d3-4dc1-8ce9-182f680b2bf5")

# 服务配置
SERVICE_PORT = int(os.getenv("SERVICE_PORT", "8100"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "300"))
HTTP_CONNECT_TIMEOUT = float(os.getenv("HTTP_CONNECT_TIMEOUT", "10"))
HTTP_READ_TIMEOUT = float(os.getenv("HTTP_READ_TIMEOUT", str(REQUEST_TIMEOUT)))
HTTP_WRITE_TIMEOUT = float(os.getenv("HTTP_WRITE_TIMEOUT", str(REQUEST_TIMEOUT)))
HTTP_POOL_TIMEOUT = float(os.getenv("HTTP_POOL_TIMEOUT", "5"))

# HTTP 连接池配置
HTTP_POOL_MAX_CONNECTIONS = int(os.getenv("HTTP_POOL_MAX_CONNECTIONS", "2000"))
HTTP_POOL_MAX_KEEPALIVE = int(os.getenv("HTTP_POOL_MAX_KEEPALIVE", "500"))
HTTP_POOL_KEEPALIVE_EXPIRY = int(os.getenv("HTTP_POOL_KEEPALIVE_EXPIRY", "30"))
HTTP_USE_HTTP2 = os.getenv("HTTP_USE_HTTP2", "false").lower() in ("1", "true", "yes")

# ==================== 智能接续配置 ====================

CONTINUATION_CONFIG = {
    "enabled": os.getenv("CONTINUATION_ENABLED", "true").lower() in ("1", "true", "yes"),
    "max_continuations": int(os.getenv("MAX_CONTINUATIONS", "5")),
    "triggers": {
        "stream_interrupted": True,
        "max_tokens_reached": True,
        "incomplete_tool_json": True,
        "parse_error": True,
    },
    "continuation_prompt": """Your previous response was truncated. Please continue EXACTLY from where you stopped.

IMPORTANT:
- Do NOT repeat any content you already generated
- Do NOT add any preamble or explanation
- Continue the JSON/tool call from the exact character where it was cut off
- If you were in the middle of a tool call, complete it properly

Your truncated response ended with:
```
{truncated_ending}
```

Continue from here:""",
    "truncated_ending_chars": 500,
    "continuation_max_tokens": int(os.getenv("CONTINUATION_MAX_TOKENS", "8192")),
    "log_continuations": True,
}

# ==================== 上下文增强配置 ====================

CONTEXT_ENHANCEMENT_CONFIG = {
    "enabled": os.getenv("CONTEXT_ENHANCEMENT_ENABLED", "true").lower() in ("1", "true", "yes"),
    "model": os.getenv("CONTEXT_ENHANCEMENT_MODEL", "claude-sonnet-4-5-20250929"),
    "max_tokens": int(os.getenv("CONTEXT_ENHANCEMENT_MAX_TOKENS", "200")),
    "min_tokens": int(os.getenv("CONTEXT_ENHANCEMENT_MIN_TOKENS", "100")),
    "update_interval": int(os.getenv("CONTEXT_ENHANCEMENT_UPDATE_INTERVAL", "10")),
    "integrate_with_summary": os.getenv("CONTEXT_ENHANCEMENT_INTEGRATE_SUMMARY", "false").lower() in ("1", "true", "yes"),
    "extraction_prompt": """请分析以下对话历史，提取项目的核心上下文信息（100-200 tokens）：

**必须包含**：
1. 编程语言和主要框架
2. 核心功能和业务领域
3. 重要的技术约束或架构决策
4. 当前正在处理的主要任务

**格式要求**：
- 使用简洁的短语，不要完整句子
- 用 | 分隔不同信息点
- 总长度控制在 100-200 tokens

**示例输出**：
Python + FastAPI | AI API 代理服务 | Anthropic/OpenAI 格式转换 | 历史消息管理与智能摘要 | 模型路由(Opus/Sonnet) | 当前任务：添加上下文增强功能

对话历史：
{conversation_history}

请直接输出项目上下文，不要有任何前缀或解释：""",
    "enhancement_template": """<project_context>
{context}
</project_context>

<user_request>
{user_input}
</user_request>""",
}

# ==================== 历史消息管理配置 ====================

HISTORY_CONFIG = HistoryConfig(
    strategies=[
        TruncateStrategy.AUTO_TRUNCATE,
        TruncateStrategy.SMART_SUMMARY,
        TruncateStrategy.ERROR_RETRY,
    ],
    max_messages=30,
    max_chars=150000,
    summary_keep_recent=10,
    summary_threshold=100000,
    retry_max_messages=20,
    max_retries=2,
    estimate_threshold=150000,
    summary_cache_enabled=True,
    add_warning_header=True,
)

# ==================== 异步摘要优化配置 ====================

ASYNC_SUMMARY_CONFIG = {
    "enabled": os.getenv("ASYNC_SUMMARY_ENABLED", "true").lower() in ("1", "true", "yes"),
    "fast_first_request": os.getenv("ASYNC_SUMMARY_FAST_FIRST", "true").lower() in ("1", "true", "yes"),
    "max_pending_tasks": int(os.getenv("ASYNC_SUMMARY_MAX_TASKS", "100")),
    "update_interval_messages": int(os.getenv("ASYNC_SUMMARY_UPDATE_INTERVAL", "5")),
    "task_timeout": int(os.getenv("ASYNC_SUMMARY_TASK_TIMEOUT", "30")),
    "simulate_cache_billing": os.getenv("SIMULATE_CACHE_BILLING", "true").lower() in ("1", "true", "yes"),
    "cache_read_discount": float(os.getenv("CACHE_READ_DISCOUNT", "0.9")),
}

SUMMARY_MODEL = os.getenv("SUMMARY_MODEL", "claude-haiku-4-5-20251001")

# ==================== 流式输出分块 ====================

STREAM_TEXT_CHUNK_SIZE = int(os.getenv("STREAM_TEXT_CHUNK_SIZE", "2000"))
STREAM_TOOL_JSON_CHUNK_SIZE = int(os.getenv("STREAM_TOOL_JSON_CHUNK_SIZE", "2000"))
STREAM_THINKING_CHUNK_SIZE = int(os.getenv("STREAM_THINKING_CHUNK_SIZE", str(STREAM_TEXT_CHUNK_SIZE)))

# ==================== Anthropic -> OpenAI 转换保真度配置 ====================

ANTHROPIC_TRUNCATE_ENABLED = os.getenv("ANTHROPIC_TRUNCATE_ENABLED", "false").lower() in ("1", "true", "yes")
ANTHROPIC_MAX_MESSAGES = int(os.getenv("ANTHROPIC_MAX_MESSAGES", "200"))
ANTHROPIC_MAX_TOTAL_CHARS = int(os.getenv("ANTHROPIC_MAX_TOTAL_CHARS", "1000000"))
ANTHROPIC_MAX_SINGLE_CONTENT = int(os.getenv("ANTHROPIC_MAX_SINGLE_CONTENT", "300000"))
ANTHROPIC_TOOL_INPUT_MAX_CHARS = int(os.getenv("ANTHROPIC_TOOL_INPUT_MAX_CHARS", "200000"))
ANTHROPIC_TOOL_RESULT_MAX_CHARS = int(os.getenv("ANTHROPIC_TOOL_RESULT_MAX_CHARS", "300000"))
ANTHROPIC_CLEAN_SYSTEM_ENABLED = os.getenv("ANTHROPIC_CLEAN_SYSTEM_ENABLED", "false").lower() in ("1", "true", "yes")
ANTHROPIC_CLEAN_ASSISTANT_ENABLED = os.getenv("ANTHROPIC_CLEAN_ASSISTANT_ENABLED", "false").lower() in ("1", "true", "yes")
ANTHROPIC_MERGE_SAME_ROLE_ENABLED = os.getenv("ANTHROPIC_MERGE_SAME_ROLE_ENABLED", "false").lower() in ("1", "true", "yes")
ANTHROPIC_ENSURE_USER_ENDING = os.getenv("ANTHROPIC_ENSURE_USER_ENDING", "true").lower() in ("1", "true", "yes")
ANTHROPIC_EMPTY_ASSISTANT_PLACEHOLDER = os.getenv("ANTHROPIC_EMPTY_ASSISTANT_PLACEHOLDER", " ")
TOOL_DESC_MAX_CHARS = int(os.getenv("TOOL_DESC_MAX_CHARS", "8000"))
TOOL_PARAM_DESC_MAX_CHARS = int(os.getenv("TOOL_PARAM_DESC_MAX_CHARS", "4000"))

# ==================== 原生 Tools 支持配置 ====================

NATIVE_TOOLS_ENABLED = os.getenv("NATIVE_TOOLS_ENABLED", "true").lower() in ("1", "true", "yes")
NATIVE_TOOLS_FALLBACK_ENABLED = os.getenv("NATIVE_TOOLS_FALLBACK_ENABLED", "true").lower() in ("1", "true", "yes")

# ==================== 智能模型路由配置 ====================

MODEL_ROUTING_CONFIG = {
    "enabled": True,
    "opus_model": "claude-opus-4-5-20251101",
    "sonnet_model": "claude-sonnet-4-5-20250929",
    "haiku_model": "claude-haiku-4-5-20251001",
    "opus_max_concurrent": int(os.getenv("OPUS_MAX_CONCURRENT", "200")),

    # 概率配置 - 目标: Opus 20%, Sonnet 80%
    "base_opus_probability": int(os.getenv("BASE_OPUS_PROBABILITY", "20")),  # 默认 20% Opus
    "first_turn_opus_probability": int(os.getenv("FIRST_TURN_OPUS_PROBABILITY", "50")),  # 首轮 50% Opus
    "first_turn_max_messages": int(os.getenv("FIRST_TURN_MAX_MESSAGES", "2")),  # ≤2条消息算首轮

    # 确定性规则
    "force_opus_on_plan_mode": True,
    "force_opus_on_thinking": True,  # Extended Thinking 强制 Opus

    # 关键词触发
    "opus_keywords": [
        "设计方案", "架构设计", "系统设计", "技术方案", "整体规划",
        "design", "architecture", "plan",
        "根因分析", "深度分析", "全面分析", "分析一下",
        "root cause", "deep analysis", "analyze",
        "整体重构", "系统重构", "重构",
        "refactor",
        "创建项目", "新建项目", "从零开始",
        "create project", "new project", "from scratch",
        "实现", "implement", "开发", "develop",
    ],
    "sonnet_keywords": [
        "看看", "显示", "查看", "列出",
        "show", "view", "list", "display",
        "修复", "修改", "添加", "删除", "更新",
        "fix", "modify", "add", "delete", "update",
        "运行", "执行", "启动", "测试", "部署",
        "run", "execute", "start", "test", "deploy",
        "继续", "下一步", "好的", "是的",
        "continue", "next", "ok", "yes", "sure",
    ],

    # 执行阶段检测
    "execution_tool_threshold": 3,  # ≥3次工具调用进入执行阶段
    "execution_sonnet_probability": 90,  # 执行阶段 90% Sonnet

    "use_haiku_for_internal": True,
    "default_model": "sonnet",
    "log_routing_decision": True,
}

# ==================== 日志配置 ====================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("ai_history_manager_api")
