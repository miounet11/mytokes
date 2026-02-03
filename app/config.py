"""配置管理模块

使用 Pydantic Settings 进行类型安全的配置管理。
所有敏感信息通过环境变量加载。
"""

import os
from typing import Optional
from pydantic import BaseModel, Field
from functools import lru_cache


# ==================== 基础配置 ====================

class ServiceConfig(BaseModel):
    """服务基础配置"""
    port: int = Field(default=8100, description="服务端口")
    host: str = Field(default="0.0.0.0", description="服务地址")
    debug: bool = Field(default=False, description="调试模式")
    workers: int = Field(default=1, description="工作进程数")
    log_level: str = Field(default="INFO", description="日志级别")
    environment: str = Field(default="development", description="运行环境")


class APIConfig(BaseModel):
    """API 配置"""
    kiro_proxy_url: str = Field(default="", description="Kiro 代理 URL")
    kiro_api_key: str = Field(default="", description="Kiro API 密钥")
    anthropic_api_key: str = Field(default="", description="Anthropic API 密钥")
    anthropic_base_url: str = Field(default="https://api.anthropic.com", description="Anthropic API 基础 URL")
    request_timeout: int = Field(default=300, description="请求超时(秒)")
    connect_timeout: int = Field(default=30, description="连接超时(秒)")


class HTTPPoolConfig(BaseModel):
    """HTTP 连接池配置"""
    max_connections: int = Field(default=1000, description="最大连接数")
    max_keepalive: int = Field(default=200, description="最大保活连接数")
    keepalive_expiry: int = Field(default=30, description="保活过期时间(秒)")
    use_http2: bool = Field(default=False, description="是否使用 HTTP/2")


class HistoryConfig(BaseModel):
    """历史消息管理配置"""
    max_messages: int = Field(default=50, description="最大消息数")
    max_chars: int = Field(default=100000, description="最大字符数")
    summary_threshold: int = Field(default=30, description="触发摘要的消息数阈值")
    summary_max_length: int = Field(default=2000, description="摘要最大长度")
    chars_per_token: float = Field(default=3.5, description="每 token 平均字符数")
    max_retries: int = Field(default=2, description="最大重试次数")
    truncate_ratio: float = Field(default=0.5, description="截断比例")
    logging_enabled: bool = Field(default=True, description="是否启用日志")
    logging_level: str = Field(default="INFO", description="日志级别")


class ContinuationConfig(BaseModel):
    """响应续传配置"""
    enabled: bool = Field(default=True, description="是否启用续传")
    max_continuations: int = Field(default=3, description="最大续传次数")
    continuation_max_tokens: int = Field(default=8192, description="续传请求的 max_tokens")
    truncated_ending_chars: int = Field(default=500, description="截断结尾保留字符数")
    continuation_prompt: str = Field(
        default="""你的上一条回复被截断了。请从以下位置继续，不要重复已输出的内容：

```
{truncated_ending}
```

请直接继续输出，不要添加任何解释或前缀。""",
        description="续传提示模板"
    )
    min_text_length: int = Field(default=10, description="最小有效文本长度")
    max_consecutive_failures: int = Field(default=3, description="最大连续失败次数")

    class TriggerConfig(BaseModel):
        """续传触发条件"""
        stream_interrupted: bool = True
        max_tokens_reached: bool = True
        incomplete_tool_json: bool = True
        parse_error: bool = True

    triggers: TriggerConfig = Field(default_factory=TriggerConfig)


class ModelRoutingConfig(BaseModel):
    """模型路由配置"""
    enabled: bool = Field(default=True, description="是否启用模型路由")
    opus_model: str = Field(default="claude-opus-4-20250514", description="Opus 模型名")
    sonnet_model: str = Field(default="claude-sonnet-4-20250514", description="Sonnet 模型名")
    haiku_model: str = Field(default="claude-3-5-haiku-20241022", description="Haiku 模型名")

    # 降级阈值
    downgrade_message_threshold: int = Field(default=5, description="降级消息数阈值")
    downgrade_char_threshold: int = Field(default=10000, description="降级字符数阈值")

    # 强制使用 Opus 的关键词
    opus_keywords: list[str] = Field(
        default_factory=lambda: [
            "architect", "design", "refactor", "optimize", "security",
            "performance", "scale", "complex", "critical", "重构",
            "架构", "设计", "优化", "安全", "性能"
        ],
        description="强制使用 Opus 的关键词"
    )


class ContextEnhancementConfig(BaseModel):
    """上下文增强配置"""
    enabled: bool = Field(default=True, description="是否启用上下文增强")
    extraction_interval: int = Field(default=5, description="提取间隔(消息数)")
    max_context_length: int = Field(default=1000, description="最大上下文长度")
    cache_ttl: int = Field(default=3600, description="缓存过期时间(秒)")
    integrate_with_summary: bool = Field(default=True, description="是否与摘要集成")

    extraction_prompt: str = Field(
        default="""分析以下对话，提取关键项目上下文信息：

{conversation}

请提取：
1. 项目类型和技术栈
2. 当前工作目录和文件结构
3. 正在进行的任务
4. 重要的决策和约束

用简洁的格式输出，不超过 {max_length} 字符。""",
        description="上下文提取提示模板"
    )

    injection_template: str = Field(
        default="""[项目上下文]
{context}
[/项目上下文]

""",
        description="上下文注入模板"
    )


class StreamConfig(BaseModel):
    """流式响应配置"""
    text_chunk_size: int = Field(default=2000, description="文本块大小")
    tool_json_chunk_size: int = Field(default=2000, description="工具 JSON 块大小")
    thinking_chunk_size: int = Field(default=2000, description="思考块大小")


class AnthropicConfig(BaseModel):
    """Anthropic 格式转换配置"""
    max_tool_result_length: int = Field(default=50000, description="工具结果最大长度")
    max_text_content_length: int = Field(default=100000, description="文本内容最大长度")
    max_image_size_mb: int = Field(default=5, description="图片最大大小(MB)")
    default_max_tokens: int = Field(default=16384, description="默认 max_tokens")
    max_allowed_tokens: int = Field(default=64000, description="最大允许 tokens")


class RateLimitConfig(BaseModel):
    """速率限制配置"""
    enabled: bool = Field(default=False, description="是否启用速率限制")
    requests_per_minute: int = Field(default=60, description="每分钟请求数")
    requests_per_hour: int = Field(default=1000, description="每小时请求数")
    requests_per_second: float = Field(default=10.0, description="每秒请求数")
    burst_size: int = Field(default=10, description="突发请求数")


class CORSConfig(BaseModel):
    """CORS 配置"""
    enabled: bool = Field(default=True, description="是否启用 CORS")
    allow_origins: list[str] = Field(default_factory=lambda: ["*"], description="允许的源")
    allow_credentials: bool = Field(default=True, description="允许凭证")
    allow_methods: list[str] = Field(default_factory=lambda: ["*"], description="允许的方法")
    allow_headers: list[str] = Field(default_factory=lambda: ["*"], description="允许的头")


# ==================== 主配置类 ====================

class Settings(BaseModel):
    """应用主配置"""
    service: ServiceConfig = Field(default_factory=ServiceConfig)
    api: APIConfig = Field(default_factory=APIConfig)
    http_pool: HTTPPoolConfig = Field(default_factory=HTTPPoolConfig)
    history: HistoryConfig = Field(default_factory=HistoryConfig)
    continuation: ContinuationConfig = Field(default_factory=ContinuationConfig)
    model_routing: ModelRoutingConfig = Field(default_factory=ModelRoutingConfig)
    context_enhancement: ContextEnhancementConfig = Field(default_factory=ContextEnhancementConfig)
    stream: StreamConfig = Field(default_factory=StreamConfig)
    anthropic: AnthropicConfig = Field(default_factory=AnthropicConfig)
    rate_limit: RateLimitConfig = Field(default_factory=RateLimitConfig)
    cors: CORSConfig = Field(default_factory=CORSConfig)

    # 便捷属性访问
    @property
    def port(self) -> int:
        return self.service.port

    @property
    def host(self) -> str:
        return self.service.host

    @property
    def workers(self) -> int:
        return self.service.workers

    @property
    def log_level(self) -> str:
        return self.service.log_level

    @property
    def environment(self) -> str:
        return self.service.environment

    class Config:
        env_prefix = "AHM_"  # AI History Manager


def load_settings_from_env() -> Settings:
    """从环境变量加载配置"""
    settings = Settings()

    # 服务配置
    settings.service.port = int(os.getenv("SERVICE_PORT", os.getenv("PORT", "8100")))
    settings.service.host = os.getenv("HOST", "0.0.0.0")
    settings.service.debug = os.getenv("DEBUG", "false").lower() == "true"
    settings.service.workers = int(os.getenv("WORKERS", "4"))
    settings.service.log_level = os.getenv("LOG_LEVEL", "INFO")
    settings.service.environment = os.getenv("ENVIRONMENT", "development")

    # API 配置 - 敏感信息必须从环境变量加载
    settings.api.kiro_api_key = os.getenv("KIRO_API_KEY", "")
    settings.api.kiro_proxy_url = os.getenv(
        "KIRO_PROXY_URL",
        "http://127.0.0.1:8000/v1/chat/completions"
    )
    settings.api.anthropic_api_key = os.getenv("ANTHROPIC_API_KEY", "")
    settings.api.anthropic_base_url = os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    settings.api.request_timeout = int(os.getenv("REQUEST_TIMEOUT", "300"))
    settings.api.connect_timeout = int(os.getenv("CONNECT_TIMEOUT", "30"))

    # HTTP 连接池配置
    settings.http_pool.max_connections = int(os.getenv("HTTP_POOL_MAX_CONNECTIONS", "1000"))
    settings.http_pool.max_keepalive = int(os.getenv("HTTP_POOL_MAX_KEEPALIVE", "200"))
    settings.http_pool.keepalive_expiry = int(os.getenv("HTTP_POOL_KEEPALIVE_EXPIRY", "30"))

    # 续传配置
    settings.continuation.enabled = os.getenv("CONTINUATION_ENABLED", "true").lower() == "true"
    settings.continuation.max_continuations = int(os.getenv("CONTINUATION_MAX_CONTINUATIONS", "3"))

    # 上下文增强配置
    settings.context_enhancement.enabled = os.getenv("CONTEXT_ENHANCEMENT_ENABLED", "true").lower() == "true"

    # 模型路由配置
    settings.model_routing.enabled = os.getenv("MODEL_ROUTING_ENABLED", "true").lower() == "true"

    # 速率限制配置
    settings.rate_limit.enabled = os.getenv("RATE_LIMIT_ENABLED", "false").lower() == "true"
    settings.rate_limit.requests_per_minute = int(os.getenv("RATE_LIMIT_RPM", "60"))

    return settings


@lru_cache()
def get_settings() -> Settings:
    """获取全局配置单例"""
    return load_settings_from_env()


def reload_settings() -> Settings:
    """重新加载配置（清除缓存）"""
    get_settings.cache_clear()
    return get_settings()
