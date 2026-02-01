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
from typing import Optional, AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel

from ai_history_manager import HistoryManager, HistoryConfig, TruncateStrategy
from ai_history_manager.utils import is_content_length_error

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
    # 默认增加到 10 次，以应对超长输出（如 6000+ 行代码文件）
    "max_continuations": int(os.getenv("MAX_CONTINUATIONS", "10")),

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

    # 服务器端强制输出上限（防止超过 Claude Code CLI 的 32000 默认限制）
    # 设置为 28000 以确保充足的安全余量，同时提供尽可能多的内容
    "max_total_output_tokens": int(os.getenv("MAX_TOTAL_OUTPUT_TOKENS", "28000")),

    # 强制字符上限（作为第二层保护，约 11 万字符对应 28k tokens）
    "max_total_chars": int(os.getenv("MAX_TOTAL_CHARS", "112000")),

    # 日志级别
    "log_continuations": True,
}

# 历史消息管理配置
# 调整阈值，更早触发截断以避免 "Input is too long" 错误
HISTORY_CONFIG = HistoryConfig(
    strategies=[
        TruncateStrategy.PRE_ESTIMATE,      # 优先预估，提前截断
        TruncateStrategy.AUTO_TRUNCATE,     # 自动截断
        TruncateStrategy.SMART_SUMMARY,     # 智能摘要
        TruncateStrategy.ERROR_RETRY,       # 错误重试
    ],
    max_messages=25,           # 30 → 25，减少最大消息数
    max_chars=100000,          # 150000 → 100000，降低字符上限
    summary_keep_recent=8,     # 10 → 8，保留更少的最近消息
    summary_threshold=80000,   # 100000 → 80000，更早触发摘要
    retry_max_messages=15,     # 20 → 15，重试时保留更少消息
    max_retries=3,             # 2 → 3，增加重试次数
    estimate_threshold=100000, # 180000 → 100000，更早预估截断
    summary_cache_enabled=True,
    add_warning_header=True,
)

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
ANTHROPIC_TRUNCATE_ENABLED = os.getenv("ANTHROPIC_TRUNCATE_ENABLED", "true").lower() in ("1", "true", "yes")
ANTHROPIC_MAX_MESSAGES = int(os.getenv("ANTHROPIC_MAX_MESSAGES", "500"))
ANTHROPIC_MAX_TOTAL_CHARS = int(os.getenv("ANTHROPIC_MAX_TOTAL_CHARS", "600000"))
ANTHROPIC_MAX_SINGLE_CONTENT = int(os.getenv("ANTHROPIC_MAX_SINGLE_CONTENT", "300000"))
ANTHROPIC_TOOL_INPUT_MAX_CHARS = int(os.getenv("ANTHROPIC_TOOL_INPUT_MAX_CHARS", "150000"))
ANTHROPIC_TOOL_RESULT_MAX_CHARS = int(os.getenv("ANTHROPIC_TOOL_RESULT_MAX_CHARS", "200000"))
ANTHROPIC_CLEAN_SYSTEM_ENABLED = os.getenv("ANTHROPIC_CLEAN_SYSTEM_ENABLED", "false").lower() in ("1", "true", "yes")
ANTHROPIC_CLEAN_ASSISTANT_ENABLED = os.getenv("ANTHROPIC_CLEAN_ASSISTANT_ENABLED", "false").lower() in ("1", "true", "yes")
ANTHROPIC_MERGE_SAME_ROLE_ENABLED = os.getenv("ANTHROPIC_MERGE_SAME_ROLE_ENABLED", "true").lower() in ("1", "true", "yes")
ANTHROPIC_ENSURE_USER_ENDING = os.getenv("ANTHROPIC_ENSURE_USER_ENDING", "true").lower() in ("1", "true", "yes")
ANTHROPIC_EMPTY_ASSISTANT_PLACEHOLDER = os.getenv("ANTHROPIC_EMPTY_ASSISTANT_PLACEHOLDER", " ")
TOOL_DESC_MAX_CHARS = int(os.getenv("TOOL_DESC_MAX_CHARS", "8000"))
TOOL_PARAM_DESC_MAX_CHARS = int(os.getenv("TOOL_PARAM_DESC_MAX_CHARS", "4000"))

# ==================== 智能模型路由配置 ====================

# 模型路由配置 - 智能判断何时使用 Opus vs Sonnet
# 设计原则：
# 1. Opus 用于真正需要深度推理的关键时刻（创建、设计、架构）
# 2. Sonnet 用于执行性任务（工具调用、简单修改）
# 3. 保证 10-20% 的 Opus 使用比例
# 4. Extended Thinking 和 Agent 调用场景使用 Opus
MODEL_ROUTING_CONFIG = {
    # 启用智能路由
    "enabled": True,

    # 目标模型映射
    "opus_model": "claude-opus-4-5-20251101",
    "sonnet_model": "claude-sonnet-4-5-20250929",

    # ============================================================
    # 第零优先级：强制 Opus 的场景（不受其他条件影响）
    # ============================================================
    # Extended Thinking 请求 - 必须使用 Opus
    "force_opus_on_thinking": True,

    # 主 Agent 请求（非子 Agent）- 更高概率用 Opus
    "main_agent_opus_probability": 60,  # 主 Agent 60% 概率用 Opus

    # ============================================================
    # 第一优先级：强制 Opus 的关键词（最后一条用户消息包含）
    # 这些是真正需要深度思考的任务
    # ============================================================
    "force_opus_keywords": [
        # 创建类 - 完整的创建任务
        "创建项目", "新建项目", "初始化项目", "搭建项目",
        "create project", "new project", "init project",
        # 设计架构类 - 需要架构思维
        "设计架构", "系统设计", "架构设计", "方案设计", "设计",
        "design architecture", "system design", "architecture design", "design",
        # 深度分析类
        "分析", "梳理", "检查问题", "全面分析", "详细分析", "根因分析", "诊断",
        "analysis", "analyze", "diagnose", "investigate",
        # 重构类 - 大规模重构
        "重构", "整体重构", "大规模重构",
        "refactor", "major refactor", "complete refactor",
        # 规划类
        "规划", "整体规划", "系统规划", "战略规划", "计划",
        "plan", "planning", "strategy",
        # Agent/Task 调用相关
        "UI-UX", "ui-ux", "UI设计", "设计稿",
    ],

    # ============================================================
    # 第二优先级：强制 Sonnet 的关键词（执行性任务）
    # ============================================================
    "force_sonnet_keywords": [
        # 简单操作
        "看看", "显示", "列出", "打开",
        "show", "list", "display", "view", "open",
        # 小改动
        "修复", "调整", "更新", "改一下", "改成",
        "fix", "adjust", "update",
        # 执行命令
        "运行", "执行", "启动", "重启", "停止",
        "run", "execute", "start", "restart", "stop",
        # 简单问答
        "什么是", "哪里", "是不是", "有没有",
        "what is", "where", "is it", "do you",
        # 读取类
        "读取", "获取", "搜索",
        "read", "get", "search", "find",
        # 安装类
        "安装", "下载",
        "install", "download",
    ],

    # ============================================================
    # 第三优先级：基于对话阶段的智能判断
    # ============================================================

    # 首轮对话检测 - 新任务开始需要一定概率 Opus
    "first_turn_opus_probability": 90,    # 首轮 50% 概率用 Opus

    # 用户消息数阈值（不含 system）- 判断是否为首轮
    "first_turn_max_user_messages": 2,    # <= 2 条用户消息视为首轮

    # 工具执行阶段检测 - 大量工具调用说明在执行阶段
    "execution_phase_tool_calls": 5,      # 工具调用 >= 5 次视为执行阶段
    "execution_phase_sonnet_probability": 80,  # 执行阶段 90% 用 Sonnet

    # ============================================================
    # 第四优先级：保底概率（确保 10-20% Opus 使用率）
    # ============================================================
    "base_opus_probability": 30,          # 基础 15% 概率使用 Opus

    # ============================================================
    # 调试和监控
    # ============================================================
    "log_routing_decision": True,         # 记录路由决策原因
}


class ModelRouter:
    """智能模型路由器 - 根据请求复杂度决定使用 Opus 还是 Sonnet"""

    def __init__(self, config: dict = None):
        self.config = config or MODEL_ROUTING_CONFIG
        self.stats = {"opus": 0, "sonnet": 0, "other": 0}
        self._lock = asyncio.Lock()

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
        import re
        files = set()
        file_pattern = r'[/\\][\w\-\.]+\.(py|js|ts|jsx|tsx|go|rs|java|cpp|c|h|md|yaml|yml|json|toml)'

        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                matches = re.findall(file_pattern, content)
                files.update(matches)
            elif isinstance(content, list):
                for item in content:
                    if isinstance(item, dict):
                        text = item.get("text", "") or item.get("content", "")
                        if isinstance(text, str):
                            matches = re.findall(file_pattern, text)
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
        """检查文本是否包含关键词"""
        text_lower = text.lower()
        for kw in keywords:
            if kw.lower() in text_lower:
                return True
        return False

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

    def should_use_opus(self, request_body: dict) -> tuple[bool, str]:
        """
        智能判断是否应该使用 Opus

        决策优先级：
        0. Extended Thinking 请求 → 强制 Opus
        0b. 主 Agent 请求 → 高概率 Opus
        1. 强制 Opus 关键词 → Opus
        2. 强制 Sonnet 关键词 → Sonnet
        3. 首轮对话（新任务）→ 概率 Opus
        4. 执行阶段（大量工具调用）→ 高概率 Sonnet
        5. 保底概率 → 确保 ~15% Opus

        Returns:
            (should_use_opus, reason)
        """
        if not self.config.get("enabled", True):
            return True, "路由已禁用"

        messages = request_body.get("messages", [])
        last_user_msg = self._get_last_user_message(messages)

        # 生成稳定的哈希种子（相同请求得到相同结果）
        hash_seed = f"{len(messages)}:{last_user_msg[:200]}"

        # ============================================================
        # 第零优先级：特殊场景强制 Opus
        # ============================================================

        # 0a. Extended Thinking 请求 - 必须使用 Opus
        if self.config.get("force_opus_on_thinking", True) and self._has_thinking_request(request_body):
            return True, "ExtendedThinking"

        # 0b. 主 Agent 请求（非子 Agent）- 更高概率用 Opus
        is_sub_agent = self._is_sub_agent_request(messages)
        if not is_sub_agent:
            main_agent_prob = self.config.get("main_agent_opus_probability", 60)
            # 只在首轮（新任务开始）时应用主 Agent 概率
            user_msg_count = self._count_user_messages(messages)
            if user_msg_count <= 2:
                if self._get_hash_probability(hash_seed + ":main", main_agent_prob):
                    return True, f"主Agent首轮({main_agent_prob}%)"

        # ============================================================
        # 第一优先级：强制 Opus 关键词
        # ============================================================
        force_opus_keywords = self.config.get("force_opus_keywords", [])
        if self._contains_keywords(last_user_msg, force_opus_keywords):
            # 找出匹配的关键词
            matched = [kw for kw in force_opus_keywords if kw.lower() in last_user_msg.lower()]
            return True, f"关键词[{matched[0] if matched else '?'}]"

        # ============================================================
        # 第二优先级：强制 Sonnet 关键词
        # ============================================================
        force_sonnet_keywords = self.config.get("force_sonnet_keywords", [])
        if self._contains_keywords(last_user_msg, force_sonnet_keywords):
            matched = [kw for kw in force_sonnet_keywords if kw.lower() in last_user_msg.lower()]
            return False, f"简单任务[{matched[0] if matched else '?'}]"

        # ============================================================
        # 第三优先级：对话阶段判断
        # ============================================================
        user_msg_count = self._count_user_messages(messages)
        tool_calls = self._count_tool_calls(messages)

        # 3a. 首轮对话检测 - 新任务开始更需要 Opus
        first_turn_max = self.config.get("first_turn_max_user_messages", 2)
        if user_msg_count <= first_turn_max:
            first_turn_prob = self.config.get("first_turn_opus_probability", 50)
            if self._get_hash_probability(hash_seed + ":first", first_turn_prob):
                return True, f"首轮对话({user_msg_count}条,{first_turn_prob}%)"
            else:
                return False, f"首轮随机Sonnet({user_msg_count}条)"

        # 3b. 执行阶段检测 - 大量工具调用说明在执行，用 Sonnet
        execution_threshold = self.config.get("execution_phase_tool_calls", 5)
        if tool_calls >= execution_threshold:
            sonnet_prob = self.config.get("execution_phase_sonnet_probability", 90)
            if self._get_hash_probability(hash_seed + ":exec", sonnet_prob):
                return False, f"执行阶段({tool_calls}次工具,{sonnet_prob}%Sonnet)"
            else:
                return True, f"执行阶段随机Opus({tool_calls}次工具)"

        # ============================================================
        # 第四优先级：保底概率
        # ============================================================
        base_opus_prob = self.config.get("base_opus_probability", 15)
        if self._get_hash_probability(hash_seed + ":base", base_opus_prob):
            return True, f"保底概率({base_opus_prob}%)"
        else:
            return False, f"默认Sonnet(msg={user_msg_count},tools={tool_calls})"

    def route(self, request_body: dict) -> tuple[str, str]:
        """
        路由到合适的模型

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
        total = self.stats["opus"] + self.stats["sonnet"]
        if total == 0:
            ratio = "N/A"
        else:
            ratio = f"1:{self.stats['sonnet'] / max(self.stats['opus'], 1):.1f}"

        return {
            "opus_requests": self.stats["opus"],
            "sonnet_requests": self.stats["sonnet"],
            "other_requests": self.stats["other"],
            "opus_sonnet_ratio": ratio,
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


async def call_kiro_for_summary(prompt: str) -> str:
    """调用 Kiro API 生成摘要 - 使用全局 HTTP 客户端"""
    summary_id = uuid.uuid4().hex[:8]
    request_body = {
        "model": "claude-haiku-4",  # 使用快速模型
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


# ==================== Token 计数 ====================

def estimate_tokens(text: str) -> int:
    """估算文本的 token 数量

    简单估算规则：
    - 英文/代码：约 4 个字符 = 1 token
    - 中文：约 1.5 个字符 = 1 token
    - 混合计算取平均
    """
    if not text:
        return 0

    # 统计中文字符数
    chinese_chars = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    other_chars = len(text) - chinese_chars

    # 中文约 1.5 字符/token，代码/英文采用更保守的 2.5 字符/token（防止低估导致溢出）
    chinese_tokens = chinese_chars / 1.5
    other_tokens = other_chars / 2.5

    return int(chinese_tokens + other_tokens)


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
    """清理 assistant 消息内容

    移除格式化标记：
    - (no content)
    - [Calling tool: xxx]
    - <thinking>...</thinking> 标签（Kiro API 不支持）
    """
    if not content:
        return content

    import re

    # 移除 (no content) 标记
    content = content.replace("(no content)", "").strip()

    # 不再移除 [Calling tool: xxx] 标记，因为我们使用这个格式来内联工具调用

    # 移除 <thinking>...</thinking> 标签（Kiro API 不支持）
    # 保留标签内的内容，但移除标签本身
    content = re.sub(r'<thinking>(.*?)</thinking>', r'\1', content, flags=re.DOTALL)

    # 移除未闭合的 <thinking> 标签
    content = re.sub(r'<thinking>.*$', '', content, flags=re.DOTALL)
    content = re.sub(r'^.*</thinking>', '', content, flags=re.DOTALL)

    # 移除 <redacted_thinking> 相关标签
    content = re.sub(r'<redacted_thinking>.*?</redacted_thinking>', '', content, flags=re.DOTALL)

    # 移除其他可能的 Claude 特有标签
    content = re.sub(r'<signature>.*?</signature>', '', content, flags=re.DOTALL)

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
                        # 智能截断过大的工具输入
                        if len(input_str) > ANTHROPIC_TOOL_INPUT_MAX_CHARS:
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

                        # 核心优化：对 Tool Result 进行更激进的截断（它们通常是历史臃肿的根源）
                        # 如果是大型文件读取或构建日志，只保留头部和尾部
                        if len(tool_content) > ANTHROPIC_TOOL_RESULT_MAX_CHARS:
                            keep = ANTHROPIC_TOOL_RESULT_MAX_CHARS // 2
                            tool_content = (
                                tool_content[:keep] + 
                                f"\n\n... [TRUNCATED {len(tool_content) - ANTHROPIC_TOOL_RESULT_MAX_CHARS} CHARS] ...\n\n" + 
                                tool_content[-keep:]
                            )
                        
                        prefix = "[Tool Error]" if is_error else "[Tool Result]"
                        text_parts.append(f"{prefix}\n{tool_content}")
                    elif item_type == "thinking":
                        thinking_content = item.get("thinking", "")
                        if thinking_content:
                            # 将 thinking block 转换为文本块，保留模型推理过程
                            # 使用特定标记包裹，以便模型识别这是它之前的思考
                            text_parts.append(f"<thinking>\n{thinking_content}\n</thinking>")
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

    return openai_body


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
    """尝试多种方式解析 JSON 字符串

    Args:
        json_str: JSON 字符串
        end_pos: 成功时返回的结束位置

    Returns:
        (parsed_json, end_position) 或抛出异常
    """
    import re

    # 直接解析
    try:
        return json.loads(json_str), end_pos
    except json.JSONDecodeError:
        pass

    # 修复策略 1: 移除尾随逗号
    try:
        fixed = re.sub(r',\s*}', '}', json_str)
        fixed = re.sub(r',\s*]', ']', fixed)
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
        fixed = re.sub(r',\s*}', '}', fixed)
        fixed = re.sub(r',\s*]', ']', fixed)
        return json.loads(fixed), end_pos
    except json.JSONDecodeError:
        pass

    # 修复策略 4: 处理截断的字符串值
    # 如果 JSON 在字符串中间被截断，尝试闭合
    try:
        # 检查未闭合的引号
        quote_count = json_str.count('"') - json_str.count('\\"')
        if quote_count % 2 == 1:
            # 奇数个引号，尝试闭合
            fixed = json_str.rstrip()
            if not fixed.endswith('"'):
                fixed = fixed + '"'
            # 检查是否需要闭合对象
            open_braces = fixed.count('{') - fixed.count('}')
            if open_braces > 0:
                fixed = fixed + '}' * open_braces
            return json.loads(fixed), end_pos
    except json.JSONDecodeError:
        pass

    # 修复策略 5: 提取有效的 JSON 子集
    # 尝试找到最长的有效 JSON 前缀
    try:
        # 使用 json.JSONDecoder 来找到有效部分
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
    """
    import re

    # 跳过空白找到 '{' 或 Markdown 代码块标记
    pos = start
    while pos < len(text) and text[pos] in ' \t\n\r':
        pos += 1

    # 检查是否以 ```json 或 ``` 开头
    markdown_match = re.match(r'```(?:json)?\s*', text[pos:])
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
                
                # 如果是 markdown 包装的，还需要跳过结尾标记
                end_pos = pos + 1
                if is_markdown_wrapped:
                    remaining = text[end_pos:]
                    end_match = re.search(r'\s*```', remaining)
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


def parse_inline_tool_blocks(text: str) -> list[dict]:
    """解析内联工具调用，保留文本与工具调用顺序"""
    import re

    blocks = []
    # 匹配 [Calling tool: name]
    tool_pattern = r'\[Calling tool:\s*([^\]]+)\]'
    last_end = 0
    pos = 0

    while pos < len(text):
        match = re.search(tool_pattern, text[pos:])
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
        
        # 查找 Input: 标记
        input_pattern = r'^[\s]*Input:\s*'
        input_match = re.match(input_pattern, after_match)

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
                # 标记包括：下一个工具调用、工具结果、或者文本结尾
                next_marker = re.search(r'\[Calling tool:|\[Tool Result\]|\[Tool Error\]', after_match[input_match.end():])
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


def build_continuation_request(
    original_messages: list,
    truncated_text: str,
    original_body: dict,
    continuation_count: int,
    request_id: str
) -> dict:
    """构建续传请求

    策略：
    1. 确保角色交替 (user -> assistant -> user)
    2. 处理空内容，防止 400 错误
    3. 合并相同角色的消息
    """
    config = CONTINUATION_CONFIG
    
    # 获取截断结尾（用于续传提示）
    ending_chars = config.get("truncated_ending_chars", 500)
    truncated_ending = truncated_text[-ending_chars:] if len(truncated_text) > ending_chars else truncated_text

    # 构建续传提示
    continuation_prompt = config.get("continuation_prompt", "").format(
        truncated_ending=truncated_ending
    )

    # 复制原始消息
    new_messages = []
    for msg in original_messages:
        new_messages.append(dict(msg))

    # 检查最后一条消息的角色
    last_role = new_messages[-1].get("role") if new_messages else None

    # 如果最后一条是 assistant，且我们有截断内容，则合并或添加
    if truncated_text and truncated_text.strip():
        if last_role == "assistant":
            # 如果原始最后一条就是 assistant，直接追加（虽然通常不会发生）
            orig_content = new_messages[-1].get("content", "")
            if isinstance(orig_content, str):
                new_messages[-1]["content"] = orig_content + truncated_text
            else:
                new_messages.append({"role": "assistant", "content": truncated_text})
        else:
            new_messages.append({"role": "assistant", "content": truncated_text})
        
        # 既然添加了 assistant，现在添加 user 续传提示
        new_messages.append({"role": "user", "content": continuation_prompt})
    else:
        # 如果没有截断内容（例如流刚开始就断了）
        if last_role == "user":
            # 如果最后一条是 user，我们不能再加一个 user
            # 修改最后一条 user 消息，追加续传提示
            orig_content = new_messages[-1].get("content", "")
            if isinstance(orig_content, str):
                new_messages[-1]["content"] = orig_content + "\n\n(Note: Previous attempt interrupted. Please start/continue your response.)"
            else:
                # 如果是复杂 content，暂不处理，直接尝试
                new_messages.append({"role": "assistant", "content": " "})
                new_messages.append({"role": "user", "content": continuation_prompt})
        else:
            # 最后一条不是 user (可能是 system 或 assistant)，可以安全添加 user
            new_messages.append({"role": "user", "content": continuation_prompt})

    # 构建新的请求体
    new_body = dict(original_body)
    new_body["messages"] = new_messages
    new_body["max_tokens"] = config.get("continuation_max_tokens", 8192)

    logger.info(f"[{request_id}] 构建续传请求 #{continuation_count + 1}: "
                f"原始消息={len(original_messages)}, 新消息={len(new_messages)}, "
                f"截断文本长度={len(truncated_text)}")

    return new_body


def merge_responses(original_text: str, continuation_text: str, request_id: str) -> str:
    """合并原始响应和续传响应，增强 JSON 边界处理

    策略：
    1. 检测续传响应是否有重复内容
    2. 智能拼接，特别处理 JSON 截断点
    3. 修复可能出现的转义冲突
    """
    if not continuation_text:
        return original_text

    # 清理续传响应开头可能的重复内容或提示
    continuation_clean = continuation_text.lstrip()
    
    # 移除模型可能添加的续传引导词，如 "Continuing from where I left off:"
    intro_patterns = [
        r"^Continuing from.*?:",
        r"^Here is the rest of the response:",
        r"^Continuing the JSON:",
        r"^```json\s*",
        r"^```\s*"
    ]
    import re
    for pattern in intro_patterns:
        match = re.match(pattern, continuation_clean, re.IGNORECASE | re.DOTALL)
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
) -> AsyncIterator[dict]:
    """带接续机制的流式请求获取

    Yields:
        {"type": "text", "text": str}
        {"type": "tool_call_delta", "tool_calls": list}
        {"type": "usage_update", "input_tokens": int, "output_tokens": int}
        {"type": "final_info", "full_text": str, "finish_reason": str, "completed": bool, "usage": dict, "tool_calls": list}
    """
    config = CONTINUATION_CONFIG
    max_continuations = config.get("max_continuations", 10)

    accumulated_text = ""
    continuation_count = 0
    final_finish_reason = "end_turn"
    final_stream_completed = False
    total_input_tokens = 0
    total_output_tokens = 0
    aggregated_tool_calls = []
    
    # 用于跟踪当前工具调用的状态
    tool_call_acc = {}

    current_body = dict(openai_body)
    original_messages = list(openai_body.get("messages", []))

    while continuation_count <= max_continuations:
        # 发起单次流式请求
        current_text = ""
        current_finish_reason = "end_turn"
        current_stream_completed = False
        current_status_code = 200
        
        async for chunk in _fetch_single_stream(current_body, headers, request_id, continuation_count):
            chunk_type = chunk.get("type")
            
            if chunk_type == "text":
                text = chunk.get("text", "")
                current_text += text
                yield {"type": "text", "text": text}
                
            elif chunk_type == "tool_call_delta":
                deltas = chunk.get("tool_calls", [])
                yield {"type": "tool_call_delta", "tool_calls": deltas}
                for tc in deltas:
                    index = tc.get("index")
                    call_id = tc.get("id")
                    key = call_id or f"index_{index}" if index is not None else f"idx_{len(tool_call_acc)}"
                    entry = tool_call_acc.setdefault(key, {"id": call_id, "name": None, "arguments": ""})
                    if call_id: entry["id"] = call_id
                    func = tc.get("function", {}) or {}
                    if func.get("name"): entry["name"] = func.get("name")
                    if func.get("arguments"): entry["arguments"] += func.get("arguments")
                    
            elif chunk_type == "usage":
                it = chunk.get("input_tokens", 0)
                ot = chunk.get("output_tokens", 0)
                total_input_tokens += it
                total_output_tokens += ot
                yield {"type": "usage_update", "input_tokens": total_input_tokens, "output_tokens": total_output_tokens}
                
            elif chunk_type == "finish":
                current_finish_reason = chunk.get("reason", "end_turn")
                current_stream_completed = chunk.get("completed", False)
                current_status_code = chunk.get("status_code", 200)

        # 400 熔断机制：如果是 400 错误（非法请求），直接终止续传，防止死循环
        if current_status_code == 400:
            logger.error(f"[{request_id}] 上游返回 400 错误（非法请求），终止续传")
            # 优雅降级：向用户发送错误提示并正常结束，避免 CLI 报错
            error_msg = "\n\n[System Error: Upstream 400 Bad Request. The conversation history may be corrupted or too complex. Ending generation.]"
            yield {"type": "text", "text": error_msg}
            accumulated_text += error_msg
            final_finish_reason = "end_turn"
            final_stream_completed = True
            break

        # 403 错误（Token 过期/上下文过长）
        if current_status_code == 403:
            logger.error(f"[{request_id}] 上游返回 403 错误（禁止访问），终止续传")
            error_msg = "\n\n[System Error: Upstream 403 Forbidden. The request might be too large or the token is invalid. Ending generation.]"
            yield {"type": "text", "text": error_msg}
            accumulated_text += error_msg
            final_finish_reason = "end_turn"
            final_stream_completed = True
            break

        # 如果单次请求由于其他错误或超时停止，且没有获取任何新内容，也直接终止续传
        if current_finish_reason in ("error", "timeout", "stream_interrupted") and not current_text:
            logger.error(f"[{request_id}] 续传请求 #{continuation_count} 遇到错误且无输出，终止续传: {current_finish_reason}")
            
            error_desc = {
                "timeout": "Upstream Timeout",
                "stream_interrupted": "Connection Interrupted",
                "error": f"Upstream Error ({current_status_code})"
            }.get(current_finish_reason, "Unknown Error")
            
            error_msg = f"\n\n[System Error: {error_desc}. Ending generation.]"
            yield {"type": "text", "text": error_msg}
            accumulated_text += error_msg
            
            final_finish_reason = "end_turn"
            final_stream_completed = True
            break

        # 合并当前请求的文本到总文本
        if continuation_count == 0:
            accumulated_text = current_text
        else:
            # 合并逻辑（处理重叠和引导语）
            accumulated_text = merge_responses(accumulated_text, current_text, request_id)

        # 检测是否需要续传
        truncation_info = detect_truncation(accumulated_text, current_stream_completed, current_finish_reason, request_id)

        # 检查是否由于网络中断且没有获取任何新内容，这种情况继续续传会导致 400 错误
        if truncation_info.reason == "stream_interrupted" and not current_text:
             logger.warning(f"[{request_id}] 流中断且无新内容，停止续传以防 400 错误")
             error_msg = "\n\n[System Error: Stream Interrupted (No Data). Ending generation.]"
             yield {"type": "text", "text": error_msg}
             accumulated_text += error_msg
             final_finish_reason = "end_turn"
             final_stream_completed = True
             break

        # 检查是否超过服务器端强制上限（策略：主动假死欺骗 CLI）
        max_total = config.get("max_total_output_tokens", 20000)
        max_chars = config.get("max_total_chars", 80000)
        
        if total_output_tokens >= max_total or len(accumulated_text) >= max_chars:
            logger.warning(f"[{request_id}] 达到强制上限，主动停止续传以欺骗 CLI")
            final_finish_reason = "end_turn"
            final_stream_completed = True
            break

        if not truncation_info.is_truncated:
            final_finish_reason = current_finish_reason
            final_stream_completed = True
            break

        # 检查触发条件
        triggers = config.get("triggers", {})
        should_continue = False
        if truncation_info.reason == "stream_interrupted" and triggers.get("stream_interrupted", True):
            should_continue = True
        elif truncation_info.reason == "max_tokens_reached" and triggers.get("max_tokens_reached", True):
            should_continue = True
        elif "incomplete_json" in str(truncation_info.reason) and triggers.get("incomplete_tool_json", True):
            should_continue = True
        elif "tool_parse_error" in str(truncation_info.reason) and triggers.get("parse_error", True):
            should_continue = True

        if not should_continue or continuation_count >= max_continuations:
            final_finish_reason = current_finish_reason
            final_stream_completed = current_stream_completed
            break

        # 构建续传请求
        logger.info(f"[{request_id}] 触发续传 #{continuation_count + 1}: reason={truncation_info.reason}")
        current_body = build_continuation_request(
            original_messages,
            accumulated_text,
            openai_body,
            continuation_count,
            request_id
        )
        continuation_count += 1

    # 最后 yield 完整的聚类信息，供外部解析使用
    yield {
        "type": "final_info",
        "full_text": accumulated_text,
        "finish_reason": final_finish_reason,
        "completed": final_stream_completed,
        "usage": {"input_tokens": total_input_tokens, "output_tokens": total_output_tokens, "continuation_count": continuation_count},
        "tool_calls": list(tool_call_acc.values())
    }


async def _fetch_single_stream(
    openai_body: dict,
    headers: dict,
    request_id: str,
    continuation_count: int
) -> AsyncIterator[dict]:
    """执行单次流式请求，并作为异步生成器产生内容块

    Yields:
        {"type": "text", "text": str}
        {"type": "tool_call_delta", "tool_calls": list}
        {"type": "usage", "input_tokens": int, "output_tokens": int}
        {"type": "finish", "reason": str, "completed": bool, "status_code": int}
    """
    finish_reason = "end_turn"
    stream_completed = False
    input_tokens = 0
    output_tokens = 0
    tool_call_acc = {}
    status_code = 200

    try:
        client = get_http_client()
        async with client.stream(
            "POST",
            KIRO_PROXY_URL,
            json=openai_body,
            headers=headers,
        ) as response:
            status_code = response.status_code
            if response.status_code != 200:
                error_text = await response.aread()
                error_str = error_text.decode()
                logger.error(f"[{request_id}] 续传请求 #{continuation_count} 失败: {response.status_code} - {error_str[:200]}")
                yield {"type": "finish", "reason": "error", "completed": False, "status_code": status_code}
                return

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
                                it = usage.get("prompt_tokens", 0)
                                ot = usage.get("completion_tokens", 0)
                                if it or ot:
                                    input_tokens = it or input_tokens
                                    output_tokens = ot or output_tokens
                                    yield {"type": "usage", "input_tokens": it, "output_tokens": ot}

                            choice = data.get("choices", [{}])[0]
                            delta = choice.get("delta", {})
                            fr = choice.get("finish_reason")

                            if fr:
                                stream_completed = True
                                if fr == "tool_calls":
                                    finish_reason = "tool_use"
                                elif fr == "length":
                                    finish_reason = "max_tokens"
                                elif fr == "stop":
                                    finish_reason = "end_turn"

                            content = delta.get("content", "")
                            if content:
                                yield {"type": "text", "text": content}

                            delta_tool_calls = delta.get("tool_calls", []) or []
                            if delta_tool_calls:
                                yield {"type": "tool_call_delta", "tool_calls": delta_tool_calls}
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
                finish_reason = "stream_interrupted"

    except httpx.TimeoutException:
        logger.error(f"[{request_id}] 续传请求 #{continuation_count} 超时")
        yield {"type": "finish", "reason": "timeout", "completed": False, "status_code": 408}
        return
    except Exception as e:
        logger.error(f"[{request_id}] 续传请求 #{continuation_count} 异常: {type(e).__name__}: {e}")
        yield {"type": "finish", "reason": "error", "completed": False, "status_code": 500}
        return

    yield {"type": "finish", "reason": finish_reason, "completed": stream_completed, "status_code": status_code}


def convert_openai_to_anthropic(openai_response: dict, model: str, request_id: str) -> dict:
    """将 OpenAI 响应转换为 Anthropic 格式

    关键增强：检测并转换内联的工具调用为标准 tool_use content blocks
    """
    choice = openai_response.get("choices", [{}])[0]
    message = choice.get("message", {})
    content = message.get("content", "")
    finish_reason = choice.get("finish_reason", "stop")

    # 构建 content blocks
    content_blocks = []
    stop_reason = "end_turn"

    if content:
        # 检测并解析内联的工具调用（保序）
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

    # 如果 OpenAI 返回了 tool_calls（tokens 网关可能在某些情况下返回）
    tool_calls = message.get("tool_calls", [])
    if tool_calls:
        content_blocks.extend(tool_calls_to_blocks(tool_calls))
        stop_reason = "tool_use"

    # 如果没有任何内容，添加空文本
    if not content_blocks:
        content_blocks = [{"type": "text", "text": ""}]

    # 根据 finish_reason 调整 stop_reason
    if finish_reason == "tool_calls":
        stop_reason = "tool_use"
    elif finish_reason == "length":
        stop_reason = "max_tokens"
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
        }
    }


def convert_anthropic_to_openai_simple(anthropic_body: dict) -> dict:
    """最简单的 Anthropic -> OpenAI 转换，带截断保护"""

    # 截断配置
    MAX_MESSAGES = 20          # 最大消息数（不含 system）
    MAX_TOTAL_CHARS = 80000    # 最大总字符数
    MAX_SINGLE_CONTENT = 30000 # 单条消息最大字符数

    messages = []

    # 处理 system 消息
    system = anthropic_body.get("system", "")
    if system:
        if isinstance(system, str):
            system_content = system
        elif isinstance(system, list):
            parts = []
            for item in system:
                if isinstance(item, dict) and "text" in item:
                    parts.append(item["text"])
                elif isinstance(item, str):
                    parts.append(item)
            system_content = "\n".join(parts)
        else:
            system_content = str(system)

        if system_content.strip():
            # 截断过长的 system 消息
            if len(system_content) > MAX_SINGLE_CONTENT:
                system_content = system_content[:MAX_SINGLE_CONTENT] + "\n...[truncated]"
            messages.append({"role": "system", "content": system_content})

    # 转换 messages
    raw_messages = anthropic_body.get("messages", [])

    # 如果消息太多，只保留最近的
    if len(raw_messages) > MAX_MESSAGES:
        raw_messages = raw_messages[-MAX_MESSAGES:]

    for msg in raw_messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")

        # 处理 content
        if isinstance(content, list):
            text_parts = []
            for item in content:
                if isinstance(item, dict):
                    if item.get("type") == "text":
                        text_parts.append(item.get("text", ""))
                    elif item.get("type") == "tool_result":
                        result_content = item.get("content", "")
                        if isinstance(result_content, str):
                            text_parts.append(result_content)
                        elif isinstance(result_content, list):
                            for rc in result_content:
                                if isinstance(rc, dict) and rc.get("type") == "text":
                                    text_parts.append(rc.get("text", ""))
                elif isinstance(item, str):
                    text_parts.append(item)
            content = "\n".join(filter(None, text_parts))

        # 确保 content 非空
        if not content or not content.strip():
            content = " "

        # 截断过长的单条消息
        if len(content) > MAX_SINGLE_CONTENT:
            content = content[:MAX_SINGLE_CONTENT] + "\n...[truncated]"

        messages.append({"role": role, "content": content})

    # 确保至少有一条消息
    if not messages:
        messages.append({"role": "user", "content": "Hello"})

    # 检查总字符数，如果超过则进一步截断
    total_chars = sum(len(m.get("content", "")) for m in messages)
    while total_chars > MAX_TOTAL_CHARS and len(messages) > 2:
        # 保留 system（如果有）和最后一条消息，删除最早的非 system 消息
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

    if "max_tokens" in anthropic_body:
        openai_body["max_tokens"] = anthropic_body["max_tokens"]
    if "temperature" in anthropic_body:
        openai_body["temperature"] = anthropic_body["temperature"]

    return openai_body


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
    # 确保有合理的 max_tokens，同时防止单次请求直接超过 CLI 的 32000 限制
    DEFAULT_MAX_TOKENS = 16384  # 16K tokens 作为默认值
    MAX_CLI_SAFE_TOKENS = 28000  # 设置一个比 32000 更有余量的安全值
    
    original_max_tokens = body.get("max_tokens")
    if original_max_tokens is None:
        body["max_tokens"] = DEFAULT_MAX_TOKENS
        logger.info(f"[{request_id}] 设置默认 max_tokens: {DEFAULT_MAX_TOKENS}")
    elif original_max_tokens > MAX_CLI_SAFE_TOKENS:
        # 强制收缩 max_tokens，防止单次生成就溢出
        body["max_tokens"] = MAX_CLI_SAFE_TOKENS
        logger.warning(f"[{request_id}] 强制收缩 max_tokens: {original_max_tokens} -> {MAX_CLI_SAFE_TOKENS}")
    elif original_max_tokens < 1000:
        # 如果设置得太小，可能导致截断
        logger.warning(f"[{request_id}] max_tokens 较小 ({original_max_tokens})，可能导致响应截断")

    # 记录 max_tokens 以便调试
    final_max_tokens = body.get("max_tokens")

    # ==================== 智能模型路由 ====================
    # 对 Opus 请求进行智能降级判断
    routed_model, route_reason = model_router.route(body)

    if routed_model != original_model:
        logger.info(f"[{request_id}] 🔀 模型路由: {original_model} -> {routed_model} ({route_reason})")
        # 更新请求中的模型
        body["model"] = routed_model
        model = routed_model
    else:
        model = original_model
        if "opus" in original_model.lower():
            logger.info(f"[{request_id}] ✅ 保留 Opus: {route_reason}")

    # 使用完整转换（包含截断和空消息过滤）
    openai_body = convert_anthropic_to_openai(body)

    final_msg_count = len(openai_body.get("messages", []))
    total_chars = sum(len(str(m.get("content", ""))) for m in openai_body.get("messages", []))

    logger.info(f"[{request_id}] Anthropic -> OpenAI: model={model}, stream={stream}, "
                f"msgs={orig_msg_count}->{final_msg_count}, chars={total_chars}, max_tokens={final_max_tokens}")

    # 保存调试文件（仅保留最近几个）
    debug_dir = "/tmp/ai-history-debug"
    import os
    os.makedirs(debug_dir, exist_ok=True)
    try:
        with open(f"{debug_dir}/{request_id}_converted.json", "w") as f:
            json.dump(openai_body, f, indent=2, ensure_ascii=False)
        # 清理旧文件（保留最近 10 个）
        debug_files = sorted(
            [f for f in os.listdir(debug_dir) if f.endswith('.json')],
            key=lambda x: os.path.getmtime(os.path.join(debug_dir, x)),
            reverse=True
        )
        for old_file in debug_files[10:]:
            os.remove(os.path.join(debug_dir, old_file))
    except:
        pass

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
        return await handle_anthropic_stream_via_openai(openai_body, headers, request_id, model)
    else:
        return await handle_anthropic_non_stream_via_openai(openai_body, headers, request_id, model)


async def handle_anthropic_stream_via_openai(
    openai_body: dict,
    headers: dict,
    request_id: str,
    model: str,
) -> StreamingResponse:
    """处理 Anthropic 流式请求 - 通过 OpenAI 格式

    关键增强：
    1. 检测内联工具调用并转换为标准 tool_use content blocks
    2. 智能接续机制 - 当检测到截断时自动发起续传请求
    3. 高并发优化 - 使用全局 HTTP 客户端连接池
    4. Token 计数 - 支持从 OpenAI API 获取或估算 token 数量

    策略：累积完整响应后解析，检测截断并自动续传，然后正确发送 content blocks
    """

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

    async def generate() -> AsyncIterator[bytes]:
        try:
            # 发送 Anthropic 流式头
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
                    "usage": {"input_tokens": estimated_input_tokens, "output_tokens": 0}
                }
            }
            yield f"data: {json.dumps(msg_start)}\n\n".encode()

            # ========== 智能接续机制 (流式转发) ==========
            full_text = ""
            finish_reason = "end_turn"
            stream_completed = True
            tool_calls = []
            output_tokens = 0
            
            block_index = 0
            text_block_started = False
            
            async for chunk in fetch_with_continuation(openai_body, headers, request_id, model):
                chunk_type = chunk.get("type")
                
                if chunk_type == "text":
                    text = chunk.get("text", "")
                    if text:
                        if not text_block_started:
                            yield f'data: {{"type":"content_block_start","index":{block_index},"content_block":{{"type":"text","text":""}}}}\n\n'.encode()
                            text_block_started = True
                        
                        # 流式发送文本 delta
                        delta_event = {
                            "type": "content_block_delta",
                            "index": block_index,
                            "delta": {"type": "text_delta", "text": text}
                        }
                        yield f"data: {json.dumps(delta_event)}\n\n".encode()
                
                elif chunk_type == "tool_call_delta":
                    # 注意：流式转发工具调用需要更复杂的索引管理，这里暂时透传
                    # 实际内联工具调用目前主要在 final_info 中处理
                    pass
                    
                elif chunk_type == "usage_update":
                    output_tokens = chunk.get("output_tokens", output_tokens)
                    
                elif chunk_type == "final_info":
                    full_text = chunk.get("full_text", "")
                    finish_reason = chunk.get("finish_reason", "end_turn")
                    stream_completed = chunk.get("completed", True)
                    tool_calls = chunk.get("tool_calls", [])
                    usage_info = chunk.get("usage", {})
                    output_tokens = usage_info.get("output_tokens", output_tokens)

            if text_block_started:
                yield f'data: {{"type":"content_block_stop","index":{block_index}}}\n\n'.encode()
                block_index += 1

            # 处理最终的工具调用（如果 fetch_with_continuation 发现了完整工具调用）
            for tc in tool_calls:
                # 发送 tool_use block
                yield f'data: {{"type":"content_block_start","index":{block_index},"content_block":{{"type":"tool_use","id":"{tc["id"]}","name":"{tc["name"]}","input":{{}}}}}}\n\n'.encode()
                
                tool_json = json.dumps(tc.get("input", tc.get("arguments", {})))
                yield f'data: {{"type":"content_block_delta","index":{block_index},"delta":{{"type":"input_json_delta","partial_json":{json.dumps(tool_json)}}}}}\n\n'.encode()
                yield f'data: {{"type":"content_block_stop","index":{block_index}}}\n\n'.encode()
                block_index += 1

            # 最终 usage 信息
            safe_output_tokens = min(output_tokens, 31000)
            
            # 确保 stop_reason 是合法的 Anthropic 值
            valid_reasons = ["end_turn", "max_tokens", "stop_sequence", "tool_use"]
            if finish_reason not in valid_reasons:
                # 如果是其他值（如 error, timeout），回退到 end_turn
                finish_reason = "end_turn"
                
            yield f'data: {{"type":"message_delta","delta":{{"stop_reason":"{finish_reason}","stop_sequence":null}},"usage":{{"output_tokens":{safe_output_tokens}}}}}\n\n'.encode()
            yield f'data: {{"type":"message_stop"}}\n\n'.encode()

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
) -> JSONResponse:
    """处理 Anthropic 非流式请求 - 通过 OpenAI 格式

    高并发优化：使用全局 HTTP 客户端连接池
    """
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

    # 创建历史管理器
    session_id = generate_session_id(messages)
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
    }


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
