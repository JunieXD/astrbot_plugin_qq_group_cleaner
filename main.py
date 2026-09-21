"""AstrBot entry point. Business rules and I/O boundaries live in qq_group_cleaner."""

from __future__ import annotations

import asyncio

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star, StarTools, register

from .qq_group_cleaner.commands import Commands
from .qq_group_cleaner.config import CleanerError, parse_settings
from .qq_group_cleaner.platform import Router
from .qq_group_cleaner.resources import InstanceLock, Journal, exception_detail
from .qq_group_cleaner.service import CleanerService
from .qq_group_cleaner.store import Store


@register("astrbot_plugin_qq_group_cleaner", "JunieXD", "按群容量和不活跃规则清理成员", "0.2.3")
class QQGroupCleaner(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context=context, config=config)
        self.raw_config = config if config is not None else {}
        self.service = None
        self.store = None
        self.journal = None
        self.lock = None
        self.start_error = "插件尚未初始化。"

    def settings(self):
        return parse_settings(dict(self.raw_config))

    async def initialize(self):
        try:
            self.settings()
            root = StarTools.get_data_dir("astrbot_plugin_qq_group_cleaner")
            self.lock = InstanceLock(root / "instance.lock")
            self.journal = Journal(root)
            self.store = Store(root / "state.sqlite3", journal=self.journal)
            await self.store.call("open_db")
            self.service = CleanerService(
                self.settings,
                self.store,
                Router(self.context, self.store, pace=lambda: self.settings().pace, journal=self.journal),
                self.journal,
            )
            await self.service.start()
            self.start_error = ""
            logger.info("QQ 群清理 v0.2.3 已加载；默认只预览，配置中启用后开始检查。")
        except BaseException as exc:
            if self.journal:
                self.journal.record("插件初始化失败", exception=exc)
            self.start_error = (
                str(exc)
                if isinstance(exc, CleanerError)
                else "插件初始化失败，请检查数据目录权限和磁盘空间。"
            )
            logger.error("QQ 群清理：%s [%s]", self.start_error, type(exc).__name__)
            logger.error("QQ 群清理初始化异常详情：%s", exception_detail(exc))
            await self.terminate()
            if not isinstance(exc, Exception):
                raise

    async def terminate(self):
        try:
            await self._terminate()
        except BaseException as exc:
            logger.error("QQ 群清理关闭异常详情：%s", exception_detail(exc))
            raise

    async def _terminate(self):
        try:
            if self.service:
                await self.service.stop()
        finally:
            try:
                if self.store:
                    await self.store.close()
            finally:
                try:
                    if self.journal:
                        self.journal.close()
                finally:
                    try:
                        if self.lock:
                            self.lock.close()
                    finally:
                        self.service = self.store = self.journal = self.lock = None

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.ALL, priority=100)
    async def observe_group(self, event):
        if self.service:
            raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
            if raw is not None:
                await self.service.observe(raw)

    @filter.command("群清理", alias={"qgclean"})
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def cleaner_command(self, event):
        event.stop_event()
        service = self.service
        if not service:
            yield event.plain_result(self.start_error or "插件正在停止，请稍后重试。")
            return
        task = asyncio.current_task()
        service.jobs.add(task)
        try:
            raw = getattr(event.message_obj, "raw_message", None)
            self_id = (
                str(raw.get("self_id", "")) if isinstance(raw, dict) else str(getattr(raw, "self_id", ""))
            )
            result = await Commands(service).run(
                event.get_message_str(), str(event.get_sender_id()), str(event.platform_meta.id), self_id
            )
        except CleanerError as exc:
            result = str(exc)
        except Exception as exc:
            service.journal.record("命令异常", exception=exc)
            result = "操作未完成，请查看插件状态和日志。"
        finally:
            service.jobs.discard(task)
        yield event.plain_result(result)
