import re
import asyncio
import time
import logging
from typing import Optional, Tuple
from app.core.config import (
    CONTEXT_ENHANCEMENT_CONFIG, ASYNC_SUMMARY_CONFIG, logger
)
from app.utils.token_utils import estimate_tokens
from app.utils.cache import TTLCache

class AsyncContextManager:
    """异步上下文提取管理器 - 后台提取上下文，不阻塞主请求"""

    def __init__(self):
        # 使用 TTLCache，默认 1000 个会话，1 小时过期
        self._context_cache = TTLCache(maxsize=1000, ttl=3600)
        # 正在进行的任务：session_id -> asyncio.Task
        self._pending_tasks: dict[str, asyncio.Task] = {}
        # 锁
        self._lock = asyncio.Lock()
        # 统计
        self._stats = {
            "cache_hits": 0,
            "cache_misses": 0,
            "async_tasks": 0,
            "tasks_completed": 0,
            "tasks_failed": 0,
        }

    def get_cached_context(self, session_id: str) -> tuple[str, bool]:
        """获取缓存的上下文"""
        content = self._context_cache.get(session_id)
        if content:
            self._stats["cache_hits"] += 1
            return content, True
        self._stats["cache_misses"] += 1
        return "", False

    def should_update_context(self, session_id: str, current_message_count: int) -> bool:
        """判断是否需要更新上下文"""
        # 注意：由于 TTLCache 只存储了内容，我们需要决定是否需要存储 message_count
        # 这里为了简化，如果缓存存在且未过期，我们认为不需要频繁更新
        return not self._context_cache.get(session_id)

    def is_task_pending(self, session_id: str) -> bool:
        """检查是否有正在进行的上下文提取任务"""
        task = self._pending_tasks.get(session_id)
        return task is not None and not task.done()

    async def schedule_context_task(
        self,
        session_id: str,
        messages: list,
        user_message_count: int,
        extract_func
    ):
        """调度后台上下文提取任务 - 不阻塞主请求"""
        if not CONTEXT_ENHANCEMENT_CONFIG.get("enabled", True):
            return

        # 检查是否已有任务在运行
        if self.is_task_pending(session_id):
            logger.debug(f"[{session_id[:8]}] 上下文提取任务已在运行，跳过")
            return

        # 检查队列大小
        async with self._lock:
            # 清理已完成的任务
            done_sessions = [s for s, t in self._pending_tasks.items() if t.done()]
            for s in done_sessions:
                del self._pending_tasks[s]

            # 限制最大并发任务数
            if len(self._pending_tasks) >= 50:
                logger.warning(f"[{session_id[:8]}] 上下文提取队列已满，跳过")
                return

            # 创建后台任务
            task = asyncio.create_task(
                self._extract_context_background(session_id, messages, user_message_count, extract_func)
            )
            self._pending_tasks[session_id] = task
            self._stats["async_tasks"] += 1

        logger.info(f"[{session_id[:8]}] 🚀 启动后台上下文提取任务")

    async def _extract_context_background(
        self,
        session_id: str,
        messages: list,
        user_message_count: int,
        extract_func
    ):
        """后台提取上下文任务"""
        try:
            # 调用提取函数
            context = await extract_func(messages, session_id)

            if context:
                # 更新缓存
                self._context_cache.set(session_id, context)
                self._stats["tasks_completed"] += 1
                logger.info(f"[{session_id[:8]}] ✅ 后台上下文提取完成: {len(context)} chars")
            else:
                self._stats["tasks_failed"] += 1
                logger.warning(f"[{session_id[:8]}] ⚠️ 后台上下文提取返回空")

        except asyncio.CancelledError:
            logger.info(f"[{session_id[:8]}] 上下文提取任务被取消")
        except Exception as e:
            self._stats["tasks_failed"] += 1
            logger.error(f"[{session_id[:8]}] ❌ 后台上下文提取失败: {e}")

    def get_stats(self) -> dict:
        """获取统计信息"""
        return {
            **self._stats,
            "pending_tasks": len([t for t in self._pending_tasks.values() if not t.done()]),
            "cache_size": len(self._context_cache),
        }


class AsyncSummaryManager:
    """异步摘要管理器 - 后台生成摘要，不阻塞主请求"""

    def __init__(self):
        # 使用 TTLCache，默认 1000 个会话，2 小时过期
        self._summary_cache = TTLCache(maxsize=1000, ttl=7200)
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
        """获取缓存的摘要"""
        cache_entry = self._summary_cache.get(session_id)
        if cache_entry and cache_entry.get("summary"):
            self._stats["cache_hits"] += 1
            original_tokens = cache_entry.get("original_tokens", 0)
            return cache_entry["summary"], True, original_tokens
        self._stats["cache_misses"] += 1
        return "", False, 0

    def get_cache_info(self, session_id: str) -> dict:
        """获取缓存信息，用于计费模拟"""
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
        """判断是否需要更新摘要"""
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
        manager,
        user_content: str,
        summary_call_func
    ):
        """调度后台摘要任务"""
        if not ASYNC_SUMMARY_CONFIG.get("enabled", True):
            return

        # 检查是否已有任务ใน运行
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
                self._generate_summary_background(session_id, messages, manager, user_content, summary_call_func)
            )
            self._pending_tasks[session_id] = task
            self._stats["async_tasks"] += 1

        logger.info(f"[{session_id[:8]}] 🚀 启动后台摘要任务")

    async def _generate_summary_background(
        self,
        session_id: str,
        messages: list,
        manager,
        user_content: str,
        summary_call_func
    ):
        """后台生成摘要任务"""
        try:
            timeout = ASYNC_SUMMARY_CONFIG.get("task_timeout", 30)

            # 计算原始消息的 token 数
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
                manager.pre_process_async(messages, user_content, summary_call_func),
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
                self._summary_cache.set(session_id, {
                    "summary": summary,
                    "message_count": len(messages),
                    "timestamp": time.time(),
                    "processed_messages": processed_messages,
                    "original_tokens": original_tokens,
                    "cached_tokens": cached_tokens,
                })
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

# 全局管理器实例
async_context_manager = AsyncContextManager()
async_summary_manager = AsyncSummaryManager()
