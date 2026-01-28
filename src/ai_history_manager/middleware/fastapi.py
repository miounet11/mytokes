"""FastAPI 中间件

提供低侵入性的历史消息管理中间件，自动处理请求中的消息历史。
"""

import json
import logging
import re
from typing import Any, Callable, Optional, Pattern

from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..config import HistoryConfig, TruncateStrategy, load_config_from_file
from ..manager import HistoryManager
from ..utils import is_content_length_error

logger = logging.getLogger("ai_history_manager.middleware")


class HistoryManagerMiddleware:
    """FastAPI 历史消息管理中间件

    自动处理 API 请求中的消息历史，支持：
    - 预处理：在发送前截断/摘要历史消息
    - 错误处理：检测长度错误并自动重试

    使用示例:
    ```python
    from fastapi import FastAPI
    from ai_history_manager.middleware import HistoryManagerMiddleware
    from ai_history_manager import load_config_from_file

    app = FastAPI()

    # 方式 1: 使用配置文件
    app.add_middleware(
        HistoryManagerMiddleware,
        config_path="config/history.yaml",
        summary_generator=my_summary_function
    )

    # 方式 2: 手动配置
    from ai_history_manager import HistoryConfig, TruncateStrategy

    config = HistoryConfig(
        strategies=[TruncateStrategy.ERROR_RETRY],
        max_messages=30
    )
    app.add_middleware(
        HistoryManagerMiddleware,
        config=config
    )
    ```
    """

    def __init__(
        self,
        app: ASGIApp,
        config_path: Optional[str] = None,
        config: Optional[HistoryConfig] = None,
        summary_generator: Optional[Callable[[str], Any]] = None,
        path_pattern: str = r"/v1/messages",
        session_id_extractor: Optional[Callable[[dict], str]] = None,
        messages_field: str = "messages",
        system_field: str = "system",
    ):
        """初始化中间件

        Args:
            app: ASGI 应用
            config_path: 配置文件路径（与 config 二选一）
            config: 配置对象（与 config_path 二选一）
            summary_generator: 摘要生成函数，签名为 async (prompt: str) -> str
            path_pattern: 需要处理的路径正则表达式
            session_id_extractor: 会话 ID 提取函数，从请求体提取会话 ID
            messages_field: 消息字段名
            system_field: 系统消息字段名
        """
        self.app = app

        # 加载配置
        if config_path:
            self.config = load_config_from_file(config_path)
        elif config:
            self.config = config
        else:
            self.config = HistoryConfig()

        self.summary_generator = summary_generator
        self.path_pattern: Pattern = re.compile(path_pattern)
        self.session_id_extractor = session_id_extractor or self._default_session_id_extractor
        self.messages_field = messages_field
        self.system_field = system_field

    def _default_session_id_extractor(self, body: dict) -> str:
        """默认会话 ID 提取器

        尝试从请求体中提取会话 ID，优先使用 conversation_id 或 session_id。

        Args:
            body: 请求体字典

        Returns:
            会话 ID
        """
        # 尝试常见的会话 ID 字段
        for field in ["conversation_id", "session_id", "thread_id", "chat_id"]:
            if field in body:
                return str(body[field])

        # 基于消息内容生成哈希
        messages = body.get(self.messages_field, [])
        if messages:
            # 使用前几条消息的内容生成哈希
            content_parts = []
            for msg in messages[:3]:
                content = msg.get("content", "")
                if isinstance(content, str):
                    content_parts.append(content[:100])
            if content_parts:
                import hashlib
                return hashlib.md5("".join(content_parts).encode()).hexdigest()[:16]

        return "default"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """ASGI 入口"""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)
        path = request.url.path

        # 检查是否需要处理此路径
        if not self.path_pattern.search(path):
            await self.app(scope, receive, send)
            return

        # 只处理 POST 请求
        if request.method != "POST":
            await self.app(scope, receive, send)
            return

        # 读取请求体
        body_bytes = await request.body()
        try:
            body = json.loads(body_bytes)
        except json.JSONDecodeError:
            # 无法解析 JSON，直接传递
            await self.app(scope, receive, send)
            return

        # 检查是否包含消息字段
        if self.messages_field not in body:
            await self.app(scope, receive, send)
            return

        # 提取会话 ID
        session_id = self.session_id_extractor(body)

        # 创建历史管理器
        manager = HistoryManager(self.config, cache_key=session_id)

        # 预处理消息历史
        messages = body.get(self.messages_field, [])
        system = body.get(self.system_field, "")
        user_content = self._extract_user_content(messages)

        # 转换为历史格式并预处理
        history = self._messages_to_history(messages)

        if manager.should_summarize(history) and self.summary_generator:
            processed_history = await manager.pre_process_async(
                history, user_content, self.summary_generator
            )
        else:
            processed_history = manager.pre_process(history, user_content)

        # 转换回消息格式
        processed_messages = self._history_to_messages(processed_history)
        body[self.messages_field] = processed_messages

        # 添加截断警告
        if manager.was_truncated and self.config.add_warning_header:
            # 可以在这里添加自定义处理逻辑
            logger.info(f"[HistoryManager] {manager.truncate_info}")

        # 创建新的请求体
        new_body = json.dumps(body).encode()

        # 创建修改后的 receive 函数
        async def modified_receive() -> Message:
            return {
                "type": "http.request",
                "body": new_body,
                "more_body": False,
            }

        # 继续处理请求
        await self.app(scope, modified_receive, send)

    def _extract_user_content(self, messages: list[dict]) -> str:
        """提取最后一条用户消息的内容

        Args:
            messages: 消息列表

        Returns:
            用户消息内容
        """
        if not messages:
            return ""

        # 从后往前找用户消息
        for msg in reversed(messages):
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, str):
                    return content
                elif isinstance(content, list):
                    # Anthropic 格式
                    texts = []
                    for item in content:
                        if isinstance(item, dict) and item.get("type") == "text":
                            texts.append(item.get("text", ""))
                    return "\n".join(texts)
        return ""

    def _messages_to_history(self, messages: list[dict]) -> list[dict]:
        """将消息列表转换为历史格式

        保持原格式，中间件主要用于截断/摘要处理，
        格式转换由具体的 API 处理层负责。

        Args:
            messages: 消息列表

        Returns:
            历史消息列表
        """
        # 对于标准 OpenAI/Anthropic 格式，直接返回
        return messages.copy()

    def _history_to_messages(self, history: list[dict]) -> list[dict]:
        """将历史格式转换回消息列表

        Args:
            history: 历史消息列表

        Returns:
            消息列表
        """
        return history


class RequestInterceptor:
    """请求拦截器

    提供更细粒度的请求处理控制，可以用于自定义处理逻辑。

    使用示例:
    ```python
    from ai_history_manager.middleware import RequestInterceptor
    from ai_history_manager import HistoryManager, HistoryConfig

    interceptor = RequestInterceptor(HistoryConfig())

    @app.post("/v1/messages")
    async def handle_messages(request: Request):
        body = await request.json()

        # 预处理
        processed_body = await interceptor.pre_process(body, session_id="xxx")

        # 调用 API
        response = await call_api(processed_body)

        # 如果遇到长度错误
        if is_length_error(response):
            processed_body = await interceptor.handle_error(body, retry_count=1)
            response = await call_api(processed_body)

        return response
    ```
    """

    def __init__(
        self,
        config: Optional[HistoryConfig] = None,
        summary_generator: Optional[Callable[[str], Any]] = None,
        messages_field: str = "messages",
    ):
        """初始化拦截器

        Args:
            config: 配置对象
            summary_generator: 摘要生成函数
            messages_field: 消息字段名
        """
        self.config = config or HistoryConfig()
        self.summary_generator = summary_generator
        self.messages_field = messages_field
        self._managers: dict[str, HistoryManager] = {}

    def get_manager(self, session_id: str) -> HistoryManager:
        """获取或创建会话的历史管理器

        Args:
            session_id: 会话 ID

        Returns:
            HistoryManager 实例
        """
        if session_id not in self._managers:
            self._managers[session_id] = HistoryManager(self.config, cache_key=session_id)
        return self._managers[session_id]

    async def pre_process(
        self,
        body: dict,
        session_id: str,
        user_content: str = "",
    ) -> dict:
        """预处理请求体

        Args:
            body: 请求体字典
            session_id: 会话 ID
            user_content: 用户当前消息

        Returns:
            处理后的请求体
        """
        messages = body.get(self.messages_field, [])
        if not messages:
            return body

        manager = self.get_manager(session_id)

        if manager.should_summarize(messages) and self.summary_generator:
            processed = await manager.pre_process_async(
                messages, user_content, self.summary_generator
            )
        else:
            processed = manager.pre_process(messages, user_content)

        result = body.copy()
        result[self.messages_field] = processed
        return result

    async def handle_length_error(
        self,
        body: dict,
        session_id: str,
        retry_count: int = 0,
    ) -> tuple[dict, bool]:
        """处理长度错误

        Args:
            body: 请求体字典
            session_id: 会话 ID
            retry_count: 当前重试次数

        Returns:
            (处理后的请求体, 是否应该重试)
        """
        messages = body.get(self.messages_field, [])
        if not messages:
            return body, False

        manager = self.get_manager(session_id)

        processed, should_retry = await manager.handle_length_error_async(
            messages, retry_count, self.summary_generator
        )

        result = body.copy()
        result[self.messages_field] = processed
        return result, should_retry

    def clear_session(self, session_id: str) -> bool:
        """清除会话的管理器

        Args:
            session_id: 会话 ID

        Returns:
            是否存在并清除了管理器
        """
        return self._managers.pop(session_id, None) is not None
