"""OneBot boundary: explicit identities, bounded reads, no implicit write retries."""

from __future__ import annotations

import asyncio
import random
import secrets
import time
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace

from .config import CleanerError, Deferred, Pace, Policy
from .rules import Member, number


class PlatformError(CleanerError):
    pass


_READ_PRIORITY = ContextVar("qq_cleaner_read_priority", default=0)


@contextmanager
def read_priority(level):
    token = _READ_PRIORITY.set(level)
    try:
        yield
    finally:
        _READ_PRIORITY.reset(token)


@dataclass(frozen=True)
class GroupInfo:
    count: int
    capacity: int
    all_muted: bool | None = None

    def check_speaking(self, protect_muted):
        if protect_muted and self.all_muted is not False:
            raise PlatformError("群正在全员禁言或全员禁言状态未知，暂缓清理。")


def merge_snapshot_evidence(first: Member, second: Member) -> Member:
    """Preserve protective facts from both responses when NapCat's caches disagree."""
    if first.joined != second.joined:
        raise PlatformError("成员入群身份在两次查询之间变化，本次暂缓，等待名单稳定。")

    def maximum_known(left, right):
        return max(left, right) if left is not None and right is not None else None

    return replace(
        second,
        last_sent=maximum_known(first.last_sent, second.last_sent),
        activity=max(first.last_sent or 0, second.last_sent or 0),
        role=first.role if first.role in ("owner", "admin") else second.role,
        group_level=maximum_known(first.group_level, second.group_level),
        qq_level=maximum_known(first.qq_level, second.qq_level),
        title=(second.title or first.title) if first.title is not None and second.title is not None else None,
        muted_until=maximum_known(first.muted_until, second.muted_until),
        robot=first.robot or second.robot,
    )


class Adapter:
    def __init__(
        self, platform_id, bot, *, sleep=asyncio.sleep, clock=time.time, store=None, pace=Pace, journal=None
    ):
        self.platform_id = platform_id
        self.bot = bot
        self.account = ""
        self.sleep = sleep
        self.clock = clock
        self.lock = asyncio.Lock()
        self.reads = deque()
        self.next_read = 0.0
        self.store = store
        self.pace = pace
        self.journal = journal
        self._recovery_loaded = False
        self._saved_recovery_until = 0
        self.session = secrets.token_hex(8)
        self.generation = 0
        self.recovery_until = 0
        self._online = None
        self.invalid_identity = False
        self._connection = self._connection_token()
        self._connection_objects = self._client_objects()

    def begin_recovery(self):
        pace = self.pace()
        self.recovery_until = max(
            self.recovery_until,
            self.clock() + random.uniform(pace.recovery_min_seconds, pace.recovery_max_seconds),
        )

    async def persist_recovery(self):
        if self.store and self.recovery_until > self._saved_recovery_until:
            self.recovery_until = await self.store.call(
                "extend_deadline", "connection-cooldown:" + self.platform_id, self.recovery_until
            )
            self._saved_recovery_until = self.recovery_until

    def _client_objects(self):
        clients = getattr(self.bot, "_wsr_api_clients", None)
        return tuple(clients.values()) if isinstance(clients, dict) else ()

    def _connection_token(self):
        clients = getattr(self.bot, "_wsr_api_clients", None)
        if not isinstance(clients, dict):
            return None
        return tuple(sorted((str(account), id(ws)) for account, ws in clients.items()))

    def connection_stamp(self):
        current = self._connection_token()
        if current != self._connection:
            self._connection = current
            # Retain the previous objects until comparison so Python cannot recycle their IDs
            # during a disconnect/reconnect that occurs entirely between two inspections.
            self._connection_objects = self._client_objects()
            self.generation += 1
            self.begin_recovery()
        if current is not None and len(current) > 1:
            self.invalid_identity = True
            raise PlatformError("同一接入连接了多个 QQ，请为每个 QQ 使用独立接入。")
        if current == () and getattr(getattr(self.bot, "_api", None), "_http_api", None) is None:
            raise Deferred("NapCat 尚未连接，等待连接就绪后再检查。", self.clock() + 30)
        if current and self.account and current[0][0] != self.account:
            self.invalid_identity = True
            raise PlatformError("接入的 QQ 身份已改变，请重载插件重新绑定。")
        return f"{self.session}:{self.generation}"

    async def call(self, action, *, before_send=None, **params):
        if self.journal is None:
            return await self._serialized_call(action, before_send=before_send, **params)
        with self.journal.span(
            "平台接口",
            request=secrets.token_hex(6),
            api=action,
            platform=self.platform_id,
            account=self.account,
            **{key: params[key] for key in ("group_id", "user_id", "no_cache") if key in params},
        ):
            return await self._serialized_call(action, before_send=before_send, **params)

    async def _serialized_call(self, action, *, before_send=None, **params):
        async with self.lock:
            if self.store and not self._recovery_loaded:
                self.recovery_until = max(
                    self.recovery_until,
                    await self.store.call("get", "connection-cooldown:" + self.platform_id, 0),
                )
                self._online = await self.store.call("get", "connection-online:" + self.platform_id)
                self._recovery_loaded = True
            try:
                return await self._call(action, before_send=before_send, **params)
            finally:
                # Read failures and reconnects must survive an ordinary short reload too.
                await self.persist_recovery()

    async def _call(self, action, *, before_send=None, **params):
        self.connection_stamp()
        if action != "set_group_kick":
            now = self.clock()
            while self.reads and self.reads[0] <= now - 3600:
                self.reads.popleft()
            limit = (120, 150, 180)[_READ_PRIORITY.get()]
            if self.store:
                await self.store.call("reserve_read", self.platform_id, now, limit)
            elif len(self.reads) >= limit:
                raise Deferred("本小时资料读取已达上限，稍后再检查。", self.reads[0] + 3601)
            await self.sleep(max(0, self.next_read - now))
            self.reads.append(self.clock())
            self.next_read = self.clock() + random.uniform(1.5, 3.0)

        async def invoke():
            self.connection_stamp()
            clients = self._connection_token()
            if clients:
                params["self_id"] = clients[0][0]  # Explicit aiocqhttp routing, also for commands.
            # Recheck inside the transport task so notices already queued on the
            # event loop can invalidate the write before transport actually starts.
            if before_send is not None:
                before_send()
            return await self.bot.call_action(action=action, **params)

        try:
            # Explicit scheduling keeps this boundary identical on Python 3.10+;
            # wait_for(coroutine) itself schedules differently across Python versions.
            result = await asyncio.wait_for(asyncio.create_task(invoke()), timeout=25)
        except CleanerError:
            raise
        except Exception as exc:
            if type(exc).__name__ in {
                "NetworkError",
                "ApiNotAvailable",
                "TimeoutError",
                "ConnectionError",
                "ConnectionResetError",
            }:
                self.generation += 1
                self.begin_recovery()
            # Do not expose exception text: transport errors can contain authentication URLs.
            raise PlatformError(
                f"{action} 接口未正常完成；已停止本次任务，请检查 NapCat 在线状态和权限。"
            ) from exc
        if isinstance(result, dict) and ("retcode" in result or "status" in result):
            if self.journal:
                self.journal.record(
                    "接口返回状态",
                    **{
                        key: result.get(key)
                        for key in ("retcode", "status", "message", "wording")
                        if isinstance(result.get(key), (str, int, float, bool, type(None)))
                    },
                )
            if result.get("status") != "ok" or result.get("retcode", 0) != 0:
                raise PlatformError(f"{action} 接口返回失败；请检查 NapCat 状态和权限。")
            result = result.get("data")
        if self.journal:
            summary = {"response_type": type(result).__name__}
            if isinstance(result, list):
                summary["count"] = len(result)
            elif isinstance(result, dict):
                for key in (
                    "user_id",
                    "group_id",
                    "member_count",
                    "max_member_count",
                    "online",
                    "role",
                    "level",
                    "qq_level",
                    "join_time",
                    "last_sent_time",
                    "group_all_shut",
                ):
                    if key in result and isinstance(result[key], (str, int, float, bool, type(None))):
                        summary[key] = result[key]
            self.journal.record("接口数据摘要", response=summary)
        return result

    async def identity(self):
        data = await self.call("get_login_info")
        if not isinstance(data, dict) or not number(data.get("user_id")):
            raise PlatformError("无法确认机器人 QQ 身份。")
        account = str(data["user_id"])
        if self.account and self.account != account:
            self.invalid_identity = True
            raise PlatformError("机器人登录账号发生变化，请重载插件重新绑定。")
        self.account = account
        return account

    async def online(self):
        data = await self.call("get_status")
        online = isinstance(data, dict) and data.get("online") is True and data.get("good", True) is True
        if not online and self._online is not False:
            self.generation += 1
            self.begin_recovery()
        if online and self._online is False:
            self.generation += 1
            self.begin_recovery()
        if self.store and online != self._online:
            await self.store.call("set", "connection-online:" + self.platform_id, online)
        self._online = online
        await self.persist_recovery()
        return online

    async def group(self, gid):
        # NapCat's ordinary group_info is cached. The detail extension is mandatory.
        data = await self.call("get_group_detail_info", group_id=gid)
        if not isinstance(data, dict):
            raise PlatformError("群人数数据格式不正确，请使用支持群详情接口的 NapCat。")
        if str(data.get("group_id", gid)) != gid:
            raise PlatformError("群详情与目标群不一致，暂不清理。")
        count, capacity = number(data.get("member_count")), number(data.get("max_member_count"))
        if not count or not capacity or count > capacity:
            raise PlatformError("群人数或容量数据异常，暂不清理。")
        all_shut = str(data.get("group_all_shut"))
        return GroupInfo(count, capacity, all_shut != "0" if all_shut in ("-1", "0", "1") else None)

    async def members(self, gid):
        data = await self.call("get_group_member_list", group_id=gid, no_cache=True)
        if not isinstance(data, list) or not data or len(data) > 10000:
            raise PlatformError("成员名单为空、过大或格式异常，暂不清理。")
        members = []
        for item in data:
            if not isinstance(item, dict) or not number(item.get("user_id")):
                raise PlatformError("成员名单有无效身份，暂不清理。")
            if str(item.get("group_id", gid)) != gid:
                raise PlatformError("成员名单与目标群不一致。")
            members.append(Member.from_api(item))
        if len({m.user_id for m in members}) != len(members):
            raise PlatformError("成员名单包含重复身份，暂不清理。")
        return members

    async def member(self, gid, uid):
        data = await self.call("get_group_member_info", group_id=gid, user_id=uid, no_cache=True)
        if (
            not isinstance(data, dict)
            or str(data.get("user_id")) != uid
            or str(data.get("group_id", gid)) != gid
        ):
            raise PlatformError("成员详情与查询身份不一致，暂不清理。")
        return Member.from_api(data)

    async def snapshot(self, gid):
        # A second list lets NapCat's asynchronous refresh progress; it is still not a server transaction.
        first = await self.members(gid)
        await self.sleep(2)
        second = await self.members(gid)
        info = await self.group(gid)
        if {m.user_id for m in first} != {m.user_id for m in second} or len(second) != info.count:
            raise PlatformError("群人数或名单正在变化，本次暂缓，下一次重新检查。")
        previous = {m.user_id: m for m in first}
        return info, [merge_snapshot_evidence(previous[m.user_id], m) for m in second]

    async def kick(self, gid, uid, *, before_send=None):
        return await self.call(
            "set_group_kick",
            group_id=gid,
            user_id=uid,
            reject_add_request=False,
            before_send=before_send,
        )


class Router:
    def __init__(self, context, store=None, *, pace=Pace, journal=None, clock=time.time):
        self.context = context
        self.adapters = {}
        self.lock = asyncio.Lock()
        self.store = store
        self.pace = pace
        self.journal = journal
        self.clock = clock

    async def persist_recovery(self):
        for adapter in self.adapters.values():
            await adapter.persist_recovery()

    def platforms(self):
        manager = self.context.platform_manager
        return manager.get_insts()

    def binding_stamp(self, adapter):
        platforms = [
            (str(p.meta().id), id(p.bot))
            for p in self.platforms()
            if p.meta().name == "aiocqhttp" and hasattr(getattr(p, "bot", None), "call_action")
        ]
        if (adapter.platform_id, id(adapter.bot)) not in platforms:
            raise PlatformError("原机器人接入已停用或替换，请重新预览。")
        connections = 0
        for platform in self.platforms():
            if platform.meta().name == "aiocqhttp":
                clients = getattr(getattr(platform, "bot", None), "_wsr_api_clients", {})
                if isinstance(clients, dict) and adapter.account in {str(key) for key in clients}:
                    connections += 1
        if connections > 1:
            raise PlatformError("同一个 QQ 新增了重复连接，暂停原计划，请只保留一个接入。")
        return repr(sorted(platforms)) + adapter.connection_stamp()

    async def resolve(self, policy: Policy) -> Adapter:
        async with self.lock:
            found = []
            for platform in self.platforms():
                meta = platform.meta()
                if meta.name != "aiocqhttp" or not hasattr(getattr(platform, "bot", None), "call_action"):
                    continue
                pid = str(meta.id)
                adapter = self.adapters.get(pid)
                if adapter is None or adapter.bot is not platform.bot:
                    if adapter is not None:
                        adapter.begin_recovery()
                        await adapter.persist_recovery()
                    adapter = self.adapters[pid] = Adapter(
                        pid,
                        platform.bot,
                        store=self.store,
                        pace=self.pace,
                        journal=self.journal,
                        clock=self.clock,
                    )
                try:
                    if (
                        policy.bot_qq
                        and adapter.account
                        and adapter.account != policy.bot_qq
                        and adapter._connection_token() is not None
                    ):
                        adapter.connection_stamp()  # A known different WS account needs no QQ lookup.
                    else:
                        await adapter.identity()
                except (PlatformError, Deferred):
                    # A known, different account being offline must not stop this account's work.
                    # Unknown identities remain a binding ambiguity and fail closed.
                    if adapter.invalid_identity or not (
                        policy.bot_qq and adapter.account and adapter.account != policy.bot_qq
                    ):
                        raise
                found.append(adapter)
            if not found:
                raise Deferred("QQ 接入尚未就绪，等待平台加载或连接恢复。", self.clock() + 30)
            if policy.bot_qq:
                found = [a for a in found if a.account == policy.bot_qq]
                if not found:
                    raise PlatformError("未找到配置的机器人 QQ，请检查机器人 QQ 和对应接入是否已启用。")
            if len({a.account for a in found}) != len(found):
                raise PlatformError("同一 QQ 接入了多个平台，请只保留一个接入后再使用清理。")
            if len(found) != 1:
                raise PlatformError("无法唯一确定机器人；多 QQ 接入时请在这个群的更多条件中填写机器人 QQ。")
            self.binding_stamp(found[0])
            return found[0]

    def shared_guard(self):
        guard = getattr(self.context.platform_manager, "_qq_automation_guard_v1", None)
        if guard is not None and (
            not callable(getattr(guard, "run", None)) or not hasattr(guard, "deferred_error")
        ):
            raise PlatformError("现有 QQ 操作协调器版本不兼容，已暂停清理。")
        return guard
