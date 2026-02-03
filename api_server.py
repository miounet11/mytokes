"""AI History Manager API 服务

提供 OpenAI 兼容的 API 接口，集成历史消息管理功能。
可接入 NewAPI 作为自定义渠道使用。

启动方式 (推荐多 worker):
    uvicorn api_server:app --host 0.0.0.0 --port 8100 --workers 4 --loop uvloop --http httptools

NewAPI 配置:
    - 类型: 自定义渠道
    - Base URL: http://your-server:8100
    - 模型: 按需配置
"""

import json
import time
import uuid
import asyncio
import logging
import os
import re
from typing import Optional, AsyncIterator, Union
from contextlib import asynccontextmanager
from functools import lru_cache

import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel

from ai_history_manager import HistoryManager, HistoryConfig, TruncateStrategy
from ai_history_manager.utils import is_content_length_error
from hallucination_detection import detect_hallucinated_tool_result

# ==================== 配置 ====================

# Kiro 代理地址 (tokens 网关, 使用内网地址)
KIRO_PROXY_BASE = "http://127.0.0.1:8000"
# OpenAI 兼容端点 (Kiro 渠道)
KIRO_PROXY_URL = f"{KIRO_PROXY_BASE}/kiro/v1/chat/completions"
KIRO_MODELS_URL = f"{KIRO_PROXY_BASE}/kiro/v1/models"
KIRO_API_KEY = "dba22273-65d3-4dc1-8ce9-182f680b2bf5"

# ==================== 智能接续配置 ====================

# 接续机制配置 - 处理上游截断响应
CONTINUATION_CONFIG = {
    # 启用接续机制
    "enabled": os.getenv("CONTINUATION_ENABLED", "true").lower() in ("1", "true", "yes"),

    # 最大续传次数（防止无限循环）
    # 优化：从 15 降低到 5，配合空响应验证可以更快失败
    # 如果需要处理超长输出，可以通过环境变量调整
    "max_continuations": int(os.getenv("MAX_CONTINUATIONS", "5")),

    # 触发续传的条件
    "triggers": {
        # 流中断（EOF/连接断开）
        "stream_interrupted": True,
        # max_tokens 达到上限
        "max_tokens_reached": True,
        # 工具调用 JSON 不完整
        "incomplete_tool_json": True,
        # 解析错误
        "parse_error": True,
    },

    # 续传提示词模板
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

    # 截断结尾保留字符数（用于续传提示）
    "truncated_ending_chars": 500,

    # 续传请求的 max_tokens（确保有足够空间完成）
    "continuation_max_tokens": int(os.getenv("CONTINUATION_MAX_TOKENS", "8192")),

    # 日志级别
    "log_continuations": True,
}

# ==================== 上下文增强配置 ====================

# 上下文增强机制 - 在用户新输入时注入项目背景信息
CONTEXT_ENHANCEMENT_CONFIG = {
    # 启用上下文增强
    "enabled": os.getenv("CONTEXT_ENHANCEMENT_ENABLED", "true").lower() in ("1", "true", "yes"),

    # 提取模型（使用 Sonnet 平衡速度和准确性）
    "model": os.getenv("CONTEXT_ENHANCEMENT_MODEL", "claude-sonnet-4-5-20250929"),

    # 上下文长度限制
    "max_tokens": int(os.getenv("CONTEXT_ENHANCEMENT_MAX_TOKENS", "200")),
    "min_tokens": int(os.getenv("CONTEXT_ENHANCEMENT_MIN_TOKENS", "100")),

    # 更新策略：每 N 条用户消息更新一次
    "update_interval": int(os.getenv("CONTEXT_ENHANCEMENT_UPDATE_INTERVAL", "10")),

    # 是否与智能摘要集成（推荐关闭以提升首字响应速度）
    # 开启时：每次摘要后会额外调用 AI 提取项目上下文，增加 3-5s 延迟
    "integrate_with_summary": os.getenv("CONTEXT_ENHANCEMENT_INTEGRATE_SUMMARY", "false").lower() in ("1", "true", "yes"),

    # 上下文提取提示词模板
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

    # 增强消息模板
    "enhancement_template": """<project_context>
{context}
</project_context>

<user_request>
{user_input}
</user_request>""",
}

# 历史消息管理配置
# 优化配置：平衡上下文保留和稳定性
HISTORY_CONFIG = HistoryConfig(
    strategies=[
        TruncateStrategy.AUTO_TRUNCATE,     # 自动截断 - 发送前优先保留最新上下文
        TruncateStrategy.SMART_SUMMARY,     # 智能摘要 - 用 AI 生成早期对话摘要
        TruncateStrategy.ERROR_RETRY,       # 错误重试 - 遇到长度错误时截断后重试（推荐）
    ],
    max_messages=30,           # 最大消息数
    max_chars=150000,          # 最大字符数
    summary_keep_recent=10,    # 保留最近 10 条消息完整
    summary_threshold=100000,  # 触发摘要阈值（字符）
    retry_max_messages=20,     # 重试时保留消息数
    max_retries=2,             # 最大重试次数
    estimate_threshold=150000, # 预估截断阈值
    summary_cache_enabled=True,
    add_warning_header=True,
)

# ==================== 异步摘要优化配置 ====================
# 核心思想：首次请求用简单截断快速响应，后台异步生成摘要供后续使用
ASYNC_SUMMARY_CONFIG = {
    # 启用异步摘要模式
    "enabled": os.getenv("ASYNC_SUMMARY_ENABLED", "true").lower() in ("1", "true", "yes"),

    # 首次请求策略：当没有缓存时，使用简单截断快速响应
    # 而非同步等待摘要生成
    "fast_first_request": os.getenv("ASYNC_SUMMARY_FAST_FIRST", "true").lower() in ("1", "true", "yes"),

    # 后台任务队列大小（防止内存泄漏）
    "max_pending_tasks": int(os.getenv("ASYNC_SUMMARY_MAX_TASKS", "100")),

    # 摘要更新间隔：每 N 条新用户消息后触发后台更新
    "update_interval_messages": int(os.getenv("ASYNC_SUMMARY_UPDATE_INTERVAL", "5")),

    # 后台任务超时（秒）
    "task_timeout": int(os.getenv("ASYNC_SUMMARY_TASK_TIMEOUT", "30")),

    # ==================== 缓存计费模拟 ====================
    # 当缓存命中时，模拟 Anthropic prompt caching 的计费折扣
    # 这样 NewAPI 会显示类似 "模型: 2.5 * 缓存: 0.1 * 专属倍率: 1"
    "simulate_cache_billing": os.getenv("SIMULATE_CACHE_BILLING", "true").lower() in ("1", "true", "yes"),

    # 缓存读取折扣比例（Anthropic 官方是 0.1，即 10% 价格）
    # 我们的摘要缓存虽然不是真正的 prompt caching，但确实节省了重复计算
    "cache_read_discount": float(os.getenv("CACHE_READ_DISCOUNT", "0.9")),
}

# 服务配置
SERVICE_PORT = int(os.getenv("SERVICE_PORT", "8100"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "300"))
HTTP_CONNECT_TIMEOUT = float(os.getenv("HTTP_CONNECT_TIMEOUT", "10"))
HTTP_READ_TIMEOUT = float(os.getenv("HTTP_READ_TIMEOUT", str(REQUEST_TIMEOUT)))
HTTP_WRITE_TIMEOUT = float(os.getenv("HTTP_WRITE_TIMEOUT", str(REQUEST_TIMEOUT)))
HTTP_POOL_TIMEOUT = float(os.getenv("HTTP_POOL_TIMEOUT", "5"))

# 流式输出分块（提升 CLI 兼容性与大文本稳定性）
STREAM_TEXT_CHUNK_SIZE = int(os.getenv("STREAM_TEXT_CHUNK_SIZE", "2000"))
STREAM_TOOL_JSON_CHUNK_SIZE = int(os.getenv("STREAM_TOOL_JSON_CHUNK_SIZE", "2000"))
STREAM_THINKING_CHUNK_SIZE = int(os.getenv("STREAM_THINKING_CHUNK_SIZE", str(STREAM_TEXT_CHUNK_SIZE)))

# Anthropic -> OpenAI 转换保真度配置（默认最大保真）
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
# Kiro 网关现支持原生 OpenAI tools 格式，启用后：
# 1. 减少 token 消耗（tools 不再注入 system prompt）
# 2. 提高解析准确性（原生 tool_calls 结构化返回）
# 3. 支持并行工具调用
NATIVE_TOOLS_ENABLED = os.getenv("NATIVE_TOOLS_ENABLED", "true").lower() in ("1", "true", "yes")
# 降级开关：当原生 tools 失败时，是否回退到文本注入方式
NATIVE_TOOLS_FALLBACK_ENABLED = os.getenv("NATIVE_TOOLS_FALLBACK_ENABLED", "true").lower() in ("1", "true", "yes")

# ==================== 智能模型路由配置 ====================

# 模型路由配置 - "Opus 大脑, Sonnet 双手" 策略
# 核心理念：
# 1. Opus 是大脑 - 负责规划、决策、深度分析、生成高质量上下文
# 2. Sonnet 是双手 - 负责执行、工具调用、代码编写
# 3. Sonnet 的弱点不是能力，而是理解深度 - 通过 Opus 生成的上下文来弥补
# 4. 目标 Opus 使用率：15-25%（关键时刻用 Opus，其余用 Sonnet）
MODEL_ROUTING_CONFIG = {
    # 启用智能路由
    "enabled": True,

    # 目标模型映射
    "opus_model": "claude-opus-4-5-20251101",
    "sonnet_model": "claude-sonnet-4-5-20250929",
    "haiku_model": "claude-haiku-4-5-20251001",

    # ============================================================
    # Opus 并发限制器 - 超出直接降级，无等待
    # ============================================================
    "opus_max_concurrent": int(os.getenv("OPUS_MAX_CONCURRENT", "200")),  # Opus 最大并发

    # ============================================================
    # 第零优先级：强制 Opus 的场景（不受其他条件影响）
    # ============================================================
    # Extended Thinking 请求 - 必须使用 Opus
    "force_opus_on_thinking": True,

    # Plan Mode 检测 - 规划模式必须用 Opus
    "force_opus_on_plan_mode": True,

    # 主 Agent 首轮请求 - 需要 Opus 进行初始分析和规划
    "main_agent_first_turn_opus_probability": 70,  # 主 Agent 首轮 70% 用 Opus

    # 主 Agent 后续请求 - 降低概率，让 Sonnet 执行
    "main_agent_opus_probability": 15,  # 主 Agent 后续 15% 用 Opus

    # ============================================================
    # 第一优先级：强制 Opus 的关键词（真正需要深度思考的任务）
    # 这些是 Opus 作为"大脑"必须处理的场景
    # ============================================================
    "force_opus_keywords": [
        # === 规划和设计 ===
        "创建项目", "新建项目", "初始化项目", "项目规划",
        "create project", "new project", "init project", "project plan",
        "系统设计", "架构设计", "整体架构", "技术方案",
        "system design", "architecture design", "technical design",
        "整体规划", "实施方案", "设计方案",
        "implementation plan", "design plan",

        # === Plan Mode 相关 ===
        "enterplanmode", "exitplanmode", "plan mode",
        "进入规划", "规划模式", "制定计划",

        # === 复杂分析和诊断 ===
        "全面分析", "根因分析", "深度分析", "问题诊断",
        "root cause", "deep analysis", "comprehensive analysis",
        "为什么会", "原因是什么", "分析一下原因",
        "why does", "what causes", "analyze why",

        # === 大规模重构 ===
        "整体重构", "大规模重构", "系统重构", "代码重构",
        "major refactor", "complete refactor", "system refactor",

        # === 决策和评估 ===
        "如何选择", "哪个更好", "对比分析", "优缺点",
        "which is better", "compare", "pros and cons", "trade-off",
        "最佳实践", "推荐方案",
        "best practice", "recommended approach",

        # === 复杂任务 ===
        "从零开始", "完整实现", "全新功能",
        "from scratch", "complete implementation", "new feature",
    ],

    # ============================================================
    # 第二优先级：强制 Sonnet 的关键词（执行性任务）
    # Sonnet 作为"双手"高效执行这些任务
    # ============================================================
    "force_sonnet_keywords": [
        # === 简单查看操作 ===
        "看看", "显示", "列出", "打开", "查看", "看一下",
        "show", "list", "display", "view", "open", "check", "look at",

        # === 小改动 ===
        "修复", "调整", "更新", "改一下", "改成", "修改", "添加", "删除",
        "fix", "adjust", "update", "modify", "add", "delete", "remove",
        "改个", "加个", "删掉", "去掉",

        # === 执行命令 ===
        "运行", "执行", "启动", "重启", "停止", "测试", "部署",
        "run", "execute", "start", "restart", "stop", "test", "deploy",

        # === 简单问答 ===
        "什么是", "哪里", "是不是", "有没有", "怎么用",
        "what is", "where is", "is it", "do you", "how to use",

        # === 读取/搜索类 ===
        "读取", "获取", "搜索", "查找", "定位", "找到",
        "read", "get", "search", "find", "locate",

        # === 安装/配置类 ===
        "安装", "下载", "配置", "设置", "环境",
        "install", "download", "config", "setup", "environment",

        # === 继续执行 ===
        "继续", "下一步", "接着", "然后", "好的", "可以",
        "continue", "next", "proceed", "then", "ok", "yes",

        # === 工具调用相关 ===
        "调用", "使用工具", "执行工具",
        "call", "use tool", "execute tool",
    ],

    # ============================================================
    # Haiku 关键词（极简任务，收紧范围）
    # 注意：Haiku 优先级已降低，只在 Sonnet 关键词之后检测
    # ============================================================
    "force_haiku_keywords": [
        # 仅保留最简单的任务
        "翻译成", "翻译为",  # 明确的翻译请求
        "translate to", "translate into",
    ],

    # ============================================================
    # 第三优先级：基于对话阶段的智能判断
    # 核心策略：首轮用 Opus 规划，后续用 Sonnet 执行
    # ============================================================

    # 首轮对话检测 - 首轮需要 Opus 进行任务理解和规划
    "first_turn_opus_probability": 60,    # 首轮 60% 概率用 Opus

    # 用户消息数阈值（不含 system）- 判断是否为首轮
    "first_turn_max_user_messages": 2,    # ≤2 条消息视为首轮

    # 工具执行阶段检测 - 有工具调用说明在执行阶段，用 Sonnet
    "execution_phase_tool_calls": 3,      # 工具调用 >= 3 次视为执行阶段
    "execution_phase_sonnet_probability": 90,  # 执行阶段 90% 用 Sonnet

    # ============================================================
    # 第四优先级：保底概率
    # ============================================================
    "base_opus_probability": 15,          # 基础 15% 概率使用 Opus

    # ============================================================
    # Sonnet 上下文增强配置
    # 核心思想：Sonnet 的弱点是理解深度，通过注入高质量上下文来弥补
    # ============================================================
    "sonnet_context_enhancement": {
        "enabled": True,
        # 当路由到 Sonnet 时，注入 Opus 生成的摘要/上下文
        "inject_opus_summary": True,
        # 上下文注入的最大 token 数
        "max_context_tokens": 2000,
        # 上下文注入模板
        "context_template": """<opus_analysis>
以下是对当前任务的深度分析（由 Opus 生成）：
{opus_context}
</opus_analysis>

请基于以上分析，高效执行用户的请求。""",
    },

    # ============================================================
    # 调试和监控
    # ============================================================
    "log_routing_decision": True,         # 记录路由决策原因
}

# ==================== 预编译正则表达式 ====================
# 性能优化：避免在热路径中重复编译正则表达式

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
# 匹配 <ToolName>...</ToolName> 格式，工具名以大写字母开头
_RE_XML_TOOL_CALL = re.compile(r'<([A-Z][a-zA-Z0-9_]*)>([\s\S]*?)</\1>')
# 匹配 XML 参数 <param_name>value</param_name>
_RE_XML_PARAM = re.compile(r'<([a-z_][a-z0-9_]*)>([\s\S]*?)</\1>', re.IGNORECASE)

# 用于文件路径匹配
_RE_FILE_PATH = re.compile(r'[/\\][\w\-\.]+\.(py|js|ts|jsx|tsx|go|rs|java|cpp|c|h|md|yaml|yml|json|toml)')


class ModelRouter:
    """智能模型路由器 - "Opus 大脑, Sonnet 双手" 策略

    核心理念：
    1. Opus 是大脑 - 负责规划、决策、深度分析 (15-25%)
       - Plan Mode（规划模式）
       - 首轮任务理解和策略制定
       - 架构设计和复杂决策
       - 错误诊断和根因分析

    2. Sonnet 是双手 - 负责执行、工具调用、代码编写 (70-80%)
       - 工具调用和代码执行
       - 迭代修改和调试
       - 简单问答和查询

    3. Haiku 是快手 - 简单快速任务 (5-10%)
       - 格式转换、翻译、简单总结

    Sonnet 增强策略：
    - Sonnet 的弱点不是能力，而是理解深度
    - 通过注入 Opus 生成的高质量上下文来弥补
    - 确保 Sonnet 有足够的背景信息来高效执行

    并发控制：
    - Opus 有最大并发限制，超出时自动降级到 Sonnet
    - 确保 Opus 资源不被耗尽
    """

    def __init__(self, config: dict = None):
        self.config = config or MODEL_ROUTING_CONFIG
        self.stats = {
            "opus": 0,
            "sonnet": 0,
            "haiku": 0,
            "opus_degraded": 0,
            "opus_plan_mode": 0,      # Plan Mode 使用 Opus 的次数
            "opus_first_turn": 0,     # 首轮使用 Opus 的次数
            "opus_keywords": 0,       # 关键词触发 Opus 的次数
            "sonnet_enhanced": 0,     # Sonnet 上下文增强的次数
        }
        self._lock = asyncio.Lock()
        # 预处理关键词为小写，避免每次匹配时重复转换
        self._opus_keywords_lower = [kw.lower() for kw in self.config.get("force_opus_keywords", [])]
        self._sonnet_keywords_lower = [kw.lower() for kw in self.config.get("force_sonnet_keywords", [])]
        self._haiku_keywords_lower = [kw.lower() for kw in self.config.get("force_haiku_keywords", [])]

        # Opus 并发控制
        self._opus_semaphore = asyncio.Semaphore(self.config.get("opus_max_concurrent", 15))
        self._opus_current = 0

        # Plan Mode 检测标记
        self._plan_mode_markers = [
            "enterplanmode", "exitplanmode", "plan mode",
            "进入规划", "规划模式", "制定计划",
            "in plan mode", "planning mode",
        ]

    def _count_chars(self, messages: list, system: str = "") -> int:
        """统计总字符数"""
        total = len(str(system)) if system else 0
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                total += len(content)
            elif isinstance(content, list):
                for item in content:
                    if isinstance(item, dict):
                        total += len(str(item.get("text", "")))
                        total += len(str(item.get("content", "")))
                    elif isinstance(item, str):
                        total += len(item)
        return total

    def _count_tool_calls(self, messages: list) -> int:
        """统计历史中的工具调用次数"""
        count = 0
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict):
                        if item.get("type") in ("tool_use", "tool_result"):
                            count += 1
        return count

    def _count_files_mentioned(self, messages: list) -> int:
        """统计提及的文件数量（简单估算）"""
        files = set()

        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                matches = _RE_FILE_PATH.findall(content)
                files.update(matches)
            elif isinstance(content, list):
                for item in content:
                    if isinstance(item, dict):
                        text = item.get("text", "") or item.get("content", "")
                        if isinstance(text, str):
                            matches = _RE_FILE_PATH.findall(text)
                            files.update(matches)
        return len(files)

    def _get_last_user_message(self, messages: list) -> str:
        """获取最后一条用户消息"""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, str):
                    return content
                elif isinstance(content, list):
                    texts = []
                    for item in content:
                        if isinstance(item, dict) and item.get("type") == "text":
                            texts.append(item.get("text", ""))
                    return " ".join(texts)
        return ""

    def _contains_keywords(self, text: str, keywords: list) -> bool:
        """检查文本是否包含关键词（兼容旧接口）"""
        text_lower = text.lower()
        for kw in keywords:
            if kw.lower() in text_lower:
                return True
        return False

    def _contains_keywords_optimized(self, text: str, keywords_lower: list) -> tuple[bool, str]:
        """优化版关键词检查，使用预处理的小写关键词列表

        Returns:
            (found, matched_keyword)
        """
        text_lower = text.lower()
        for kw in keywords_lower:
            if kw in text_lower:
                return True, kw
        return False, ""

    def _count_user_messages(self, messages: list) -> int:
        """统计用户消息数量"""
        return sum(1 for msg in messages if msg.get("role") == "user")

    def _get_hash_probability(self, seed: str, threshold: int) -> bool:
        """基于哈希的概率判断，确保相同输入得到相同结果"""
        import hashlib
        hash_val = int(hashlib.md5(seed.encode()).hexdigest()[:8], 16)
        return (hash_val % 100) < threshold

    def _is_sub_agent_request(self, messages: list) -> bool:
        """检测是否为子 Agent 请求"""
        # 子 Agent 的 system prompt 通常包含这些特征
        if not messages:
            return False
        first_msg = messages[0]
        if first_msg.get("role") != "system":
            return False
        content = first_msg.get("content", "")
        # 子 Agent 特征
        sub_agent_markers = [
            "command execution specialist",
            "exploring codebase",
            "specialized agent",
            "bash commands efficiently",
            "research task",
        ]
        content_lower = content.lower()
        return any(marker in content_lower for marker in sub_agent_markers)

    def _has_thinking_request(self, request_body: dict) -> bool:
        """检测是否为 Extended Thinking 请求"""
        # 检查是否有 thinking 相关参数
        if "thinking" in request_body:
            return True
        if "budget_tokens" in request_body:
            return True
        # 检查消息中是否有 thinking content
        for msg in request_body.get("messages", []):
            content = msg.get("content", [])
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "thinking":
                        return True
        return False

    def _is_plan_mode(self, messages: list) -> bool:
        """检测是否处于 Plan Mode（规划模式）

        Plan Mode 是 Claude Code 的规划模式，需要深度思考和分析
        """
        # 检查 system prompt 中是否有 plan mode 标记
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                content_lower = content.lower()
                for marker in self._plan_mode_markers:
                    if marker in content_lower:
                        return True
            elif isinstance(content, list):
                for item in content:
                    if isinstance(item, dict):
                        text = item.get("text", "") or item.get("content", "")
                        if isinstance(text, str):
                            text_lower = text.lower()
                            for marker in self._plan_mode_markers:
                                if marker in text_lower:
                                    return True
        return False

    def _is_debugging_task(self, text: str) -> tuple[bool, str]:
        """检测是否为调试/排查任务 - 这类任务需要 Opus 的深度分析能力

        调试任务特征：
        - 排查问题、分析错误
        - 理解逻辑、追踪流程
        - 诊断原因、定位 bug
        """
        debugging_keywords = [
            # 排查和诊断
            "排查", "排错", "诊断", "定位问题", "找问题",
            "troubleshoot", "diagnose", "debug",
            # 错误相关
            "报错", "出错", "错误", "异常", "失败",
            "error", "exception", "failed", "failure",
            # 问题分析
            "问题", "bug", "issue", "问题是",
            # 逻辑分析
            "逻辑", "流程", "原理", "机制",
            "logic", "flow", "mechanism",
            # 持续性问题
            "还是", "依然", "仍然", "一直", "总是",
            "still", "always", "keeps",
            # 原因分析
            "为什么", "原因", "怎么回事", "怎么了",
            "why", "reason", "what happened",
        ]
        text_lower = text.lower()
        for kw in debugging_keywords:
            if kw in text_lower:
                return True, kw
        return False, ""

    def should_use_opus(self, request_body: dict) -> tuple[bool, str]:
        """
        智能判断是否应该使用 Opus - "Opus 大脑" 策略

        决策优先级：
        0a. Extended Thinking 请求 → 强制 Opus
        0b. Plan Mode（规划模式）→ 强制 Opus
        0c. 调试/排查任务 → 强制 Opus（需要深度分析）
        1. 强制 Opus 关键词 → Opus
        2. 主 Agent 首轮 → 高概率 Opus（任务理解和规划）
        3. 强制 Sonnet 关键词 → Sonnet
        4. 执行阶段（大量工具调用）→ 高概率 Sonnet
        5. 首轮对话 → 概率 Opus
        6. 保底概率 → 确保 ~15% Opus

        Returns:
            (should_use_opus, reason)
        """
        if not self.config.get("enabled", True):
            return True, "路由已禁用"

        messages = request_body.get("messages", [])
        last_user_msg = self._get_last_user_message(messages)
        user_msg_count = self._count_user_messages(messages)

        # 生成稳定的哈希种子（相同请求得到相同结果）
        hash_seed = f"{len(messages)}:{last_user_msg[:200]}"

        # ============================================================
        # 第零优先级：特殊场景强制 Opus（大脑必须介入）
        # ============================================================

        # 0a. Extended Thinking 请求 - 必须使用 Opus
        if self.config.get("force_opus_on_thinking", True) and self._has_thinking_request(request_body):
            return True, "ExtendedThinking"

        # 0b. Plan Mode 检测 - 规划模式必须用 Opus
        if self.config.get("force_opus_on_plan_mode", True) and self._is_plan_mode(messages):
            return True, "PlanMode规划"

        # 0c. 调试/排查任务 - 需要 Opus 的深度分析能力
        is_debug, debug_kw = self._is_debugging_task(last_user_msg)
        if is_debug:
            return True, f"调试任务[{debug_kw}]"

        # ============================================================
        # 第一优先级：强制 Opus 关键词（使用预处理的小写关键词）
        # ============================================================
        found, matched_kw = self._contains_keywords_optimized(last_user_msg, self._opus_keywords_lower)
        if found:
            return True, f"关键词[{matched_kw}]"

        # ============================================================
        # 第二优先级：主 Agent 首轮请求 - 需要 Opus 进行任务理解
        # ============================================================
        is_sub_agent = self._is_sub_agent_request(messages)
        if not is_sub_agent and user_msg_count <= 2:
            # 主 Agent 首轮使用更高的 Opus 概率
            first_turn_prob = self.config.get("main_agent_first_turn_opus_probability", 70)
            if self._get_hash_probability(hash_seed + ":main_first", first_turn_prob):
                return True, f"主Agent首轮({first_turn_prob}%)"

        # ============================================================
        # 第三优先级：强制 Sonnet 关键词（执行性任务）
        # ============================================================
        found, matched_kw = self._contains_keywords_optimized(last_user_msg, self._sonnet_keywords_lower)
        if found:
            return False, f"简单任务[{matched_kw}]"

        # ============================================================
        # 第四优先级：执行阶段检测 - 有工具调用说明在执行，用 Sonnet
        # ============================================================
        tool_calls = self._count_tool_calls(messages)
        execution_threshold = self.config.get("execution_phase_tool_calls", 3)
        if tool_calls >= execution_threshold:
            sonnet_prob = self.config.get("execution_phase_sonnet_probability", 90)
            if self._get_hash_probability(hash_seed + ":exec", sonnet_prob):
                return False, f"执行阶段({tool_calls}次工具,{sonnet_prob}%Sonnet)"
            else:
                return True, f"执行阶段随机Opus({tool_calls}次工具)"

        # ============================================================
        # 第五优先级：首轮对话检测
        # ============================================================
        first_turn_max = self.config.get("first_turn_max_user_messages", 2)
        if user_msg_count <= first_turn_max:
            first_turn_prob = self.config.get("first_turn_opus_probability", 60)
            if self._get_hash_probability(hash_seed + ":first", first_turn_prob):
                return True, f"首轮对话({user_msg_count}条,{first_turn_prob}%)"

        # ============================================================
        # 第六优先级：保底概率
        # ============================================================
        base_opus_prob = self.config.get("base_opus_probability", 15)
        if self._get_hash_probability(hash_seed + ":base", base_opus_prob):
            return True, f"保底概率({base_opus_prob}%)"
        else:
            return False, f"默认Sonnet(msg={user_msg_count},tools={tool_calls})"

    def should_use_haiku(self, request_body: dict) -> tuple[bool, str]:
        """
        判断是否应该使用 Haiku（最快速的模型）

        适用场景：
        - 简单查询、格式转换、快速总结
        - 不涉及代码修改或复杂推理

        Returns:
            (should_use_haiku, reason)
        """
        messages = request_body.get("messages", [])
        last_user_msg = self._get_last_user_message(messages)

        # 检查 Haiku 关键词
        found, matched_kw = self._contains_keywords_optimized(last_user_msg, self._haiku_keywords_lower)
        if found:
            return True, f"Haiku任务[{matched_kw}]"

        return False, ""

    async def acquire_opus_slot(self) -> bool:
        """
        尝试获取 Opus 槽位

        Returns:
            True 如果成功获取，False 如果 Opus 已满
        """
        try:
            # 非阻塞尝试获取
            acquired = self._opus_semaphore.locked() is False
            if acquired:
                await self._opus_semaphore.acquire()
                async with self._lock:
                    self._opus_current += 1
                return True
            return False
        except Exception:
            return False

    def release_opus_slot(self):
        """释放 Opus 槽位"""
        try:
            self._opus_semaphore.release()
            # 注意：这里不用 async with，因为可能在非异步上下文调用
        except ValueError:
            pass  # 已经释放过了

    async def route(self, request_body: dict) -> tuple[str, str]:
        """
        路由到合适的模型 - "Opus 大脑, Sonnet 双手" 策略

        策略优先级：
        1. 非 Opus 请求直接放行
        2. 检查是否应该用 Opus（复杂任务、规划、调试）
        3. 检查 Opus 并发限制，超出时降级到 Sonnet
        4. 检查是否应该用 Haiku（极简任务，优先级最低）
        5. 默认使用 Sonnet

        Returns:
            (routed_model, reason)
        """
        original_model = request_body.get("model", "")

        # 只处理 Opus 请求（Sonnet/Haiku 请求直接放行）
        if "opus" not in original_model.lower():
            async with self._lock:
                if "haiku" in original_model.lower():
                    self.stats["haiku"] += 1
                else:
                    self.stats["sonnet"] += 1
            return original_model, "非Opus请求"

        # 检查是否应该用 Opus（优先级最高）
        should_opus, reason = self.should_use_opus(request_body)

        if should_opus:
            # 检查 Opus 并发限制
            max_concurrent = self.config.get("opus_max_concurrent", 15)
            async with self._lock:
                current_opus = self.stats["opus"] - self.stats.get("opus_completed", 0)

            if current_opus >= max_concurrent:
                # Opus 已满，降级到 Sonnet
                async with self._lock:
                    self.stats["sonnet"] += 1
                    self.stats["opus_degraded"] += 1
                return self.config.get("sonnet_model"), f"Opus已满({current_opus}/{max_concurrent}),降级Sonnet"

            async with self._lock:
                self.stats["opus"] += 1
            return self.config.get("opus_model"), reason

        # Haiku 检测（优先级最低，只处理极简任务）
        should_haiku, haiku_reason = self.should_use_haiku(request_body)
        if should_haiku:
            async with self._lock:
                self.stats["haiku"] += 1
            return self.config.get("haiku_model"), haiku_reason

        # 默认使用 Sonnet
        async with self._lock:
            self.stats["sonnet"] += 1
        return self.config.get("sonnet_model"), reason

    def route_sync(self, request_body: dict) -> tuple[str, str]:
        """
        路由到合适的模型（同步版本，用于非异步上下文）
        注意：统计数据在高并发下可能不精确

        Returns:
            (routed_model, reason)
        """
        original_model = request_body.get("model", "")

        # 只处理 Opus 请求
        if "opus" not in original_model.lower():
            self.stats["other"] += 1
            return original_model, "非Opus请求"

        should_opus, reason = self.should_use_opus(request_body)

        if should_opus:
            self.stats["opus"] += 1
            return self.config.get("opus_model", "claude-opus-4-5-20251101"), reason
        else:
            self.stats["sonnet"] += 1
            return self.config.get("sonnet_model", "claude-sonnet-4-5-20250929"), reason

    def get_stats(self) -> dict:
        """获取路由统计"""
        total = self.stats["opus"] + self.stats["sonnet"] + self.stats["haiku"]
        if total == 0:
            opus_pct = sonnet_pct = haiku_pct = 0
        else:
            opus_pct = round(self.stats["opus"] / total * 100, 1)
            sonnet_pct = round(self.stats["sonnet"] / total * 100, 1)
            haiku_pct = round(self.stats["haiku"] / total * 100, 1)

        return {
            "opus_requests": self.stats["opus"],
            "sonnet_requests": self.stats["sonnet"],
            "haiku_requests": self.stats["haiku"],
            "opus_degraded": self.stats.get("opus_degraded", 0),
            "total_requests": total,
            "opus_percent": f"{opus_pct}%",
            "sonnet_percent": f"{sonnet_pct}%",
            "haiku_percent": f"{haiku_pct}%",
            "opus_max_concurrent": self.config.get("opus_max_concurrent", 10),
        }


# 全局模型路由器实例
model_router = ModelRouter(MODEL_ROUTING_CONFIG)

# ==================== 高并发配置 ====================

# HTTP 连接池配置 - 针对高并发优化
# 关键：禁用 HTTP/2，使用 HTTP/1.1 多连接模式
# 原因：HTTP/2 多路复用会让所有请求走同一连接，tokens 可能误认为是同一终端
HTTP_POOL_MAX_CONNECTIONS = int(os.getenv("HTTP_POOL_MAX_CONNECTIONS", "2000"))
HTTP_POOL_MAX_KEEPALIVE = int(os.getenv("HTTP_POOL_MAX_KEEPALIVE", "500"))
HTTP_POOL_KEEPALIVE_EXPIRY = int(os.getenv("HTTP_POOL_KEEPALIVE_EXPIRY", "30"))
HTTP_USE_HTTP2 = os.getenv("HTTP_USE_HTTP2", "false").lower() in ("1", "true", "yes")

# ==================== 日志 ====================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("ai_history_manager_api")

# ==================== 全局 HTTP 客户端 ====================

# 全局 HTTP 客户端 (连接池复用，极致高并发)
http_client: Optional[httpx.AsyncClient] = None


def get_http_client() -> httpx.AsyncClient:
    """获取全局 HTTP 客户端"""
    global http_client
    if http_client is None:
        raise RuntimeError("HTTP client not initialized. Server not started properly.")
    return http_client


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理 - 初始化和清理全局资源"""
    global http_client

    # 启动时初始化
    logger.info("初始化全局 HTTP 客户端 (高并发模式)...")

    # 创建连接池限制 - 大容量
    limits = httpx.Limits(
        max_connections=HTTP_POOL_MAX_CONNECTIONS,
        max_keepalive_connections=HTTP_POOL_MAX_KEEPALIVE,
        keepalive_expiry=HTTP_POOL_KEEPALIVE_EXPIRY,
    )

    # 创建全局 HTTP 客户端 - 优化配置
    # 关键修改：禁用 HTTP/2，使用 HTTP/1.1
    # 原因：HTTP/2 多路复用让所有请求走同一连接，tokens 可能误认为是同一终端
    # HTTP/1.1 允许多个独立的 TCP 连接，每个请求可以并行处理
    timeout = httpx.Timeout(
        timeout=REQUEST_TIMEOUT,
        connect=HTTP_CONNECT_TIMEOUT,
        read=HTTP_READ_TIMEOUT,
        write=HTTP_WRITE_TIMEOUT,
        pool=HTTP_POOL_TIMEOUT,
    )
    http_client = httpx.AsyncClient(
        timeout=timeout,
        limits=limits,
        http2=HTTP_USE_HTTP2,  # 使用配置的 HTTP 版本
    )

    logger.info(f"HTTP 客户端已初始化: max_connections={HTTP_POOL_MAX_CONNECTIONS}, "
                f"keepalive={HTTP_POOL_MAX_KEEPALIVE}")

    yield  # 应用运行中

    # 关闭时清理
    logger.info("关闭全局 HTTP 客户端...")
    if http_client:
        await http_client.aclose()
        http_client = None
    logger.info("资源清理完成")


# ==================== FastAPI App ====================

app = FastAPI(
    title="AI History Manager API",
    description="OpenAI 兼容 API，集成智能历史消息管理",
    version="1.0.0",
    lifespan=lifespan,  # 使用生命周期管理
)


# ==================== 数据模型 ====================

class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[dict]
    stream: Optional[bool] = False
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    top_p: Optional[float] = None
    frequency_penalty: Optional[float] = None
    presence_penalty: Optional[float] = None
    stop: Optional[list[str]] = None


# ==================== 辅助函数 ====================

def generate_session_id(messages: list[dict]) -> str:
    """基于消息内容生成会话 ID"""
    if not messages:
        return "default"

    content_parts = []
    for msg in messages[:3]:
        content = msg.get("content", "")
        if isinstance(content, str):
            content_parts.append(content[:100])

    if content_parts:
        import hashlib
        return hashlib.md5("".join(content_parts).encode()).hexdigest()[:16]

    return "default"


def extract_user_content(messages: list[dict]) -> str:
    """提取最后一条用户消息"""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                return content
    return ""


# ==================== 上下文增强机制 ====================

# Session 上下文存储（内存）
_session_contexts = {}


def get_session_context(session_id: str) -> dict:
    """获取 session 的项目上下文"""
    return _session_contexts.get(session_id, {
        "content": "",
        "last_updated_at": 0,
        "message_count_at_update": 0,
        "version": 0,
    })


def update_session_context(session_id: str, context: str, message_count: int):
    """更新 session 的项目上下文"""
    _session_contexts[session_id] = {
        "content": context,
        "last_updated_at": time.time(),
        "message_count_at_update": message_count,
        "version": _session_contexts.get(session_id, {}).get("version", 0) + 1,
    }


def count_user_messages(messages: list[dict]) -> int:
    """统计用户消息数量"""
    return sum(1 for msg in messages if msg.get("role") == "user")


async def extract_project_context(messages: list[dict], session_id: str) -> str:
    """从对话历史中提取项目上下文

    Args:
        messages: 对话历史（建议传入最近 20 条）
        session_id: 会话 ID

    Returns:
        项目上下文字符串（100-200 tokens）
    """
    if not CONTEXT_ENHANCEMENT_CONFIG["enabled"]:
        return ""

    if not messages:
        return ""

    # 格式化对话历史
    conversation_history = []
    for msg in messages[-20:]:  # 只看最近 20 条
        role = msg.get("role", "")
        content = msg.get("content", "")

        # 处理复杂 content 结构
        if isinstance(content, list):
            content_str = ""
            for item in content:
                if isinstance(item, dict):
                    if item.get("type") == "text":
                        content_str += item.get("text", "")
                    elif item.get("type") == "tool_use":
                        content_str += f"[Tool: {item.get('name', 'unknown')}]"
                    elif item.get("type") == "tool_result":
                        content_str += "[Tool Result]"
            content = content_str

        if isinstance(content, str) and content.strip():
            # 截断过长的消息
            if len(content) > 500:
                content = content[:500] + "..."
            conversation_history.append(f"{role}: {content}")

    if not conversation_history:
        return ""

    # 构建提取提示词
    prompt = CONTEXT_ENHANCEMENT_CONFIG["extraction_prompt"].format(
        conversation_history="\n".join(conversation_history)
    )

    # 调用 LLM 提取上下文
    context_id = uuid.uuid4().hex[:8]
    request_body = {
        "model": CONTEXT_ENHANCEMENT_CONFIG["model"],
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "max_tokens": CONTEXT_ENHANCEMENT_CONFIG["max_tokens"] + 50,  # 留一些余量
    }

    headers = {
        "Authorization": f"Bearer {KIRO_API_KEY}",
        "Content-Type": "application/json",
        "X-Request-ID": f"context_{context_id}",
        "X-Trace-ID": f"trace_{uuid.uuid4().hex}",
    }

    try:
        client = get_http_client()
        response = await client.post(
            KIRO_PROXY_URL,
            json=request_body,
            headers=headers,
            timeout=30.0,
        )

        if response.status_code == 200:
            result = response.json()
            context = result.get("choices", [{}])[0].get("message", {}).get("content", "").strip()

            # 验证长度
            if len(context) < CONTEXT_ENHANCEMENT_CONFIG["min_tokens"] * 4:  # 粗略估算
                logger.warning(f"[{context_id}] 提取的上下文过短: {len(context)} chars")
            elif len(context) > CONTEXT_ENHANCEMENT_CONFIG["max_tokens"] * 4:
                logger.warning(f"[{context_id}] 提取的上下文过长，截断: {len(context)} chars")
                context = context[:CONTEXT_ENHANCEMENT_CONFIG["max_tokens"] * 4]

            logger.info(f"[{context_id}] ✅ 上下文提取成功: {len(context)} chars")
            return context
        else:
            logger.error(f"[{context_id}] 上下文提取失败: {response.status_code}")
            return ""

    except Exception as e:
        logger.error(f"[{context_id}] 上下文提取异常: {e}")
        return ""


async def enhance_user_message(messages: list[dict], session_id: str) -> list[dict]:
    """增强用户消息（在最后一条用户消息中注入项目上下文）

    Args:
        messages: 原始消息列表
        session_id: 会话 ID

    Returns:
        增强后的消息列表
    """
    if not CONTEXT_ENHANCEMENT_CONFIG["enabled"]:
        return messages

    if not messages:
        return messages

    # 检查最后一条是否是用户消息
    if messages[-1].get("role") != "user":
        return messages

    # 获取当前上下文
    session_context = get_session_context(session_id)
    user_message_count = count_user_messages(messages)

    # 判断是否需要更新上下文
    should_update = (
        not session_context["content"] or  # 首次
        user_message_count - session_context["message_count_at_update"] >= CONTEXT_ENHANCEMENT_CONFIG["update_interval"]  # 超过间隔
    )

    if should_update:
        logger.info(f"[{session_id}] 🔄 触发上下文提取（用户消息数: {user_message_count}）")
        context = await extract_project_context(messages, session_id)
        if context:
            update_session_context(session_id, context, user_message_count)
            session_context = get_session_context(session_id)
    else:
        context = session_context["content"]

    # 如果没有上下文，直接返回
    if not context:
        return messages

    # 增强最后一条用户消息
    enhanced_messages = messages.copy()
    last_message = enhanced_messages[-1].copy()
    original_content = last_message.get("content", "")

    # 处理复杂 content 结构
    if isinstance(original_content, list):
        # 如果是列表，找到第一个 text 类型并增强
        enhanced_content = []
        text_enhanced = False
        for item in original_content:
            if isinstance(item, dict) and item.get("type") == "text" and not text_enhanced:
                enhanced_text = CONTEXT_ENHANCEMENT_CONFIG["enhancement_template"].format(
                    context=context,
                    user_input=item.get("text", "")
                )
                enhanced_content.append({"type": "text", "text": enhanced_text})
                text_enhanced = True
            else:
                enhanced_content.append(item)
        last_message["content"] = enhanced_content
    elif isinstance(original_content, str):
        # 如果是字符串，直接增强
        enhanced_text = CONTEXT_ENHANCEMENT_CONFIG["enhancement_template"].format(
            context=context,
            user_input=original_content
        )
        last_message["content"] = enhanced_text

    enhanced_messages[-1] = last_message

    logger.info(f"[{session_id}] 🎯 上下文增强完成: {len(original_content) if isinstance(original_content, str) else 'complex'} -> {len(str(last_message['content']))} chars")

    return enhanced_messages


# 摘要生成模型配置
SUMMARY_MODEL = os.getenv("SUMMARY_MODEL", "claude-haiku-4-5-20251001")


async def call_kiro_for_summary(prompt: str) -> str:
    """调用 Kiro API 生成摘要 - 使用全局 HTTP 客户端"""
    summary_id = uuid.uuid4().hex[:8]
    request_body = {
        "model": SUMMARY_MODEL,  # 使用 Haiku 4.5 快速模型
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "max_tokens": 2000,
    }

    # 添加唯一请求标识
    headers = {
        "Authorization": f"Bearer {KIRO_API_KEY}",
        "Content-Type": "application/json",
        "X-Request-ID": f"summary_{summary_id}",
        "X-Trace-ID": f"trace_{uuid.uuid4().hex}",
    }

    try:
        client = get_http_client()
        response = await client.post(
            KIRO_PROXY_URL,
            json=request_body,
            headers=headers,
            timeout=60,  # 摘要请求使用较短超时
        )

        if response.status_code == 200:
            data = response.json()
            return data.get("choices", [{}])[0].get("message", {}).get("content", "")
    except Exception as e:
        logger.warning(f"摘要生成失败: {e}")

    return ""


# ==================== 异步摘要管理器 ====================

class AsyncSummaryManager:
    """异步摘要管理器 - 后台生成摘要，不阻塞主请求

    核心思想：
    1. 首次请求：使用简单截断快速响应，后台启动摘要任务
    2. 后续请求：使用缓存的摘要，0 延迟
    3. 增量更新：每 N 条新消息后，后台更新摘要
    """

    def __init__(self):
        # 摘要缓存：session_id -> {"summary": str, "message_count": int, "timestamp": float, "original_tokens": int}
        self._summary_cache: dict[str, dict] = {}
        # 正在进行的任务：session_id -> asyncio.Task
        self._pending_tasks: dict[str, asyncio.Task] = {}
        # 锁
        self._lock = asyncio.Lock()
        # 统计
        self._stats = {
            "cache_hits": 0,
            "cache_misses": 0,
            "async_tasks": 0,
            "tokens_saved": 0,  # 通过缓存节省的 tokens
        }

    def get_cached_summary(self, session_id: str) -> tuple[str, bool, int]:
        """获取缓存的摘要

        Returns:
            (summary, is_valid, original_tokens) - 摘要内容、是否有效、原始 token 数
        """
        cache_entry = self._summary_cache.get(session_id)
        if cache_entry and cache_entry.get("summary"):
            self._stats["cache_hits"] += 1
            original_tokens = cache_entry.get("original_tokens", 0)
            return cache_entry["summary"], True, original_tokens
        self._stats["cache_misses"] += 1
        return "", False, 0

    def get_cache_info(self, session_id: str) -> dict:
        """获取缓存信息，用于计费模拟

        Returns:
            {
                "hit": bool,
                "original_tokens": int,  # 原始消息的 token 数
                "cached_tokens": int,    # 缓存的摘要 token 数
                "saved_tokens": int,     # 节省的 tokens
            }
        """
        cache_entry = self._summary_cache.get(session_id)
        if cache_entry and cache_entry.get("summary"):
            original_tokens = cache_entry.get("original_tokens", 0)
            cached_tokens = cache_entry.get("cached_tokens", 0)
            return {
                "hit": True,
                "original_tokens": original_tokens,
                "cached_tokens": cached_tokens,
                "saved_tokens": max(0, original_tokens - cached_tokens),
            }
        return {"hit": False, "original_tokens": 0, "cached_tokens": 0, "saved_tokens": 0}

    def should_update_summary(self, session_id: str, current_message_count: int) -> bool:
        """判断是否需要更新摘要

        条件：
        1. 没有缓存
        2. 消息数增加超过阈值
        """
        cache_entry = self._summary_cache.get(session_id)
        if not cache_entry:
            return True

        cached_count = cache_entry.get("message_count", 0)
        update_interval = ASYNC_SUMMARY_CONFIG.get("update_interval_messages", 5)

        return (current_message_count - cached_count) >= update_interval

    def is_task_pending(self, session_id: str) -> bool:
        """检查是否有正在进行的摘要任务"""
        task = self._pending_tasks.get(session_id)
        return task is not None and not task.done()

    async def schedule_summary_task(
        self,
        session_id: str,
        messages: list,
        manager: "HistoryManager",
        user_content: str
    ):
        """调度后台摘要任务

        不阻塞主请求，后台生成摘要并更新缓存
        """
        if not ASYNC_SUMMARY_CONFIG.get("enabled", True):
            return

        # 检查是否已有任务在运行
        if self.is_task_pending(session_id):
            logger.debug(f"[{session_id[:8]}] 异步摘要任务已在运行，跳过")
            return

        # 检查队列大小
        async with self._lock:
            # 清理已完成的任务
            done_sessions = [s for s, t in self._pending_tasks.items() if t.done()]
            for s in done_sessions:
                del self._pending_tasks[s]

            if len(self._pending_tasks) >= ASYNC_SUMMARY_CONFIG.get("max_pending_tasks", 100):
                logger.warning(f"[{session_id[:8]}] 异步摘要队列已满，跳过")
                return

            # 创建后台任务
            task = asyncio.create_task(
                self._generate_summary_background(session_id, messages, manager, user_content)
            )
            self._pending_tasks[session_id] = task
            self._stats["async_tasks"] += 1

        logger.info(f"[{session_id[:8]}] 🚀 启动后台摘要任务")

    async def _generate_summary_background(
        self,
        session_id: str,
        messages: list,
        manager: "HistoryManager",
        user_content: str
    ):
        """后台生成摘要任务"""
        try:
            timeout = ASYNC_SUMMARY_CONFIG.get("task_timeout", 30)

            # 计算原始消息的 token 数（用于计费模拟）
            original_tokens = 0
            for msg in messages:
                content = msg.get("content", "")
                if isinstance(content, str):
                    original_tokens += estimate_tokens(content)
                elif isinstance(content, list):
                    for item in content:
                        if isinstance(item, dict):
                            original_tokens += estimate_tokens(str(item.get("text", "") or item.get("content", "")))

            # 使用 asyncio.wait_for 添加超时
            processed_messages = await asyncio.wait_for(
                manager.pre_process_async(messages, user_content, call_kiro_for_summary),
                timeout=timeout
            )

            # 从处理后的消息中提取摘要，并计算摘要 token 数
            summary = ""
            cached_tokens = 0
            for msg in processed_messages:
                content = msg.get("content", "")
                if isinstance(content, str):
                    cached_tokens += estimate_tokens(content)
                    if "[历史摘要]" in content:
                        summary = content

            if summary:
                # 更新缓存，包含 token 信息
                self._summary_cache[session_id] = {
                    "summary": summary,
                    "message_count": len(messages),
                    "timestamp": time.time(),
                    "processed_messages": processed_messages,
                    "original_tokens": original_tokens,
                    "cached_tokens": cached_tokens,
                }
                saved = original_tokens - cached_tokens
                self._stats["tokens_saved"] += max(0, saved)
                logger.info(f"[{session_id[:8]}] ✅ 后台摘要完成: {original_tokens} -> {cached_tokens} tokens (节省 {saved})")
            else:
                logger.debug(f"[{session_id[:8]}] 后台摘要完成，但无摘要内容")

        except asyncio.TimeoutError:
            logger.warning(f"[{session_id[:8]}] ⚠️ 后台摘要超时")
        except Exception as e:
            logger.error(f"[{session_id[:8]}] ❌ 后台摘要失败: {e}")

    def get_cached_processed_messages(self, session_id: str) -> list | None:
        """获取缓存的已处理消息（包含摘要）"""
        cache_entry = self._summary_cache.get(session_id)
        if cache_entry:
            return cache_entry.get("processed_messages")
        return None

    def get_stats(self) -> dict:
        """获取统计信息"""
        return {
            **self._stats,
            "cache_size": len(self._summary_cache),
            "pending_tasks": len([t for t in self._pending_tasks.values() if not t.done()]),
        }


# 全局异步摘要管理器实例
async_summary_manager = AsyncSummaryManager()


# ==================== Token 计数 ====================

# Token 估算缓存 - 避免对相同文本重复计算
# 使用文本哈希作为缓存键，避免存储大量文本
@lru_cache(maxsize=2048)
def _estimate_tokens_cached(text_hash: int, text_len: int, chinese_ratio_pct: int) -> int:
    """基于文本特征的 token 估算（带缓存）

    Args:
        text_hash: 文本的哈希值
        text_len: 文本长度
        chinese_ratio_pct: 中文字符占比（0-100）

    Returns:
        估算的 token 数量
    """
    chinese_chars = int(text_len * chinese_ratio_pct / 100)
    other_chars = text_len - chinese_chars

    # 中文约 1.5 字符/token，其他约 4 字符/token
    chinese_tokens = chinese_chars / 1.5
    other_tokens = other_chars / 4

    return int(chinese_tokens + other_tokens)


def estimate_tokens(text: str) -> int:
    """估算文本的 token 数量（优化版，带缓存）

    简单估算规则：
    - 英文/代码：约 4 个字符 = 1 token
    - 中文：约 1.5 个字符 = 1 token
    - 混合计算取平均

    优化：
    - 使用 LRU 缓存避免重复计算
    - 对于短文本直接计算，避免缓存开销
    """
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


def estimate_messages_tokens(messages: list, system: str = "") -> int:
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


# ==================== API 端点 ====================

@app.get("/")
@app.get("/v1/health")
@app.get("/api/v1/health")
@app.get("/api/v8/health")
async def root():
    """健康检查 - 支持多种路径以兼容不同客户端"""
    return {
        "status": "ok",
        "service": "AI History Manager",
        "version": "1.0.0",
        "timestamp": time.time()
    }

@app.get("/admin/routing/stats")
async def routing_stats():
    """获取模型路由统计信息"""
    stats = model_router.get_stats()
    return {
        "status": "ok",
        "routing": {
            "enabled": MODEL_ROUTING_CONFIG.get("enabled", True),
            "stats": stats,
            "config": {
                "opus_model": MODEL_ROUTING_CONFIG.get("opus_model"),
                "sonnet_model": MODEL_ROUTING_CONFIG.get("sonnet_model"),
                "total_chars_threshold": MODEL_ROUTING_CONFIG.get("total_chars_threshold"),
                "message_count_threshold": MODEL_ROUTING_CONFIG.get("message_count_threshold"),
            }
        }
    }


@app.post("/admin/routing/reset")
async def reset_routing_stats():
    """重置路由统计"""
    model_router.stats = {"opus": 0, "sonnet": 0, "other": 0}
    return {"status": "ok", "message": "Routing stats reset"}


@app.get("/v1/models")
async def list_models():
    """列出可用模型 - Anthropic 格式"""
    return {
        "data": [
            {"id": "claude-opus-4-5-20251101", "object": "model", "created": 1699900000, "owned_by": "anthropic"},
            {"id": "claude-sonnet-4-5-20250929", "object": "model", "created": 1699900000, "owned_by": "anthropic"},
            {"id": "claude-haiku-4-5-20251001", "object": "model", "created": 1699900000, "owned_by": "anthropic"},
            {"id": "claude-sonnet-4", "object": "model", "created": 1699900000, "owned_by": "anthropic"},
            {"id": "claude-haiku-4", "object": "model", "created": 1699900000, "owned_by": "anthropic"},
            {"id": "claude-opus-4", "object": "model", "created": 1699900000, "owned_by": "anthropic"},
        ],
        "object": "list"
    }


@app.post("/v1/messages/count_tokens")
async def count_tokens(request: Request):
    """Token 计数端点 (简化实现)"""
    try:
        body = await request.json()
        # 简单估算: 约 4 字符 = 1 token
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

        estimated_tokens = total_chars // 4

        return {"input_tokens": estimated_tokens}
    except Exception as e:
        return JSONResponse(
            status_code=400,
            content={"error": {"type": "invalid_request_error", "message": str(e)}}
        )


# ==================== Anthropic -> OpenAI 转换 ====================

def extract_content_item(item: dict) -> str:
    """提取单个 content item 的文本表示

    支持的类型：
    - text: 纯文本
    - image: 图像（base64/URL）
    - document: 文档（PDF等）
    - file: 文件
    - tool_use: 工具调用
    - tool_result: 工具结果
    - thinking: 思考内容
    - code_execution_result: 代码执行结果
    - citation: 引用
    - redacted_thinking: 隐藏的思考
    """
    item_type = item.get("type", "")

    if item_type == "text":
        return item.get("text", "")

    elif item_type == "image":
        # 图像内容 - 提取描述或标记
        source = item.get("source", {})
        if source.get("type") == "base64":
            media_type = source.get("media_type", "image")
            return f"[Image: {media_type}]"
        elif source.get("type") == "url":
            url = source.get("url", "")
            return f"[Image: {url[:50]}...]" if len(url) > 50 else f"[Image: {url}]"
        return "[Image]"

    elif item_type == "document":
        # 文档内容（PDF等）
        source = item.get("source", {})
        doc_type = source.get("media_type", "document")
        doc_name = item.get("name", "document")

        # 提取文档文本内容（如果有）
        if "text" in item:
            return f"[Document: {doc_name}]\n{item.get('text', '')}"

        # 如果有 content 字段（某些 API 版本）
        if "content" in item:
            doc_content = item.get("content", "")
            if isinstance(doc_content, str):
                return f"[Document: {doc_name}]\n{doc_content}"

        return f"[Document: {doc_name} ({doc_type})]"

    elif item_type == "file":
        # 文件内容
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
        # 工具结果
        tool_id = item.get("tool_use_id", "")
        tool_content = item.get("content", "")
        is_error = item.get("is_error", False)

        # 处理 content 可能是列表的情况
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
        # 思考内容 - 不使用 <thinking> 标签（Kiro API 不支持）
        # 直接返回思考内容，或者完全跳过
        thinking_text = item.get("thinking", "")
        # 跳过思考内容，避免 Kiro API 报错
        return ""

    elif item_type == "redacted_thinking":
        # 隐藏的思考内容（跳过）
        return ""

    elif item_type == "signature":
        # 签名（用于扩展思考，跳过）
        return ""

    elif item_type == "code_execution_result":
        # 代码执行结果
        output = item.get("output", "")
        return_code = item.get("return_code", 0)
        if return_code != 0:
            return f"[Code Execution Error (exit={return_code})]\n{output}"
        return f"[Code Execution Result]\n{output}" if output else ""

    elif item_type == "citation":
        # 引用
        cited_text = item.get("cited_text", "")
        source_name = item.get("source", {}).get("name", "source")
        return f"[Citation from {source_name}]: {cited_text}" if cited_text else ""

    elif item_type == "video":
        # 视频
        source = item.get("source", {})
        return f"[Video: {source.get('url', 'embedded')}]"

    elif item_type == "audio":
        # 音频
        source = item.get("source", {})
        return f"[Audio: {source.get('url', 'embedded')}]"

    else:
        # 未知类型 - 尝试提取文本或返回类型标记
        if "text" in item:
            return item.get("text", "")
        if "content" in item:
            content = item.get("content", "")
            if isinstance(content, str):
                return content
        # 返回类型标记而非空
        return f"[{item_type}]" if item_type else ""


def clean_system_content(content: str) -> str:
    """清理 system 消息内容

    移除不应该出现在 system prompt 中的内容：
    - HTTP header 格式的内容（如 x-anthropic-billing-header）
    - 其他元数据
    """
    if not content:
        return content

    lines = content.split('\n')
    cleaned_lines = []

    for line in lines:
        # 跳过 HTTP header 格式的行 (key: value)
        if ':' in line:
            key = line.split(':')[0].strip().lower()
            # 跳过已知的 header 类型
            if key.startswith('x-') or key in [
                'content-type', 'authorization', 'user-agent',
                'accept', 'cache-control', 'cookie'
            ]:
                continue
        cleaned_lines.append(line)

    return '\n'.join(cleaned_lines).strip()


def clean_assistant_content(content: str) -> str:
    """清理 assistant 消息内容（优化版）

    移除格式化标记：
    - (no content)
    - [Calling tool: xxx]
    - <thinking>...</thinking> 标签（Kiro API 不支持）

    优化：使用预编译的正则表达式
    """
    if not content:
        return content

    # 移除 (no content) 标记
    content = content.replace("(no content)", "").strip()

    # 不再移除 [Calling tool: xxx] 标记，因为我们使用这个格式来内联工具调用

    # 移除 <thinking>...</thinking> 标签（Kiro API 不支持）
    # 保留标签内的内容，但移除标签本身（使用预编译正则）
    content = _RE_THINKING_TAG.sub(r'\1', content)

    # 移除未闭合的 <thinking> 标签（使用预编译正则）
    content = _RE_THINKING_UNCLOSED.sub('', content)
    content = _RE_THINKING_UNOPEN.sub('', content)

    # 移除 <redacted_thinking> 相关标签（使用预编译正则）
    content = _RE_REDACTED_THINKING.sub('', content)

    # 移除其他可能的 Claude 特有标签（使用预编译正则）
    content = _RE_SIGNATURE_TAG.sub('', content)

    return content.strip() if content.strip() else " "


def convert_anthropic_to_openai(anthropic_body: dict) -> dict:
    """将 Anthropic 请求转换为 OpenAI 格式

    处理 Claude Code 发送的完整 Anthropic 格式请求，包括：
    - system 消息（字符串或列表，支持缓存控制）
    - messages 消息列表（支持多模态内容）
    - tools 工具定义
    - tool_choice 工具选择
    - thinking/extended thinking 相关字段
    - 图像、文档、文件等多媒体内容

    同时包含截断保护和空消息过滤
    """
    # 截断配置（可通过环境变量调节）
    MAX_MESSAGES = ANTHROPIC_MAX_MESSAGES
    MAX_TOTAL_CHARS = ANTHROPIC_MAX_TOTAL_CHARS
    MAX_SINGLE_CONTENT = ANTHROPIC_MAX_SINGLE_CONTENT

    messages = []

    # 处理 system 消息
    system = anthropic_body.get("system", "")
    if system:
        if isinstance(system, str):
            system_content = clean_system_content(system) if ANTHROPIC_CLEAN_SYSTEM_ENABLED else system
        elif isinstance(system, list):
            # Anthropic 允许 system 为列表格式（支持缓存控制等）
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
            # 截断过长的 system 消息
            if len(system_content) > MAX_SINGLE_CONTENT:
                system_content = system_content[:MAX_SINGLE_CONTENT] + "\n...[truncated]"
            messages.append({"role": "system", "content": system_content})

    # 获取原始消息并截断
    raw_messages = anthropic_body.get("messages", [])
    if ANTHROPIC_TRUNCATE_ENABLED and len(raw_messages) > MAX_MESSAGES:
        raw_messages = raw_messages[-MAX_MESSAGES:]

    # 转换 messages
    converted_messages = []

    for msg in raw_messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")

        # 处理 content 为列表的情况 (多模态/工具调用)
        if isinstance(content, list):
            # 使用内联文本格式（网关不支持 OpenAI tool_calls）
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
                                            # Strip potential double prefix from extract_content_item
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
                        pass  # 忽略 thinking blocks
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
                converted_messages.append({
                    "role": role,
                    "content": content
                })
            elif role == "assistant":
                converted_messages.append({
                    "role": "assistant",
                    "content": ANTHROPIC_EMPTY_ASSISTANT_PLACEHOLDER
                })
        else:
            # content 是字符串
            if role == "assistant" and ANTHROPIC_CLEAN_ASSISTANT_ENABLED:
                content = clean_assistant_content(content)

            # 截断过长内容
            if ANTHROPIC_TRUNCATE_ENABLED and len(content) > MAX_SINGLE_CONTENT:
                content = content[:MAX_SINGLE_CONTENT] + "\n...[truncated]"

            # 跳过空消息
            if content.strip():
                converted_messages.append({
                    "role": role,
                    "content": content
                })

    # 合并连续同角色消息（可配置）
    if ANTHROPIC_MERGE_SAME_ROLE_ENABLED:
        merged_messages = []
        for msg in converted_messages:
            role = msg.get("role")
            if merged_messages and merged_messages[-1].get("role") == role:
                # 合并内容
                merged_messages[-1]["content"] += "\n" + msg.get("content", "")
            else:
                merged_messages.append(msg.copy())
        final_messages = merged_messages
    else:
        final_messages = converted_messages

    # 添加到主消息列表
    messages.extend(final_messages)

    # 确保至少有一条消息
    if not messages:
        messages.append({"role": "user", "content": "Hello"})

    # 确保最后一条不是 system
    if len(messages) == 1 and messages[0]["role"] == "system":
        messages.append({"role": "user", "content": "Hello"})

    # 关键修复：确保最后一条消息不是 role="tool"
    # Kiro API 需要最后一条是 user 消息
    if ANTHROPIC_ENSURE_USER_ENDING and messages and messages[-1].get("role") == "tool":
        messages.append({"role": "user", "content": "Please continue based on the tool results above."})

    # 确保消息不以 assistant 结尾（Kiro 需要 user 结尾）
    if ANTHROPIC_ENSURE_USER_ENDING and messages and messages[-1].get("role") == "assistant":
        messages.append({"role": "user", "content": "Please continue."})

    # 检查总字符数，如果超过则进一步截断
    if ANTHROPIC_TRUNCATE_ENABLED:
        total_chars = sum(len(m.get("content", "")) for m in messages)
        while total_chars > MAX_TOTAL_CHARS and len(messages) > 2:
            if messages[0].get("role") == "system":
                if len(messages) > 2:
                    messages.pop(1)
            else:
                messages.pop(0)
            total_chars = sum(len(m.get("content", "")) for m in messages)

    # 构建 OpenAI 请求
    openai_body = {
        "model": anthropic_body.get("model", "claude-sonnet-4"),
        "messages": messages,
        "stream": anthropic_body.get("stream", False),
    }

    # 流式响应时，请求包含 usage 信息
    if anthropic_body.get("stream", False):
        openai_body["stream_options"] = {"include_usage": True}

    # 转换参数
    if "max_tokens" in anthropic_body:
        openai_body["max_tokens"] = anthropic_body["max_tokens"]
    if "temperature" in anthropic_body:
        openai_body["temperature"] = anthropic_body["temperature"]
    if "top_p" in anthropic_body:
        openai_body["top_p"] = anthropic_body["top_p"]
    if "stop_sequences" in anthropic_body:
        openai_body["stop"] = anthropic_body["stop_sequences"]

    # ==================== 工具定义处理 ====================
    anthropic_tools = anthropic_body.get("tools", [])
    if anthropic_tools:
        if NATIVE_TOOLS_ENABLED:
            # 原生 OpenAI tools 格式 - Kiro 网关现已支持
            # 优势：减少 token 消耗、结构化返回、支持并行调用
            openai_body["tools"] = convert_anthropic_tools_to_openai(anthropic_tools)

            # 转换 tool_choice
            if "tool_choice" in anthropic_body:
                openai_tool_choice = convert_anthropic_tool_choice_to_openai(anthropic_body["tool_choice"])
                if openai_tool_choice:
                    openai_body["tool_choice"] = openai_tool_choice

            logger.debug(f"使用原生 tools 模式，工具数量: {len(anthropic_tools)}")
        else:
            # 降级模式：将工具定义注入系统提示
            # 模型通过 [Calling tool: xxx] 格式调用工具，响应时自动解析
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
            logger.debug(f"使用文本注入 tools 模式，工具数量: {len(anthropic_tools)}")

    return openai_body


def convert_anthropic_tools_to_openai(anthropic_tools: list) -> list:
    """
    将 Anthropic 格式的 tools 转换为 OpenAI 格式

    Anthropic 格式:
    {
        "name": "tool_name",
        "description": "...",
        "input_schema": { "type": "object", "properties": {...} }
    }

    OpenAI 格式:
    {
        "type": "function",
        "function": {
            "name": "tool_name",
            "description": "...",
            "parameters": { "type": "object", "properties": {...} }
        }
    }
    """
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
    """
    将 Anthropic 格式的 tool_choice 转换为 OpenAI 格式

    Anthropic 格式:
    - {"type": "auto"} -> "auto"
    - {"type": "any"} -> "required"
    - {"type": "tool", "name": "xxx"} -> {"type": "function", "function": {"name": "xxx"}}
    """
    if not tool_choice:
        return None

    tc_type = tool_choice.get("type", "")

    if tc_type == "auto":
        return "auto"
    elif tc_type == "any":
        return "required"
    elif tc_type == "tool":
        return {
            "type": "function",
            "function": {"name": tool_choice.get("name", "")}
        }

    return None


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


def escape_json_string_newlines(json_str: str) -> str:
    """转义 JSON 字符串值中的原始换行符和控制字符

    当模型输出的 JSON 在字符串值中包含未转义的换行符时，
    标准 JSON 解析会失败。此函数将这些控制字符正确转义。
    """
    result = []
    in_string = False
    escape = False
    i = 0

    while i < len(json_str):
        c = json_str[i]

        if escape:
            # 正常的转义序列，保持原样
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
            # 在字符串内部，转义控制字符
            if c == '\n':
                result.append('\\n')
            elif c == '\r':
                result.append('\\r')
            elif c == '\t':
                result.append('\\t')
            elif ord(c) < 32:
                # 其他控制字符
                result.append(f'\\u{ord(c):04x}')
            else:
                result.append(c)
        else:
            result.append(c)

        i += 1

    return ''.join(result)


def _try_parse_json(json_str: str, end_pos: int) -> tuple[dict, int]:
    """尝试多种方式解析 JSON 字符串（优化版）

    Args:
        json_str: JSON 字符串
        end_pos: 成功时返回的结束位置

    Returns:
        (parsed_json, end_position) 或抛出异常

    优化：
    - 快速路径：直接解析成功则立即返回
    - 使用预编译的正则表达式
    - 减少不必要的字符串操作
    """
    # 快速路径：直接解析
    try:
        return json.loads(json_str), end_pos
    except json.JSONDecodeError:
        pass

    # 进入修复路径
    return _try_repair_json(json_str, end_pos)


def _try_repair_json(json_str: str, end_pos: int) -> tuple[dict, int]:
    """尝试修复并解析 JSON 字符串

    仅在直接解析失败时调用，避免不必要的修复尝试
    """
    # 修复策略 1: 移除尾随逗号（使用预编译正则）
    try:
        fixed = _RE_TRAILING_COMMA_OBJ.sub('}', json_str)
        fixed = _RE_TRAILING_COMMA_ARR.sub(']', fixed)
        return json.loads(fixed), end_pos
    except json.JSONDecodeError:
        pass

    # 修复策略 2: 转义字符串内的控制字符
    try:
        fixed = escape_json_string_newlines(json_str)
        return json.loads(fixed), end_pos
    except json.JSONDecodeError:
        pass

    # 修复策略 3: 组合修复
    try:
        fixed = escape_json_string_newlines(json_str)
        fixed = _RE_TRAILING_COMMA_OBJ.sub('}', fixed)
        fixed = _RE_TRAILING_COMMA_ARR.sub(']', fixed)
        return json.loads(fixed), end_pos
    except json.JSONDecodeError:
        pass

    # 修复策略 4: 处理截断的字符串值
    try:
        quote_count = json_str.count('"') - json_str.count('\\"')
        if quote_count % 2 == 1:
            fixed = json_str.rstrip()
            if not fixed.endswith('"'):
                fixed = fixed + '"'
            open_braces = fixed.count('{') - fixed.count('}')
            if open_braces > 0:
                fixed = fixed + '}' * open_braces
            return json.loads(fixed), end_pos
    except json.JSONDecodeError:
        pass

    # 修复策略 5: 提取有效的 JSON 子集
    try:
        decoder = json.JSONDecoder()
        obj, idx = decoder.raw_decode(json_str)
        return obj, end_pos
    except json.JSONDecodeError:
        pass

    raise json.JSONDecodeError("Failed to parse JSON after all recovery attempts", json_str, 0)


def extract_json_from_position(text: str, start: int) -> tuple[dict, int]:
    """从指定位置提取 JSON 对象，支持任意嵌套深度并处理 Markdown 包装

    Args:
        text: 源文本
        start: 开始搜索的位置

    Returns:
        (parsed_json, end_position) 或抛出异常

    优化：使用预编译的正则表达式
    """
    # 跳过空白找到 '{' 或 Markdown 代码块标记
    pos = start
    while pos < len(text) and text[pos] in ' \t\n\r':
        pos += 1

    # 检查是否以 ```json 或 ``` 开头（使用预编译正则）
    markdown_match = _RE_MARKDOWN_START.match(text[pos:])
    is_markdown_wrapped = False
    if markdown_match:
        is_markdown_wrapped = True
        pos += markdown_match.end()
        # 跳过 markdown 标记后的空白
        while pos < len(text) and text[pos] in ' \t\n\r':
            pos += 1

    if pos >= len(text) or text[pos] != '{':
        raise ValueError(f"No JSON object found at position {start}")

    # 使用括号计数来找到匹配的 '}'
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

        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                json_str = text[json_start:pos + 1]
                parsed_json, _ = _try_parse_json(json_str, pos + 1)

                # 如果是 markdown 包装的，还需要跳过结尾标记（使用预编译正则）
                end_pos = pos + 1
                if is_markdown_wrapped:
                    remaining = text[end_pos:]
                    end_match = _RE_MARKDOWN_END.search(remaining)
                    if end_match:
                        end_pos += end_match.end()

                return parsed_json, end_pos

        pos += 1

    # JSON 不完整 - 尝试智能修复
    incomplete_json = text[json_start:]
    
    # 策略 1: 尝试强制闭合 JSON
    if depth > 0:
        # 补全缺失的引号和括号
        repaired_json = incomplete_json
        if in_string:
            repaired_json += '"'
        repaired_json += '}' * depth
        
        try:
            parsed_json, _ = _try_parse_json(repaired_json, len(text))
            logger.warning(f"JSON was incomplete (depth={depth}), auto-repaired successfully")
            return parsed_json, len(text)
        except Exception:
            pass

    # 策略 2: 查找最后一个可能的有效 JSON
    for i in range(len(text) - 1, json_start, -1):
        if text[i] == '}':
            try:
                candidate = text[json_start:i+1]
                parsed_json, _ = _try_parse_json(candidate, i + 1)
                return parsed_json, i + 1
            except Exception:
                continue

    raise ValueError("Incomplete or malformed JSON object")


def iter_text_chunks(text: str, chunk_size: int):
    """将文本分块，用于流式输出"""
    if chunk_size <= 0:
        yield text
        return
    for i in range(0, len(text), chunk_size):
        yield text[i:i + chunk_size]


def split_thinking_blocks(text: str) -> list[dict]:
    """将文本按 <thinking> 标签拆分为 text/thinking blocks"""
    import re

    if not text:
        return []

    lower = text.lower()
    open_pos = lower.rfind("<thinking>")
    close_pos = lower.rfind("</thinking>")
    if open_pos != -1 and (close_pos == -1 or close_pos < open_pos):
        prefix = text[:open_pos]
        thinking = text[open_pos + len("<thinking>"):]
        blocks = []
        if prefix and prefix.strip():
            blocks.append({"type": "text", "text": prefix})
        if thinking and thinking.strip():
            blocks.append({"type": "thinking", "thinking": thinking})
        return blocks

    blocks = []
    pattern = re.compile(r"<thinking>(.*?)</thinking>", re.IGNORECASE | re.DOTALL)
    last_end = 0
    for match in pattern.finditer(text):
        if match.start() > last_end:
            prefix = text[last_end:match.start()]
            if prefix and prefix.strip():
                blocks.append({"type": "text", "text": prefix})
        thinking_text = match.group(1)
        if thinking_text and thinking_text.strip():
            blocks.append({"type": "thinking", "thinking": thinking_text})
        last_end = match.end()

    if last_end < len(text):
        suffix = text[last_end:]
        if suffix and suffix.strip():
            blocks.append({"type": "text", "text": suffix})

    return blocks


def expand_thinking_blocks(blocks: list[dict]) -> list[dict]:
    """将 text block 内的 thinking 标签展开为独立 block"""
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
    """将 OpenAI tool_calls 转换为 Anthropic tool_use blocks"""
    blocks = []
    for tc in tool_calls or []:
        func = tc.get("function", {}) or {}
        name = func.get("name") or tc.get("name") or "unknown"
        args_str = func.get("arguments") or tc.get("arguments") or ""
        tool_id = tc.get("id") or f"toolu_{uuid.uuid4().hex[:12]}"

        if not args_str:
            parsed_input = {}
        else:
            try:
                parsed_input = json.loads(args_str)
            except json.JSONDecodeError:
                try:
                    parsed_input = _try_parse_json(args_str, len(args_str))[0]
                except Exception as e:
                    parsed_input = {"_raw": args_str, "_parse_error": str(e)}

        blocks.append({
            "type": "tool_use",
            "id": tool_id,
            "name": name,
            "input": parsed_input,
        })

    return blocks


def parse_xml_tool_params(xml_content: str) -> dict:
    """解析 XML 格式的工具参数

    例如: <path>/etc/hostname</path> -> {"path": "/etc/hostname"}
    """
    params = {}
    for match in _RE_XML_PARAM.finditer(xml_content):
        param_name = match.group(1)
        param_value = match.group(2).strip()
        # 尝试解析 JSON 值（支持嵌套对象）
        try:
            params[param_name] = json.loads(param_value)
        except (json.JSONDecodeError, ValueError):
            params[param_name] = param_value
    return params


def parse_xml_tool_blocks(text: str) -> list[dict]:
    """解析 XML 格式的工具调用（Kiro 返回格式）

    检测格式:
    <ToolName>
    <param1>value1</param1>
    <param2>value2</param2>
    </ToolName>

    返回保持顺序的 blocks 列表，包含 text 和 tool_use 类型
    """
    blocks = []
    last_end = 0

    for match in _RE_XML_TOOL_CALL.finditer(text):
        # 提取工具调用前的文本
        before_text = text[last_end:match.start()]
        if before_text and before_text.strip():
            blocks.append({"type": "text", "text": before_text})

        tool_name = match.group(1)
        xml_content = match.group(2)

        # 解析 XML 参数
        params = parse_xml_tool_params(xml_content)

        blocks.append({
            "type": "tool_use",
            "id": f"toolu_{uuid.uuid4().hex[:12]}",
            "name": tool_name,
            "input": params,
        })

        last_end = match.end()

    # 添加剩余文本
    if last_end < len(text):
        remaining = text[last_end:]
        if remaining and remaining.strip():
            blocks.append({"type": "text", "text": remaining})

    return blocks


def parse_inline_tool_blocks(text: str) -> list[dict]:
    """解析内联工具调用，保留文本与工具调用顺序（优化版）

    优化：使用预编译的正则表达式
    """
    blocks = []
    last_end = 0
    pos = 0

    while pos < len(text):
        # 使用预编译正则匹配 [Calling tool: name]
        match = _RE_TOOL_CALL.search(text[pos:])
        if not match:
            break

        match_start = pos + match.start()
        match_end = pos + match.end()

        # 提取工具调用前的文本
        before_text = text[last_end:match_start]
        if before_text and before_text.strip():
            blocks.append({"type": "text", "text": before_text})

        tool_name = match.group(1).strip()
        after_match = text[match_end:]

        # 查找 Input: 标记（使用预编译正则）
        input_match = _RE_INPUT_PREFIX.match(after_match)

        if input_match:
            json_start_pos = match_end + input_match.end()
            try:
                # 使用改进的 extract_json_from_position 进行解析
                input_json, json_end_pos = extract_json_from_position(text, json_start_pos)
                blocks.append({
                    "type": "tool_use",
                    "id": f"toolu_{uuid.uuid4().hex[:12]}",
                    "name": tool_name,
                    "input": input_json,
                })
                last_end = json_end_pos
                pos = json_end_pos
                continue
            except Exception as e:
                logger.warning(f"JSON parse failed for tool {tool_name} at pos {json_start_pos}: {e}")

                # 备选方案：如果 extract_json_from_position 失败，尝试定位下一个标记并提取中间文本
                # 标记包括：下一个工具调用、工具结果、或者文本结尾（使用预编译正则）
                next_marker = _RE_NEXT_MARKER.search(after_match[input_match.end():])
                if next_marker:
                    raw_text = after_match[input_match.end():input_match.end() + next_marker.start()].strip()
                else:
                    raw_text = after_match[input_match.end():].strip()

                # 尝试再次解析这个片段
                try:
                    input_json, _ = _try_parse_json(raw_text, 0)
                    blocks.append({
                        "type": "tool_use",
                        "id": f"toolu_{uuid.uuid4().hex[:12]}",
                        "name": tool_name,
                        "input": input_json,
                    })
                    last_end = match_end + input_match.end() + len(raw_text)
                    pos = last_end
                    continue
                except Exception as e:
                    # 记录原始文本以便调试
                    blocks.append({
                        "type": "tool_use",
                        "id": f"toolu_{uuid.uuid4().hex[:12]}",
                        "name": tool_name,
                        "input": {"_raw": raw_text[:2000], "_parse_error": str(e)},
                    })
                    last_end = match_end + input_match.end() + len(raw_text)
                    pos = last_end
                    continue

        # 如果没有找到 Input:，或者格式完全不匹配，将标记本身作为文本保留
        marker_text = text[match_start:match_end]
        if marker_text and marker_text.strip():
            blocks.append({"type": "text", "text": marker_text})
        last_end = match_end
        pos = match_end

    # 添加剩余文本
    if last_end < len(text):
        remaining = text[last_end:]
        if remaining and remaining.strip():
            blocks.append({"type": "text", "text": remaining})

    # 如果没有找到 [Calling tool: ...] 格式的工具调用，
    # 尝试解析 XML 格式的工具调用（Kiro 返回格式）
    has_tool_use = any(b.get("type") == "tool_use" for b in blocks)
    if not has_tool_use and _RE_XML_TOOL_CALL.search(text):
        logger.debug("No [Calling tool:] format found, trying XML format")
        return parse_xml_tool_blocks(text)

    return blocks


def parse_inline_tool_calls(text: str) -> tuple[list, str]:
    """解析内联的工具调用文本，转换为 Anthropic tool_use content blocks

    检测格式 (支持多种变体):
    [Calling tool: tool_name]
    Input: {"arg": "value"}

    或者带缩进:
    [Calling tool: tool_name]
      Input: {"arg": "value"}

    Returns:
        (tool_use_blocks, remaining_text)
    """
    import re

    blocks = parse_inline_tool_blocks(text)
    tool_uses = [b for b in blocks if b.get("type") == "tool_use"]
    remaining_parts = []
    for block in blocks:
        if block.get("type") == "text":
            text_part = block.get("text", "").strip()
            if text_part:
                remaining_parts.append(text_part)
    remaining_text = "\n".join(remaining_parts)
    return tool_uses, remaining_text


# ==================== 智能接续机制 ====================

class TruncationInfo:
    """截断信息封装类"""
    def __init__(self):
        self.is_truncated = False
        self.reason = None
        self.truncated_text = ""
        self.valid_tool_uses = []
        self.failed_tool_uses = []
        self.stream_completed = False
        self.finish_reason = "end_turn"

    def __repr__(self):
        return f"TruncationInfo(truncated={self.is_truncated}, reason={self.reason}, valid_tools={len(self.valid_tool_uses)}, failed_tools={len(self.failed_tool_uses)})"


def detect_truncation(full_text: str, stream_completed: bool, finish_reason: str, request_id: str) -> TruncationInfo:
    """检测响应是否被截断，返回详细的截断信息

    检测策略：
    1. 流未正常完成（EOF/连接中断）
    2. finish_reason 是 max_tokens 或 length
    3. 工具调用解析失败
    """
    info = TruncationInfo()
    info.truncated_text = full_text
    info.stream_completed = stream_completed
    info.finish_reason = finish_reason

    # 检测1: 流未正常完成
    if not stream_completed:
        info.is_truncated = True
        info.reason = "stream_interrupted"
        logger.warning(f"[{request_id}] 截断检测: 流未正常完成")

    # 检测2: finish_reason 表示达到上限
    if finish_reason in ("max_tokens", "length"):
        info.is_truncated = True
        info.reason = "max_tokens_reached"
        logger.warning(f"[{request_id}] 截断检测: finish_reason={finish_reason}")

    # 解析工具调用
    tool_uses, remaining_text = parse_inline_tool_calls(full_text)

    # 检测4: 检查解析结果中是否有错误
    parse_error_should_truncate = (not stream_completed) or (finish_reason in ("max_tokens", "length"))
    for tu in tool_uses:
        inp = tu.get("input", {})
        if isinstance(inp, dict) and ("_parse_error" in inp or "_raw" in inp):
            info.failed_tool_uses.append(tu)
            if parse_error_should_truncate and not info.is_truncated:
                info.is_truncated = True
                info.reason = f"tool_parse_error in {tu.get('name', 'unknown')}"
                logger.warning(f"[{request_id}] 截断检测: 工具解析失败 - {tu.get('name')}")
            elif not parse_error_should_truncate:
                logger.warning(f"[{request_id}] 工具解析失败但流已完成，跳过续传: {tu.get('name')}")
        else:
            info.valid_tool_uses.append(tu)

    return info


# 续传请求验证配置
CONTINUATION_VALIDATION = {
    # 最小有效文本长度（低于此值不进行续传）
    "min_text_length": 10,
    # 最大连续失败次数（超过后停止续传）
    "max_consecutive_failures": 3,
    # 空响应时的降级策略
    "empty_response_action": "skip",  # skip | retry_with_lower_tokens | error
}


def validate_continuation_text(truncated_text: str, request_id: str) -> tuple[bool, str]:
    """验证截断文本是否有效，决定是否应该续传

    Returns:
        (is_valid, reason)
    """
    config = CONTINUATION_VALIDATION
    min_length = config.get("min_text_length", 10)

    # 检查是否为空或过短
    if not truncated_text:
        return False, "截断文本为空"

    stripped_text = truncated_text.strip()
    if len(stripped_text) < min_length:
        return False, f"截断文本过短 ({len(stripped_text)} < {min_length})"

    # 检查是否只包含错误信息
    error_markers = ["[上游服务错误]", "[Tool Error]", "Error:", "error:"]
    for marker in error_markers:
        if stripped_text.startswith(marker):
            return False, f"截断文本是错误信息: {marker}"

    return True, "有效"


def build_continuation_request(
    original_messages: list,
    truncated_text: str,
    original_body: dict,
    continuation_count: int,
    request_id: str
) -> tuple[dict | None, bool, str]:
    """构建续传请求（增强版，带验证）

    策略：
    1. 验证截断文本是否有效
    2. 保留原始消息历史
    3. 添加截断的 assistant 响应
    4. 添加续传提示作为新的 user 消息

    Returns:
        (request_body, should_continue, reason)
        - request_body: 续传请求体，如果不应续传则为 None
        - should_continue: 是否应该继续续传
        - reason: 决策原因
    """
    config = CONTINUATION_CONFIG

    # ==================== 关键修复：验证截断文本 ====================
    is_valid, validation_reason = validate_continuation_text(truncated_text, request_id)
    if not is_valid:
        logger.warning(f"[{request_id}] 续传验证失败: {validation_reason}，停止续传")
        return None, False, validation_reason

    # 获取截断结尾（用于续传提示）
    ending_chars = config.get("truncated_ending_chars", 500)
    truncated_ending = truncated_text[-ending_chars:] if len(truncated_text) > ending_chars else truncated_text

    # 构建续传提示
    continuation_prompt = config.get("continuation_prompt", "").format(
        truncated_ending=truncated_ending
    )

    # 构建新的消息列表
    new_messages = list(original_messages)  # 复制原始消息

    # 添加截断的 assistant 响应
    new_messages.append({
        "role": "assistant",
        "content": truncated_text
    })

    # 添加续传提示
    new_messages.append({
        "role": "user",
        "content": continuation_prompt
    })

    # 构建新的请求体
    new_body = dict(original_body)
    new_body["messages"] = new_messages

    # 使用续传专用的 max_tokens
    new_body["max_tokens"] = config.get("continuation_max_tokens", 8192)

    logger.info(f"[{request_id}] 构建续传请求 #{continuation_count + 1}: "
                f"原始消息={len(original_messages)}, 新消息={len(new_messages)}, "
                f"截断文本长度={len(truncated_text)}, 截断结尾预览={truncated_ending[:100]}...")

    return new_body, True, "验证通过"


def merge_responses(original_text: str, continuation_text: str, request_id: str) -> str:
    """合并原始响应和续传响应，增强 JSON 边界处理（优化版）

    策略：
    1. 检测续传响应是否有重复内容
    2. 智能拼接，特别处理 JSON 截断点
    3. 修复可能出现的转义冲突

    优化：使用预编译的正则表达式
    """
    if not continuation_text:
        return original_text

    # 清理续传响应开头可能的重复内容或提示
    continuation_clean = continuation_text.lstrip()

    # 移除模型可能添加的续传引导词（使用预编译正则）
    for pattern in _RE_CONTINUATION_INTRO:
        match = pattern.match(continuation_clean)
        if match:
            continuation_clean = continuation_clean[match.end():].lstrip()

    # 检查重叠
    overlap_check_len = min(2000, len(original_text), len(continuation_clean))
    if overlap_check_len > 0:
        original_ending = original_text[-overlap_check_len:]
        for i in range(overlap_check_len, 0, -1):
            if continuation_clean.startswith(original_ending[-i:]):
                # 发现重叠，剥离重复部分
                continuation_clean = continuation_clean[i:]
                logger.info(f"[{request_id}] Merge: stripped {i} chars overlap")
                break

    # 智能拼接
    # 如果原始文本以反斜杠结尾，可能正在转义字符
    if original_text.endswith('\\') and not original_text.endswith('\\\\'):
        # 这是一个未完成的转义序列
        merged = original_text + continuation_clean
    elif original_text.rstrip().endswith(('"', '{', '[', ',', ':', ' ')):
        # 处于 JSON 结构或值中间，直接拼接
        merged = original_text + continuation_clean
    else:
        # 其他情况，尝试平滑过渡
        merged = original_text + continuation_clean

    logger.info(f"[{request_id}] Combined response: orig={len(original_text)}, cont={len(continuation_text)} -> final={len(merged)}")
    return merged


async def fetch_with_continuation(
    openai_body: dict,
    headers: dict,
    request_id: str,
    model: str,
) -> tuple[str, str, bool, dict, list]:
    """带接续机制的请求获取

    Returns:
        (full_text, finish_reason, stream_completed, usage_info, tool_calls)
    """
    config = CONTINUATION_CONFIG
    max_continuations = config.get("max_continuations", 3)

    accumulated_text = ""
    continuation_count = 0
    consecutive_failures = 0  # 连续失败计数（用于智能停止）
    final_finish_reason = "end_turn"
    final_stream_completed = False
    total_input_tokens = 0
    total_output_tokens = 0
    aggregated_tool_calls = []

    current_body = dict(openai_body)
    original_messages = list(openai_body.get("messages", []))

    while continuation_count <= max_continuations:
        # 发起请求
        text, finish_reason, stream_completed, usage, tool_calls = await _fetch_single_stream(
            current_body, headers, request_id, continuation_count
        )

        # 累积 token 计数
        total_input_tokens += usage.get("input_tokens", 0)
        total_output_tokens += usage.get("output_tokens", 0)

        # 合并响应
        if continuation_count == 0:
            accumulated_text = text
        else:
            accumulated_text = merge_responses(accumulated_text, text, request_id)

        # ==================== 幻觉检测 ====================
        # 检测 AI 是否生成了虚假的工具结果
        has_hallucination, cleaned_text, hallucination_reason = detect_hallucinated_tool_result(
            accumulated_text, request_id
        )
        if has_hallucination:
            logger.warning(f"[{request_id}] 检测到幻觉，清理后继续: {hallucination_reason}")
            accumulated_text = cleaned_text
            # 幻觉通常意味着模型在等待工具结果时产生了错误输出
            # 清理后应该停止续传，让系统正常处理工具调用
            final_finish_reason = "end_turn"
            final_stream_completed = True
            break

        if tool_calls:
            aggregated_tool_calls.extend(tool_calls)

        # ==================== 增强错误处理 ====================
        # 关键：如果上游返回错误，不要续传
        if finish_reason in ("error", "timeout"):
            logger.warning(f"[{request_id}] 上游返回错误 ({finish_reason})，停止续传")
            final_finish_reason = "end_turn"  # 返回 end_turn 避免触发 CLI 错误
            final_stream_completed = True
            break

        # 检测本次请求是否获得了有效内容
        current_text_len = len(text.strip()) if text else 0
        if current_text_len == 0 and continuation_count > 0:
            # 续传请求返回空内容，增加失败计数
            consecutive_failures += 1
            logger.warning(f"[{request_id}] 续传请求 #{continuation_count} 返回空内容，连续失败={consecutive_failures}")

            # 检查是否超过最大连续失败次数
            max_failures = CONTINUATION_VALIDATION.get("max_consecutive_failures", 3)
            if consecutive_failures >= max_failures:
                logger.error(f"[{request_id}] 连续 {consecutive_failures} 次续传失败，停止续传")
                final_finish_reason = "end_turn"
                final_stream_completed = True
                break
        else:
            # 获得了有效内容，重置失败计数
            consecutive_failures = 0

        # 检测是否需要续传
        truncation_info = detect_truncation(accumulated_text, stream_completed, finish_reason, request_id)

        if not truncation_info.is_truncated:
            # 没有截断，正常完成
            final_finish_reason = finish_reason
            final_stream_completed = True
            logger.info(f"[{request_id}] 请求完成: 无截断, 总续传次数={continuation_count}")
            break

        # ==================== 智能续传决策 ====================
        should_continue = False
        triggers = config.get("triggers", {})

        # 基于触发条件判断
        if truncation_info.reason == "stream_interrupted" and triggers.get("stream_interrupted", True):
            should_continue = True
        elif truncation_info.reason == "max_tokens_reached" and triggers.get("max_tokens_reached", True):
            should_continue = True
        elif "incomplete_json" in str(truncation_info.reason) and triggers.get("incomplete_tool_json", True):
            should_continue = True
        elif "tool_parse_error" in str(truncation_info.reason) and triggers.get("parse_error", True):
            should_continue = True

        # 额外检查：如果累积文本为空或过短，不应续传
        accumulated_len = len(accumulated_text.strip()) if accumulated_text else 0
        min_text_for_continuation = CONTINUATION_VALIDATION.get("min_text_length", 10)
        if accumulated_len < min_text_for_continuation:
            logger.warning(f"[{request_id}] 累积文本过短 ({accumulated_len} < {min_text_for_continuation})，停止续传")
            should_continue = False

        if not should_continue:
            logger.info(f"[{request_id}] 截断但不续传: reason={truncation_info.reason}, accumulated_len={accumulated_len}")
            final_finish_reason = finish_reason
            final_stream_completed = stream_completed
            break

        if continuation_count >= max_continuations:
            logger.warning(f"[{request_id}] 达到最大续传次数 {max_continuations}，停止续传")
            final_finish_reason = "end_turn"  # 不返回 max_tokens，避免触发 CLI 错误
            final_stream_completed = False
            break

        # ==================== 关键修复：构建续传请求（带验证） ====================
        logger.info(f"[{request_id}] 触发续传 #{continuation_count + 1}: reason={truncation_info.reason}")

        # 使用新的验证版本构建续传请求
        continuation_result = build_continuation_request(
            original_messages,
            accumulated_text,
            openai_body,
            continuation_count,
            request_id
        )

        # 检查返回值类型（兼容新旧版本）
        if isinstance(continuation_result, tuple):
            # 新版本：返回 (body, should_continue, reason)
            new_body, should_build, build_reason = continuation_result
            if not should_build or new_body is None:
                logger.warning(f"[{request_id}] 续传请求构建失败: {build_reason}，停止续传")
                final_finish_reason = "end_turn"
                final_stream_completed = True
                break
            current_body = new_body
        else:
            # 旧版本兼容：直接返回 body
            current_body = continuation_result

        continuation_count += 1

    # ==================== 完成日志和降级处理 ====================
    final_text_len = len(accumulated_text.strip()) if accumulated_text else 0
    final_tool_count = len(aggregated_tool_calls)

    # 判断是否需要降级处理
    if final_text_len == 0 and final_tool_count == 0 and continuation_count > 0:
        # 多次续传后仍然没有有效内容，记录详细警告
        logger.error(f"[{request_id}] ⚠️ 续传失败: {continuation_count} 次续传后无有效内容")
        # 降级策略：返回友好的错误提示而不是空响应
        accumulated_text = "[系统提示] 请求处理遇到问题，请稍后重试或简化您的请求。"
        final_finish_reason = "end_turn"
        final_stream_completed = True
    elif continuation_count > 0:
        logger.info(f"[{request_id}] 🔄 接续完成: {continuation_count} 次续传, "
                    f"最终文本长度={final_text_len}, 工具调用={final_tool_count}")
    else:
        logger.info(f"[{request_id}] ✅ 请求完成: 无需续传, "
                    f"文本长度={final_text_len}, 工具调用={final_tool_count}")

    return accumulated_text, final_finish_reason, final_stream_completed, {
        "input_tokens": total_input_tokens,
        "output_tokens": total_output_tokens,
        "continuation_count": continuation_count,
        "consecutive_failures": consecutive_failures,
        "final_text_length": final_text_len,
    }, aggregated_tool_calls


async def _fetch_single_stream(
    openai_body: dict,
    headers: dict,
    request_id: str,
    continuation_count: int
) -> tuple[str, str, bool, dict, list]:
    """执行单次流式请求

    Returns:
        (text, finish_reason, stream_completed, usage, tool_calls)
    """
    full_text = ""
    finish_reason = "end_turn"
    stream_completed = False
    input_tokens = 0
    output_tokens = 0
    tool_call_acc = {}

    try:
        client = get_http_client()
        async with client.stream(
            "POST",
            KIRO_PROXY_URL,
            json=openai_body,
            headers=headers,
        ) as response:
            if response.status_code != 200:
                error_text = await response.aread()
                error_str = error_text.decode()

                # ==================== 增强错误分类和日志 ====================
                error_msg = error_str[:500]
                error_type = "unknown"
                is_retryable = False

                try:
                    error_json = json.loads(error_str)
                    error_msg = error_json.get("error", {}).get("message", error_str[:500])
                    # error_code 和 error_param 可用于未来扩展
                    # error_code = error_json.get("error", {}).get("code")
                    # error_param = error_json.get("error", {}).get("param")

                    # 分类错误类型
                    if "Improperly formed request" in error_msg:
                        error_type = "malformed_request"
                        is_retryable = False
                        logger.error(f"[{request_id}] ❌ 请求格式错误 (续传 #{continuation_count}): {error_msg[:200]}")
                    elif "token" in error_msg.lower() or "没有可用" in error_msg:
                        error_type = "token_exhausted"
                        is_retryable = False
                        logger.error(f"[{request_id}] ❌ Token 耗尽 (续传 #{continuation_count}): {error_msg[:200]}")
                    elif "rate limit" in error_msg.lower() or "too many" in error_msg.lower():
                        error_type = "rate_limit"
                        is_retryable = True
                        logger.warning(f"[{request_id}] ⚠️ 速率限制 (续传 #{continuation_count}): {error_msg[:200]}")
                    elif "timeout" in error_msg.lower():
                        error_type = "timeout"
                        is_retryable = True
                        logger.warning(f"[{request_id}] ⚠️ 超时 (续传 #{continuation_count}): {error_msg[:200]}")
                    elif response.status_code == 400:
                        error_type = "bad_request"
                        is_retryable = False
                        logger.error(f"[{request_id}] ❌ 错误请求 (续传 #{continuation_count}): {error_msg[:200]}")
                    elif response.status_code >= 500:
                        error_type = "server_error"
                        is_retryable = True
                        logger.warning(f"[{request_id}] ⚠️ 服务器错误 (续传 #{continuation_count}): {error_msg[:200]}")
                    else:
                        logger.error(f"[{request_id}] ❌ 未知错误 (续传 #{continuation_count}): status={response.status_code}, msg={error_msg[:200]}")

                except json.JSONDecodeError:
                    logger.error(f"[{request_id}] ❌ 无法解析错误响应 (续传 #{continuation_count}): {error_str[:200]}")

                # 返回错误信息，包含错误类型以便上层决策
                return f"[上游服务错误] {error_msg}", "error", True, {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "error_type": error_type,
                    "is_retryable": is_retryable,
                    "status_code": response.status_code,
                }, []

            buffer = ""
            try:
                async for chunk in response.aiter_text():
                    buffer += chunk
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        line = line.strip()
                        if not line or not line.startswith("data:"):
                            continue

                        data_str = line[5:].strip()
                        if data_str == "[DONE]":
                            stream_completed = True
                            continue

                        try:
                            data = json.loads(data_str)

                            # 获取 usage
                            usage = data.get("usage")
                            if usage:
                                input_tokens = usage.get("prompt_tokens", input_tokens)
                                output_tokens = usage.get("completion_tokens", output_tokens)

                            choice = data.get("choices", [{}])[0]
                            delta = choice.get("delta", {})
                            fr = choice.get("finish_reason")

                            if fr:
                                stream_completed = True
                                if fr == "tool_calls":
                                    finish_reason = "tool_use"
                                elif fr == "length":
                                    finish_reason = "end_turn"  # 不返回 max_tokens，续传机制会处理
                                elif fr == "stop":
                                    finish_reason = "end_turn"

                            content = delta.get("content", "")
                            if content:
                                full_text += content

                            delta_tool_calls = delta.get("tool_calls", []) or []
                            for tc in delta_tool_calls:
                                index = tc.get("index")
                                call_id = tc.get("id")
                                key = call_id or f"index_{index}" if index is not None else None
                                if not key:
                                    key = f"idx_{len(tool_call_acc)}"
                                entry = tool_call_acc.setdefault(
                                    key,
                                    {"id": call_id or f"toolu_{uuid.uuid4().hex[:12]}", "name": None, "arguments": ""}
                                )
                                if call_id:
                                    entry["id"] = call_id
                                func = tc.get("function", {}) or {}
                                if func.get("name"):
                                    entry["name"] = func.get("name")
                                if func.get("arguments"):
                                    entry["arguments"] += func.get("arguments")

                        except json.JSONDecodeError:
                            pass

            except (httpx.RemoteProtocolError, httpx.ReadError) as e:
                logger.error(f"[{request_id}] 续传请求 #{continuation_count} 流中断: {type(e).__name__}")
                stream_completed = False

    except httpx.TimeoutException:
        logger.error(f"[{request_id}] 续传请求 #{continuation_count} 超时")
        return full_text, "timeout", False, {"input_tokens": input_tokens, "output_tokens": output_tokens}, []
    except Exception as e:
        logger.error(f"[{request_id}] 续传请求 #{continuation_count} 异常: {type(e).__name__}: {e}")
        return full_text, "error", False, {"input_tokens": input_tokens, "output_tokens": output_tokens}, []

    # 估算 token（如果 API 没返回）
    if output_tokens == 0:
        output_tokens = estimate_tokens(full_text)

    logger.info(f"[{request_id}] 续传请求 #{continuation_count} 完成: "
                f"text_len={len(full_text)}, finish={finish_reason}, completed={stream_completed}")

    return full_text, finish_reason, stream_completed, {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens
    }, list(tool_call_acc.values())


def convert_openai_to_anthropic(openai_response: dict, model: str, request_id: str) -> dict:
    """将 OpenAI 响应转换为 Anthropic 格式

    支持两种工具调用格式：
    1. 原生 tool_calls（优先）- Kiro 网关原生支持
    2. 内联文本格式（降级）- [Calling tool: xxx] 格式
    """
    choice = openai_response.get("choices", [{}])[0]
    message = choice.get("message", {})
    content = message.get("content", "")
    finish_reason = choice.get("finish_reason", "stop")

    # 构建 content blocks
    content_blocks = []
    stop_reason = "end_turn"

    # 优先处理原生 tool_calls（Kiro 网关原生支持）
    tool_calls = message.get("tool_calls", [])
    has_native_tool_calls = bool(tool_calls)

    if has_native_tool_calls:
        # 原生 tool_calls 模式
        logger.debug(f"[{request_id}] 检测到原生 tool_calls: {len(tool_calls)} 个")

        # 先添加文本内容（如果有）
        if content:
            # 检查是否有 thinking 标签
            blocks = expand_thinking_blocks([{"type": "text", "text": content}])
            for block in blocks:
                if block.get("type") == "text":
                    text_value = block.get("text", "")
                    if text_value and text_value.strip():
                        content_blocks.append({"type": "text", "text": text_value})
                elif block.get("type") == "thinking":
                    content_blocks.append({"type": "thinking", "thinking": block.get("thinking", "")})

        # 添加原生 tool_calls
        content_blocks.extend(tool_calls_to_blocks(tool_calls))
        stop_reason = "tool_use"

    elif content:
        # 降级模式：解析内联的工具调用（[Calling tool: xxx] 格式）
        blocks = parse_inline_tool_blocks(content)
        blocks = expand_thinking_blocks(blocks)
        for block in blocks:
            if block.get("type") == "text":
                text_value = block.get("text", "")
                if text_value:
                    content_blocks.append({"type": "text", "text": text_value})
            elif block.get("type") == "thinking":
                content_blocks.append({"type": "thinking", "thinking": block.get("thinking", "")})
            elif block.get("type") == "tool_use":
                content_blocks.append(block)
                stop_reason = "tool_use"

    # 如果没有任何内容，添加空文本
    if not content_blocks:
        content_blocks = [{"type": "text", "text": ""}]

    # 根据 finish_reason 调整 stop_reason
    if finish_reason == "tool_calls":
        stop_reason = "tool_use"
    elif finish_reason == "length":
        stop_reason = "end_turn"  # 不返回 max_tokens，避免触发 Claude Code CLI 错误
    elif finish_reason == "stop" and stop_reason != "tool_use":
        stop_reason = "end_turn"

    return {
        "id": f"msg_{request_id}",
        "type": "message",
        "role": "assistant",
        "content": content_blocks,
        "model": model,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": openai_response.get("usage", {}).get("prompt_tokens", 0),
            "output_tokens": openai_response.get("usage", {}).get("completion_tokens", 0),
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        }
    }


@app.post("/v1/messages")
async def anthropic_messages(request: Request):
    """Anthropic /v1/messages 端点 - 通过 OpenAI 格式发送到 tokens 网关"""
    request_id = uuid.uuid4().hex[:8]

    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(400, "Invalid JSON")

    original_model = body.get("model", "claude-sonnet-4")
    stream = body.get("stream", False)
    orig_msg_count = len(body.get("messages", []))

    # ==================== max_tokens 处理 ====================
    # 确保有合理的 max_tokens，防止响应被意外截断
    DEFAULT_MAX_TOKENS = 16384  # 16K tokens 作为默认值
    MAX_ALLOWED_TOKENS = 64000  # 64K tokens 上限

    original_max_tokens = body.get("max_tokens")
    if original_max_tokens is None:
        body["max_tokens"] = DEFAULT_MAX_TOKENS
        logger.info(f"[{request_id}] 设置默认 max_tokens: {DEFAULT_MAX_TOKENS}")
    elif original_max_tokens < 1000:
        # 如果设置得太小，可能导致截断
        logger.warning(f"[{request_id}] max_tokens 较小 ({original_max_tokens})，可能导致响应截断")

    # 记录 max_tokens 以便调试
    final_max_tokens = body.get("max_tokens")

    # ==================== 智能模型路由 ====================
    # 对 Opus 请求进行智能降级判断
    routed_model, route_reason = await model_router.route(body)

    if routed_model != original_model:
        logger.info(f"[{request_id}] 🔀 模型路由: {original_model} -> {routed_model} ({route_reason})")
        # 更新请求中的模型
        body["model"] = routed_model
        model = routed_model
    else:
        model = original_model
        if "opus" in original_model.lower():
            logger.info(f"[{request_id}] ✅ 保留 Opus: {route_reason}")

    # ==================== 上下文增强 ====================
    # 在历史管理前增强用户消息
    messages = body.get("messages", [])
    session_id = generate_session_id(messages)

    # 增强最后一条用户消息（注入项目上下文）
    messages = await enhance_user_message(messages, session_id)
    body["messages"] = messages

    # ==================== 历史消息管理 ====================
    # 创建历史管理器（与 /v1/chat/completions 保持一致）
    manager = HistoryManager(HISTORY_CONFIG, cache_key=session_id)

    # 预处理消息（截断/摘要）
    user_content = extract_user_content(messages)

    # 计算原始消息大小
    original_chars = len(json.dumps(messages, ensure_ascii=False))
    logger.info(f"[{request_id}] 原始消息: {len(messages)} 条, {original_chars} 字符")

    # 检查是否需要截断/摘要
    should_summarize = manager.should_summarize(messages)
    logger.info(f"[{request_id}] 需要摘要: {should_summarize}, 阈值: {HISTORY_CONFIG.summary_threshold}")

    # ==================== 异步摘要优化 ====================
    # 核心思想：首次请求用简单截断快速响应，后台异步生成摘要供后续使用
    cache_info = {"hit": False, "original_tokens": 0, "cached_tokens": 0, "saved_tokens": 0}

    if should_summarize and ASYNC_SUMMARY_CONFIG.get("enabled", True):
        # 检查是否有缓存的摘要
        cached_summary, has_cache, original_tokens = async_summary_manager.get_cached_summary(session_id)

        if has_cache:
            # 获取缓存信息用于计费模拟
            cache_info = async_summary_manager.get_cache_info(session_id)

            # 使用缓存的已处理消息
            cached_processed = async_summary_manager.get_cached_processed_messages(session_id)
            if cached_processed:
                logger.info(f"[{request_id}] ⚡ 使用缓存摘要，跳过同步摘要 (节省 {cache_info['saved_tokens']} tokens)")
                processed_messages = cached_processed

                # 检查是否需要后台更新摘要
                if async_summary_manager.should_update_summary(session_id, len(messages)):
                    await async_summary_manager.schedule_summary_task(
                        session_id, messages, manager, user_content
                    )
            else:
                # 缓存不完整，使用简单截断
                logger.info(f"[{request_id}] ⚡ 缓存不完整，使用简单截断")
                processed_messages = manager.pre_process(messages, user_content)
                cache_info = {"hit": False, "original_tokens": 0, "cached_tokens": 0, "saved_tokens": 0}
        elif ASYNC_SUMMARY_CONFIG.get("fast_first_request", True):
            # 首次请求：使用简单截断快速响应
            logger.info(f"[{request_id}] ⚡ 首次请求，使用简单截断（后台生成摘要）")
            processed_messages = manager.pre_process(messages, user_content)

            # 启动后台摘要任务
            await async_summary_manager.schedule_summary_task(
                session_id, messages, manager, user_content
            )
        else:
            # 禁用快速首次请求，同步生成摘要（旧行为）
            logger.info(f"[{request_id}] 触发同步智能摘要...")
            processed_messages = await manager.pre_process_async(
                messages, user_content, call_kiro_for_summary
            )
    elif should_summarize:
        # 异步摘要禁用，使用同步摘要（旧行为）
        logger.info(f"[{request_id}] 触发同步智能摘要...")
        processed_messages = await manager.pre_process_async(
            messages, user_content, call_kiro_for_summary
        )

        # 与智能摘要集成：摘要时同步更新上下文
        if CONTEXT_ENHANCEMENT_CONFIG["integrate_with_summary"]:
            logger.info(f"[{request_id}] 🔄 摘要触发，同步更新项目上下文...")
            context = await extract_project_context(messages, session_id)
            if context:
                user_message_count = count_user_messages(messages)
                update_session_context(session_id, context, user_message_count)
                logger.info(f"[{request_id}] ✅ 项目上下文已更新")
    else:
        processed_messages = manager.pre_process(messages, user_content)

    if manager.was_truncated:
        logger.info(f"[{request_id}] ✂️ {manager.truncate_info}")
    else:
        logger.info(f"[{request_id}] 无需截断")

    # 更新 body 中的 messages
    body["messages"] = processed_messages

    # 使用完整转换（包含截断和空消息过滤）
    openai_body = convert_anthropic_to_openai(body)

    final_msg_count = len(openai_body.get("messages", []))
    total_chars = sum(len(str(m.get("content", ""))) for m in openai_body.get("messages", []))

    # 记录工具模式
    tools_count = len(openai_body.get("tools", []))
    tools_mode = "原生" if tools_count > 0 and NATIVE_TOOLS_ENABLED else ("文本注入" if body.get("tools") else "无")

    logger.info(f"[{request_id}] Anthropic -> OpenAI: model={model}, stream={stream}, "
                f"msgs={orig_msg_count}->{final_msg_count}, chars={total_chars}, max_tokens={final_max_tokens}, "
                f"tools={tools_count}({tools_mode})")

    # 保存调试文件（仅保留最近几个）
    debug_dir = "/tmp/ai-history-debug"
    os.makedirs(debug_dir, exist_ok=True)
    try:
        with open(f"{debug_dir}/{request_id}_converted.json", "w") as f:
            json.dump(openai_body, f, indent=2, ensure_ascii=False)
        # 清理旧文件（保留最近 10 个）- 处理并发删除的竞态条件
        try:
            debug_files = sorted(
                [f for f in os.listdir(debug_dir) if f.endswith('.json')],
                key=lambda x: os.path.getmtime(os.path.join(debug_dir, x)),
                reverse=True
            )
            for old_file in debug_files[10:]:
                try:
                    os.remove(os.path.join(debug_dir, old_file))
                except FileNotFoundError:
                    pass  # 已被其他请求删除
                except OSError:
                    pass  # 其他文件系统错误
        except OSError:
            pass  # 目录列表失败
    except Exception:
        pass  # 非关键操作，忽略所有错误

    # 构建请求头 - 添加唯一标识让 tokens 区分不同请求
    # 关键：每个请求使用不同的 X-Request-ID 和 X-Trace-ID
    # 这样 tokens 不会把多个请求当作同一终端处理
    headers = {
        "Authorization": f"Bearer {KIRO_API_KEY}",
        "Content-Type": "application/json",
        "X-Request-ID": f"req_{request_id}_{uuid.uuid4().hex[:8]}",
        "X-Trace-ID": f"trace_{uuid.uuid4().hex}",
        "X-Client-ID": f"client_{uuid.uuid4().hex[:12]}",  # 模拟不同客户端
    }

    if stream:
        return await handle_anthropic_stream_via_openai(openai_body, headers, request_id, model, cache_info)
    else:
        return await handle_anthropic_non_stream_via_openai(openai_body, headers, request_id, model, cache_info)


async def handle_anthropic_stream_via_openai(
    openai_body: dict,
    headers: dict,
    request_id: str,
    model: str,
    cache_info: dict = None,
) -> StreamingResponse:
    """处理 Anthropic 流式请求 - 通过 OpenAI 格式

    关键增强：
    1. 检测内联工具调用并转换为标准 tool_use content blocks
    2. 智能接续机制 - 当检测到截断时自动发起续传请求
    3. 高并发优化 - 使用全局 HTTP 客户端连接池
    4. Token 计数 - 支持从 OpenAI API 获取或估算 token 数量
    5. 缓存计费模拟 - 当缓存命中时，模拟 prompt caching 折扣

    策略：累积完整响应后解析，检测截断并自动续传，然后正确发送 content blocks
    """
    cache_info = cache_info or {"hit": False, "original_tokens": 0, "cached_tokens": 0, "saved_tokens": 0}

    # 预估输入 token 数
    estimated_input_tokens = 0
    for msg in openai_body.get("messages", []):
        content = msg.get("content", "")
        if isinstance(content, str):
            estimated_input_tokens += estimate_tokens(content)
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, dict):
                    estimated_input_tokens += estimate_tokens(item.get("text", ""))
                elif isinstance(item, str):
                    estimated_input_tokens += estimate_tokens(item)
        estimated_input_tokens += 4  # 每条消息额外开销

    # ==================== 缓存计费模拟 ====================
    # 当缓存命中时，将节省的 tokens 报告为 cache_read_input_tokens
    # 这样 NewAPI 会显示类似 "模型: 2.5 * 缓存: 0.1"
    cache_read_tokens = 0
    if cache_info.get("hit") and ASYNC_SUMMARY_CONFIG.get("simulate_cache_billing", True):
        # 计算缓存读取的 tokens（节省的部分）
        saved_tokens = cache_info.get("saved_tokens", 0)
        if saved_tokens > 0:
            # 将节省的 tokens 报告为 cache_read
            # Anthropic 官方 cache_read 折扣是 0.1x
            cache_read_tokens = saved_tokens
            # 实际计费的 input_tokens 减少
            estimated_input_tokens = max(0, estimated_input_tokens - saved_tokens) + cache_read_tokens
            logger.info(f"[{request_id}] 💰 缓存计费模拟: cache_read={cache_read_tokens}, 实际input={estimated_input_tokens - cache_read_tokens}")

    async def generate() -> AsyncIterator[bytes]:
        try:
            # 发送 Anthropic 流式头
            # 计算实际的 input_tokens（扣除缓存读取部分）
            actual_input_tokens = max(0, estimated_input_tokens - cache_read_tokens)
            msg_start = {
                "type": "message_start",
                "message": {
                    "id": f"msg_{request_id}",
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": model,
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {
                        "input_tokens": actual_input_tokens,
                        "output_tokens": 0,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": cache_read_tokens,
                    }
                }
            }
            yield f"data: {json.dumps(msg_start)}\n\n".encode()

            # ========== 智能接续机制 ==========
            # 使用 fetch_with_continuation 获取完整响应（自动处理截断和续传）
            if CONTINUATION_CONFIG.get("enabled", True):
                full_text, finish_reason, stream_completed, usage_info, tool_calls = await fetch_with_continuation(
                    openai_body, headers, request_id, model
                )
                input_tokens = usage_info.get("input_tokens", estimated_input_tokens)
                output_tokens = usage_info.get("output_tokens", 0)
                continuation_count = usage_info.get("continuation_count", 0)

                if continuation_count > 0:
                    logger.info(f"[{request_id}] 🔄 接续完成: {continuation_count} 次续传, "
                                f"最终文本长度={len(full_text)}")
            else:
                # 接续机制禁用，使用单次请求
                full_text, finish_reason, stream_completed, usage_info, tool_calls = await _fetch_single_stream(
                    openai_body, headers, request_id, 0
                )
                input_tokens = usage_info.get("input_tokens", estimated_input_tokens)
                output_tokens = usage_info.get("output_tokens", 0)

            # 检测最终响应是否仍有截断（接续后仍可能有问题）
            truncation_info = detect_truncation(full_text, stream_completed, finish_reason, request_id)

            # 解析内联工具调用（保序）
            blocks = parse_inline_tool_blocks(full_text)
            tool_call_blocks = tool_calls_to_blocks(tool_calls or [])
            if tool_call_blocks:
                blocks.extend(tool_call_blocks)
            blocks = expand_thinking_blocks(blocks)

            # 处理截断情况
            if truncation_info.is_truncated:
                # 过滤掉解析失败的工具调用
                valid_tools = []
                tool_call_ids = {b.get("id") for b in tool_call_blocks if b.get("id")}
                for tu in (b for b in blocks if b.get("type") == "tool_use"):
                    inp = tu.get("input", {})
                    if tu.get("id") in tool_call_ids:
                        valid_tools.append(tu)
                    elif isinstance(inp, dict) and ("_parse_error" not in inp and "_raw" not in inp):
                        valid_tools.append(tu)
                    else:
                        logger.warning(f"[{request_id}] 丢弃无效工具调用: {tu.get('name')} - "
                                       f"{inp.get('_parse_error', 'unknown error')[:100]}")

                if valid_tools:
                    blocks = [b for b in blocks if b.get("type") != "tool_use"] + valid_tools
                    logger.info(f"[{request_id}] 恢复 {len(valid_tools)} 个有效工具调用")
                else:
                    # 所有工具调用都失败，且确实发生了截断，才添加警告
                    blocks = [{"type": "text", "text": full_text}]
                    logger.warning(f"[{request_id}] 所有工具调用解析失败，回退为纯文本响应")
                    # 不添加 [⚠️ Response truncated: ...] 标记
                    # 原因：Claude Code CLI 会解析这个格式并显示为 API 错误
                    # 即使响应被截断，也让续传机制处理，不要触发 CLI 错误提示
                    pass

            # 发送 content blocks（保序）
            block_index = 0
            emitted_block = False

            for block in blocks:
                if block.get("type") == "text":
                    text_value = block.get("text", "")
                    if not text_value:
                        continue
                    emitted_block = True
                    yield (
                        f'data: {{"type":"content_block_start","index":{block_index},"content_block":'
                        f'{{"type":"text","text":""}}}}\n\n'
                    ).encode()
                    for chunk in iter_text_chunks(text_value, STREAM_TEXT_CHUNK_SIZE):
                        delta_event = {
                            "type": "content_block_delta",
                            "index": block_index,
                            "delta": {"type": "text_delta", "text": chunk}
                        }
                        yield f"data: {json.dumps(delta_event)}\n\n".encode()
                    yield f'data: {{"type":"content_block_stop","index":{block_index}}}\n\n'.encode()
                    block_index += 1
                elif block.get("type") == "thinking":
                    thinking_value = block.get("thinking", "")
                    if not thinking_value:
                        continue
                    emitted_block = True
                    yield (
                        f'data: {{"type":"content_block_start","index":{block_index},"content_block":'
                        f'{{"type":"thinking","thinking":""}}}}\n\n'
                    ).encode()
                    for chunk in iter_text_chunks(thinking_value, STREAM_THINKING_CHUNK_SIZE):
                        delta_event = {
                            "type": "content_block_delta",
                            "index": block_index,
                            "delta": {"type": "thinking_delta", "thinking": chunk}
                        }
                        yield f"data: {json.dumps(delta_event)}\n\n".encode()
                    yield f'data: {{"type":"content_block_stop","index":{block_index}}}\n\n'.encode()
                    block_index += 1
                elif block.get("type") == "tool_use":
                    emitted_block = True
                    finish_reason = "tool_use"
                    tool_start = {
                        "type": "content_block_start",
                        "index": block_index,
                        "content_block": {
                            "type": "tool_use",
                            "id": block["id"],
                            "name": block["name"],
                            "input": {}
                        }
                    }
                    yield f"data: {json.dumps(tool_start)}\n\n".encode()

                    tool_json = json.dumps(block.get("input", {}))
                    for chunk in iter_text_chunks(tool_json, STREAM_TOOL_JSON_CHUNK_SIZE):
                        delta_event = {
                            "type": "content_block_delta",
                            "index": block_index,
                            "delta": {"type": "input_json_delta", "partial_json": chunk}
                        }
                        yield f"data: {json.dumps(delta_event)}\n\n".encode()

                    yield f'data: {{"type":"content_block_stop","index":{block_index}}}\n\n'.encode()
                    block_index += 1

            if not emitted_block:
                yield f'data: {{"type":"content_block_start","index":0,"content_block":{{"type":"text","text":""}}}}\n\n'.encode()
                yield f'data: {{"type":"content_block_stop","index":0}}\n\n'.encode()

            # 如果 OpenAI 没有返回 usage，使用估算值
            if output_tokens == 0:
                output_tokens = estimate_tokens(full_text)

            # 如果检测到截断，记录详细信息
            if truncation_info.is_truncated:
                tool_count = len([b for b in blocks if b.get("type") == "tool_use"])
                logger.warning(f"[{request_id}] ⚠️ 响应截断完成: reason={truncation_info.reason}, "
                               f"text_len={len(full_text)}, tools={tool_count}, "
                               f"finish_reason={finish_reason}")

            # message delta with token usage (包含缓存信息)
            yield f'data: {{"type":"message_delta","delta":{{"stop_reason":"{finish_reason}","stop_sequence":null}},"usage":{{"output_tokens":{output_tokens},"cache_creation_input_tokens":0,"cache_read_input_tokens":{cache_read_tokens}}}}}\n\n'.encode()

            # message stop
            yield f'data: {{"type":"message_stop"}}\n\n'.encode()

        except httpx.TimeoutException:
            logger.error(f"[{request_id}] 请求超时")
            error_response = {
                "type": "error",
                "error": {"type": "timeout_error", "message": "Request timeout"}
            }
            yield f"data: {json.dumps(error_response)}\n\n".encode()
        except (httpx.RemoteProtocolError, httpx.ReadError) as e:
            # EOF / 连接中断 - 这是常见的上游错误
            logger.error(f"[{request_id}] 连接中断 (EOF): {type(e).__name__}: {e}")
            error_response = {
                "type": "error",
                "error": {"type": "stream_error", "message": f"Connection interrupted: {type(e).__name__}. Please retry."}
            }
            yield f"data: {json.dumps(error_response)}\n\n".encode()
        except Exception as e:
            logger.error(f"[{request_id}] 请求异常: {type(e).__name__}: {e}")
            error_response = {
                "type": "error",
                "error": {"type": "api_error", "message": str(e)}
            }
            yield f"data: {json.dumps(error_response)}\n\n".encode()

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
    )


async def handle_anthropic_non_stream_via_openai(
    openai_body: dict,
    headers: dict,
    request_id: str,
    model: str,
    cache_info: dict = None,
) -> JSONResponse:
    """处理 Anthropic 非流式请求 - 通过 OpenAI 格式

    高并发优化：使用全局 HTTP 客户端连接池
    支持缓存计费模拟
    """
    cache_info = cache_info or {"hit": False, "original_tokens": 0, "cached_tokens": 0, "saved_tokens": 0}

    try:
        client = get_http_client()
        response = await client.post(
            KIRO_PROXY_URL,
            json=openai_body,
            headers=headers,
        )

        if response.status_code != 200:
            error_str = response.text
            logger.error(f"[{request_id}] OpenAI API Error {response.status_code}: {error_str[:200]}")

            return JSONResponse(
                status_code=response.status_code,
                content={
                    "type": "error",
                    "error": {
                        "type": "api_error",
                        "message": error_str[:500],
                    }
                }
            )

        # 转换 OpenAI 响应为 Anthropic 格式
        openai_response = response.json()
        anthropic_response = convert_openai_to_anthropic(openai_response, model, request_id)

        # 添加缓存计费信息
        if cache_info.get("hit") and ASYNC_SUMMARY_CONFIG.get("simulate_cache_billing", True):
            saved_tokens = cache_info.get("saved_tokens", 0)
            if saved_tokens > 0 and "usage" in anthropic_response:
                original_input = anthropic_response["usage"].get("input_tokens", 0)
                # 将节省的 tokens 作为 cache_read
                anthropic_response["usage"]["cache_read_input_tokens"] = saved_tokens
                # 调整实际 input_tokens
                anthropic_response["usage"]["input_tokens"] = max(0, original_input - saved_tokens)
                logger.info(f"[{request_id}] 💰 缓存计费模拟: cache_read={saved_tokens}")

        return JSONResponse(content=anthropic_response)

    except httpx.TimeoutException:
        logger.error(f"[{request_id}] 请求超时")
        return JSONResponse(
            status_code=408,
            content={
                "type": "error",
                "error": {"type": "timeout_error", "message": "Request timeout"}
            }
        )
    except Exception as e:
        logger.error(f"[{request_id}] 请求异常: {e}")
        return JSONResponse(
            status_code=500,
            content={
                "type": "error",
                "error": {"type": "api_error", "message": str(e)}
            }
        )


# ==================== OpenAI API ====================

@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """聊天完成接口 - OpenAI 兼容"""
    start_time = time.time()
    request_id = uuid.uuid4().hex[:8]

    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(400, "Invalid JSON")

    model = body.get("model", "claude-sonnet-4")
    messages = body.get("messages", [])
    stream = body.get("stream", False)

    if not messages:
        raise HTTPException(400, "messages is required")

    logger.info(f"[{request_id}] Request: model={model}, messages={len(messages)}, stream={stream}")

    # ==================== 上下文增强 ====================
    # 在历史管理前增强用户消息
    session_id = generate_session_id(messages)
    messages = await enhance_user_message(messages, session_id)
    body["messages"] = messages

    # 创建历史管理器
    manager = HistoryManager(HISTORY_CONFIG, cache_key=session_id)

    # 预处理消息
    user_content = extract_user_content(messages)

    if manager.should_summarize(messages):
        processed_messages = await manager.pre_process_async(
            messages, user_content, call_kiro_for_summary
        )
    else:
        processed_messages = manager.pre_process(messages, user_content)

    if manager.was_truncated:
        logger.info(f"[{request_id}] {manager.truncate_info}")

    # 构建请求
    kiro_request = {
        "model": model,
        "messages": processed_messages,
        "stream": stream,
    }

    # 传递其他参数
    for key in ["temperature", "max_tokens", "top_p", "frequency_penalty", "presence_penalty", "stop"]:
        if key in body and body[key] is not None:
            kiro_request[key] = body[key]

    # ==================== 原生 Tools 支持 ====================
    # 透传 tools 和 tool_choice 参数给 Kiro 网关
    if NATIVE_TOOLS_ENABLED:
        if "tools" in body and body["tools"]:
            kiro_request["tools"] = body["tools"]
            logger.debug(f"[{request_id}] 透传 tools 参数，工具数量: {len(body['tools'])}")
        if "tool_choice" in body and body["tool_choice"]:
            kiro_request["tool_choice"] = body["tool_choice"]

    # 添加唯一请求标识
    headers = {
        "Authorization": f"Bearer {KIRO_API_KEY}",
        "Content-Type": "application/json",
        "X-Request-ID": f"chat_{request_id}_{uuid.uuid4().hex[:8]}",
        "X-Trace-ID": f"trace_{uuid.uuid4().hex}",
        "X-Client-ID": f"client_{uuid.uuid4().hex[:12]}",  # 模拟不同客户端
    }

    if stream:
        return await handle_stream(kiro_request, headers, manager, request_id, messages)
    else:
        return await handle_non_stream(kiro_request, headers, manager, request_id, messages)


async def handle_stream(
    kiro_request: dict,
    headers: dict,
    manager: HistoryManager,
    request_id: str,
    original_messages: list,
) -> StreamingResponse:
    """处理流式响应 - 使用全局 HTTP 客户端，无并发限制"""

    async def generate() -> AsyncIterator[bytes]:
        nonlocal kiro_request
        retry_count = 0
        max_retries = HISTORY_CONFIG.max_retries

        while retry_count <= max_retries:
            try:
                client = get_http_client()
                async with client.stream(
                    "POST",
                    KIRO_PROXY_URL,
                    json=kiro_request,
                    headers=headers,
                ) as response:

                    if response.status_code != 200:
                        error_text = await response.aread()
                        error_str = error_text.decode()

                        logger.error(f"[{request_id}] Kiro API Error {response.status_code}: {error_str[:200]}")

                        # 检查是否为长度错误
                        if is_content_length_error(response.status_code, error_str):
                            logger.info(f"[{request_id}] 检测到长度错误，尝试截断重试")

                            truncated, should_retry = await manager.handle_length_error_async(
                                kiro_request["messages"],
                                retry_count,
                                call_kiro_for_summary,
                            )

                            if should_retry:
                                kiro_request["messages"] = truncated
                                retry_count += 1
                                logger.info(f"[{request_id}] {manager.truncate_info}")
                                continue

                        # 返回错误
                        error_response = {
                            "error": {
                                "message": error_str[:500],
                                "type": "api_error",
                                "code": response.status_code,
                            }
                        }
                        yield f"data: {json.dumps(error_response)}\n\n".encode()
                        yield b"data: [DONE]\n\n"
                        return

                    # 正常流式响应
                    async for chunk in response.aiter_bytes():
                        yield chunk

                    return

            except httpx.TimeoutException:
                logger.error(f"[{request_id}] 请求超时")
                if retry_count < max_retries:
                    retry_count += 1
                    await asyncio.sleep(1)
                    continue

                error_response = {"error": {"message": "Request timeout", "type": "timeout_error"}}
                yield f"data: {json.dumps(error_response)}\n\n".encode()
                yield b"data: [DONE]\n\n"
                return

            except Exception as e:
                logger.error(f"[{request_id}] 请求异常: {e}")
                error_response = {"error": {"message": str(e), "type": "api_error"}}
                yield f"data: {json.dumps(error_response)}\n\n".encode()
                yield b"data: [DONE]\n\n"
                return

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
    )


async def handle_non_stream(
    kiro_request: dict,
    headers: dict,
    manager: HistoryManager,
    request_id: str,
    original_messages: list,
) -> JSONResponse:
    """处理非流式响应 - 使用全局 HTTP 客户端，无并发限制"""
    retry_count = 0
    max_retries = HISTORY_CONFIG.max_retries

    while retry_count <= max_retries:
        try:
            client = get_http_client()
            response = await client.post(
                KIRO_PROXY_URL,
                json=kiro_request,
                headers=headers,
            )

            if response.status_code != 200:
                error_str = response.text
                logger.error(f"[{request_id}] Kiro API Error {response.status_code}: {error_str[:200]}")

                # 检查是否为长度错误
                if is_content_length_error(response.status_code, error_str):
                    logger.info(f"[{request_id}] 检测到长度错误，尝试截断重试")

                    truncated, should_retry = await manager.handle_length_error_async(
                        kiro_request["messages"],
                        retry_count,
                        call_kiro_for_summary,
                    )

                    if should_retry:
                        kiro_request["messages"] = truncated
                        retry_count += 1
                        logger.info(f"[{request_id}] {manager.truncate_info}")
                        continue

                raise HTTPException(response.status_code, error_str[:500])

            return JSONResponse(content=response.json())

        except HTTPException:
            raise
        except httpx.TimeoutException:
            logger.error(f"[{request_id}] 请求超时")
            if retry_count < max_retries:
                retry_count += 1
                await asyncio.sleep(1)
                continue
            raise HTTPException(408, "Request timeout")
        except Exception as e:
            logger.error(f"[{request_id}] 请求异常: {e}")
            raise HTTPException(500, str(e))

    raise HTTPException(503, "All retries exhausted")


# ==================== 配置接口 ====================

@app.get("/admin/config")
async def get_config():
    """获取当前配置"""
    return {
        "kiro_proxy_url": KIRO_PROXY_URL,
        "history_config": HISTORY_CONFIG.to_dict(),
        "async_summary_config": ASYNC_SUMMARY_CONFIG,
        "native_tools_enabled": NATIVE_TOOLS_ENABLED,
    }


@app.get("/admin/async-summary/stats")
async def get_async_summary_stats():
    """获取异步摘要统计"""
    return {
        "config": ASYNC_SUMMARY_CONFIG,
        "stats": async_summary_manager.get_stats(),
    }


@app.get("/admin/routing/stats")
async def get_routing_stats():
    """获取模型路由统计"""
    return model_router.get_stats()


@app.post("/admin/routing/reset")
async def reset_routing_stats():
    """重置路由统计"""
    model_router.stats = {"opus": 0, "sonnet": 0, "haiku": 0, "opus_degraded": 0}
    return {"status": "ok", "message": "路由统计已重置"}


@app.post("/admin/config/history")
async def update_history_config(request: Request):
    """更新历史管理配置"""
    global HISTORY_CONFIG

    try:
        data = await request.json()
        HISTORY_CONFIG = HistoryConfig.from_dict(data)
        return {"status": "ok", "config": HISTORY_CONFIG.to_dict()}
    except Exception as e:
        raise HTTPException(400, str(e))


# ==================== 启动入口 ====================

if __name__ == "__main__":
    import uvicorn

    print(f"""
╔══════════════════════════════════════════════════════════════╗
║           AI History Manager API Server                      ║
╠══════════════════════════════════════════════════════════════╣
║  服务地址: http://0.0.0.0:{SERVICE_PORT}                          ║
║  API 端点: /v1/chat/completions                              ║
║  健康检查: /                                                 ║
║  模型列表: /v1/models                                        ║
║  配置查看: /admin/config                                     ║
╠══════════════════════════════════════════════════════════════╣
║  NewAPI 配置:                                                ║
║  - 类型: 自定义渠道 (OpenAI)                                 ║
║  - Base URL: http://your-server:{SERVICE_PORT}/v1                 ║
║  - API Key: 任意值                                           ║
╚══════════════════════════════════════════════════════════════╝
""")

    uvicorn.run(app, host="0.0.0.0", port=SERVICE_PORT)
