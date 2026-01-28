"""Kiro API 适配器

提供与 Kiro API 集成的摘要生成功能。
"""

import json
import logging
from typing import Any, Optional

import httpx

logger = logging.getLogger("ai_history_manager.adapters.kiro")


class KiroSummaryAdapter:
    """Kiro API 摘要生成适配器

    使用 Kiro API 生成对话摘要，支持 event-stream 格式响应解析。

    使用示例:
    ```python
    from ai_history_manager.adapters import KiroSummaryAdapter
    from ai_history_manager import HistoryManager

    # 创建适配器
    adapter = KiroSummaryAdapter(
        api_url="https://kiro.api.endpoint/v1/conversations",
        token="your-token",
        machine_id="machine-id"
    )

    # 使用适配器
    manager = HistoryManager(config)
    processed = await manager.pre_process_async(
        history, user_content, summary_generator=adapter.generate_summary
    )
    ```
    """

    def __init__(
        self,
        api_url: str,
        token: str,
        machine_id: Optional[str] = None,
        profile_arn: Optional[str] = None,
        client_id: Optional[str] = None,
        model: str = "claude-haiku-4.5",
        timeout: int = 60,
        verify_ssl: bool = False,
    ):
        """初始化适配器

        Args:
            api_url: Kiro API 地址
            token: 认证 token
            machine_id: 机器 ID
            profile_arn: AWS Profile ARN
            client_id: 客户端 ID
            model: 摘要使用的模型（推荐使用快速模型）
            timeout: 请求超时时间（秒）
            verify_ssl: 是否验证 SSL 证书
        """
        self.api_url = api_url
        self.token = token
        self.machine_id = machine_id
        self.profile_arn = profile_arn
        self.client_id = client_id
        self.model = model
        self.timeout = timeout
        self.verify_ssl = verify_ssl

    def _build_headers(self) -> dict[str, str]:
        """构建请求头"""
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/vnd.amazon.eventstream",
        }

        if self.machine_id:
            headers["x-amzn-machine-id"] = self.machine_id
        if self.profile_arn:
            headers["x-amzn-profile-arn"] = self.profile_arn
        if self.client_id:
            headers["x-amzn-client-id"] = self.client_id

        return headers

    def _build_request(self, prompt: str) -> dict[str, Any]:
        """构建 Kiro API 请求体"""
        return {
            "conversationState": {
                "conversationId": "",
                "currentMessage": {
                    "userInputMessage": {
                        "content": prompt,
                        "modelId": self.model,
                        "origin": "AI_EDITOR",
                    }
                },
                "history": [],
            }
        }

    def _parse_event_stream(self, content: bytes) -> str:
        """解析 event-stream 格式响应

        Args:
            content: 响应内容（二进制）

        Returns:
            提取的文本内容
        """
        text_parts = []

        try:
            pos = 0
            while pos < len(content):
                if pos + 12 > len(content):
                    break

                total_len = int.from_bytes(content[pos : pos + 4], "big")
                if total_len == 0 or total_len > len(content) - pos:
                    break

                headers_len = int.from_bytes(content[pos + 4 : pos + 8], "big")
                payload_start = pos + 12 + headers_len
                payload_end = pos + total_len - 4

                if payload_start < payload_end:
                    try:
                        payload = json.loads(content[payload_start:payload_end].decode("utf-8"))

                        # 提取内容
                        text_content = None
                        if "assistantResponseEvent" in payload:
                            text_content = payload["assistantResponseEvent"].get("content")
                        elif "content" in payload:
                            text_content = payload["content"]

                        if text_content:
                            text_parts.append(text_content)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        pass

                pos += total_len

        except Exception as e:
            logger.debug(f"解析 event-stream 时出错: {e}")

        return "".join(text_parts)

    async def generate_summary(self, prompt: str) -> str:
        """生成摘要

        Args:
            prompt: 摘要提示词

        Returns:
            摘要文本，失败返回空字符串
        """
        headers = self._build_headers()
        request_body = self._build_request(prompt)

        try:
            async with httpx.AsyncClient(
                verify=self.verify_ssl, timeout=self.timeout
            ) as client:
                response = await client.post(
                    self.api_url, json=request_body, headers=headers
                )

                if response.status_code == 200:
                    return self._parse_event_stream(response.content)
                else:
                    logger.warning(
                        f"摘要 API 调用失败: {response.status_code} - {response.text[:200]}"
                    )

        except httpx.TimeoutException:
            logger.warning("摘要 API 调用超时")
        except Exception as e:
            logger.warning(f"摘要 API 调用异常: {e}")

        return ""

    def update_token(self, token: str) -> None:
        """更新认证 token

        Args:
            token: 新的 token
        """
        self.token = token

    def update_credentials(
        self,
        token: Optional[str] = None,
        machine_id: Optional[str] = None,
        profile_arn: Optional[str] = None,
        client_id: Optional[str] = None,
    ) -> None:
        """更新认证凭证

        Args:
            token: 新的 token
            machine_id: 新的机器 ID
            profile_arn: 新的 Profile ARN
            client_id: 新的客户端 ID
        """
        if token is not None:
            self.token = token
        if machine_id is not None:
            self.machine_id = machine_id
        if profile_arn is not None:
            self.profile_arn = profile_arn
        if client_id is not None:
            self.client_id = client_id


def create_kiro_adapter(
    api_url: str,
    token: str,
    **kwargs,
) -> KiroSummaryAdapter:
    """创建 Kiro 适配器的工厂函数

    Args:
        api_url: Kiro API 地址
        token: 认证 token
        **kwargs: 其他参数

    Returns:
        KiroSummaryAdapter 实例
    """
    return KiroSummaryAdapter(api_url=api_url, token=token, **kwargs)
