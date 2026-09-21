"""OneBot boundary: explicit identities, bounded reads, no implicit write retries."""

from __future__ import annotations

import asyncio
import random
import secrets
import time
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from .config import CleanerError, Deferred, Policy
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


class Adapter:
    def __init__(self, platform_id, bot, *, sleep=asyncio.sleep, clock=time.time, store=None):
        self.platform_id = platform_id
        self.bot = bot
        self.account = ""
        self.sleep = sleep
        self.clock = clock
        self.lock = asyncio.Lock()
        self.reads = deque()
        self.next_read = 0.0
        self.store = store
        self.session = secrets.token_hex(8)
        self.generation = 0
        self.recovery_until = 0
        self._online = None
        self.invalid_identity = False
        self._connection = self._connection_token()
        self._connection_objects = self._client_objects()

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
            self.recovery_until = max(self.recovery_until, self.clock() + random.uniform(300, 900))
        if current is not None and len(current) > 1:
            self.invalid_identity = True
            raise PlatformError("同一接入连接了多个 QQ，请为每个 QQ 使用独立接入。")
        if current == () and getattr(getattr(self.bot, "_api", None), "_http_api", None) is None:
            raise PlatformError("NapCat 连接已断开，等待重新连接后再检查。")
        if current and self.account and current[0][0] != self.account:
            self.invalid_identity = True
            raise PlatformError("接入的 QQ 身份已改变，请重载插件重新绑定。")
        return f"{self.session}:{self.generation}"

    async def call(self, action, *, before_send=None, **params):
        async with self.lock:
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
            self.connection_stamp()
            clients = self._connection_token()
            if clients:
                params["self_id"] = clients[0][
                    0
                ]  # Bind aiocqhttp routing explicitly, including private commands.
            if before_send is not None:
                before_send()
            try:
                result = await asyncio.wait_for(self.bot.call_action(action=action, **params), timeout=25)
            except Exception as exc:
                if type(exc).__name__ in {
                    "NetworkError",
                    "ApiNotAvailable",
                    "TimeoutError",
                    "ConnectionError",
                    "ConnectionResetError",
                }:
                    self.generation += 1
                    self.recovery_until = max(self.recovery_until, self.clock() + random.uniform(300, 900))
                # Do not expose exception text: transport errors can contain authentication URLs.
                raise PlatformError(
                    f"{action} 接口未正常完成；已停止本次任务，请检查 NapCat 在线状态和权限。"
                ) from exc
            if isinstance(result, dict) and ("retcode" in result or "status" in result):
                if result.get("status") != "ok" or result.get("retcode", 0) != 0:
                    raise PlatformError(f"{action} 接口返回失败；请检查 NapCat 状态和权限。")
                result = result.get("data")
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
        if online and self._online is False:
            self.generation += 1
            self.recovery_until = max(self.recovery_until, self.clock() + random.uniform(300, 900))
        self._online = online
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
        return info, second

    async def kick(self, gid, uid, *, before_send=None):
        return await self.call(
            "set_group_kick",
            group_id=gid,
            user_id=uid,
            reject_add_request=False,
            before_send=before_send,
        )


class Router:
    def __init__(self, context, store=None):
        self.context = context
        self.adapters = {}
        self.lock = asyncio.Lock()
        self.store = store

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
                    adapter = self.adapters[pid] = Adapter(pid, platform.bot, store=self.store)
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
            if policy.bot_qq:
                found = [a for a in found if a.account == policy.bot_qq]
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
