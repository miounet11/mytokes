import re
"""业务服务模块"""

from .converter import (
    convert_anthropic_to_openai,
    convert_openai_to_anthropic,
    parse_inline_tool_blocks,
    expand_thinking_blocks,
    tool_calls_to_blocks,
)

from app.core.router import (
    model_router,
    ModelRouter,
)

from .managers import (
    async_context_manager,
    async_summary_manager,
)

from .context import (
    generate_session_id,
    enhance_user_message,
    extract_user_content,
    extract_project_context,
)

from .streaming import (
    handle_anthropic_stream_via_openai,
    handle_anthropic_non_stream_via_openai,
    set_http_client_getter,
)

__all__ = [
    "convert_anthropic_to_openai",
    "convert_openai_to_anthropic",
    "parse_inline_tool_blocks",
    "expand_thinking_blocks",
    "tool_calls_to_blocks",
    "model_router",
    "ModelRouter",
    "async_context_manager",
    "async_summary_manager",
    "generate_session_id",
    "enhance_user_message",
    "extract_user_content",
    "extract_project_context",
    "handle_anthropic_stream_via_openai",
    "handle_anthropic_non_stream_via_openai",
    "set_http_client_getter",
]
