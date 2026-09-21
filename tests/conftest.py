import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from qq_group_cleaner.config import Pace, Policy, Settings
from qq_group_cleaner.platform import GroupInfo, PlatformError
from qq_group_cleaner.rules import DAY, Member
from qq_group_cleaner.service import CleanerService
from qq_group_cleaner.store import Store

ACCOUNT = "100001"
GROUP = "100002"
ADMIN = "100003"


class Clock:
    def __init__(self):
        self.now = datetime(2026, 9, 21, 2, tzinfo=timezone.utc).timestamp()
        self.hook = None
        self.waits = []

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds
        if self.hook:
            await self.hook(seconds)
        await asyncio.sleep(0)


def member(uid, now, **changes):
    return replace(
        Member(
            str(uid),
            "member",
            int(now - 400 * DAY),
            int(now - 100 * DAY),
            group_level=3,
            qq_level=10,
            title="",
            muted_until=0,
        ),
        **changes,
    )


class FakeAdapter:
    account = ACCOUNT
    platform_id = "test-platform"

    def __init__(self, clock):
        self.clock = clock
        self.people = {uid: member(uid, clock()) for uid in (ACCOUNT, ADMIN, "200001", "200002", "200003")}
        self.people[ACCOUNT] = replace(self.people[ACCOUNT], role="admin")
        self.people[ADMIN] = replace(self.people[ADMIN], role="owner")
        self.kicks = []
        self.details = []
        self.connected = True
        self.behavior = "remove"
        self.detail_hook = None
        self.kick_hook = None
        self.snapshot_calls = 0
        self.all_muted = False
        self.recovery_until = 0

    async def identity(self):
        return self.account

    async def online(self):
        return self.connected

    async def group(self, gid):
        return GroupInfo(len(self.people), 500, self.all_muted)

    async def snapshot(self, gid):
        self.snapshot_calls += 1
        return await self.group(gid), list(self.people.values())

    async def member(self, gid, uid):
        self.details.append(uid)
        if self.detail_hook:
            await self.detail_hook(uid)
        if uid not in self.people:
            raise PlatformError("成员详情读取失败")
        return self.people[uid]

    async def kick(self, gid, uid, *, before_send=None):
        if self.kick_hook:
            await self.kick_hook(uid)
        before_send()
        self.kicks.append(uid)
        if self.behavior == "timeout":
            raise PlatformError("请求中断")
        if self.behavior in ("remove", "event"):
            self.people.pop(uid)


class FakeRouter:
    def __init__(self, adapter):
        self.adapter = adapter
        self.guard = None
        self.binding = "test-connection"

    async def resolve(self, policy):
        return self.adapter

    def shared_guard(self):
        return self.guard

    def binding_stamp(self, adapter):
        return self.binding

    async def persist_recovery(self):
        pass


class FakeJournal:
    def __init__(self):
        self.records = []
        self.failure = None

    def check(self):
        if self.failure:
            raise self.failure

    def maintain(self):
        self.check()

    def record(self, *args):
        self.records.append(args)


@pytest.fixture
async def env(tmp_path):
    clock = Clock()
    policy = Policy(GROUP, mode="自动清理", trigger=4, target=2)
    box = SimpleNamespace(settings=Settings(True, (policy,), Pace(min_delay=30, max_delay=30)))
    store = Store(tmp_path / "state.sqlite3")
    await store.call("open_db")
    adapter = FakeAdapter(clock)
    journal = FakeJournal()
    router = FakeRouter(adapter)
    service = CleanerService(
        lambda: box.settings, store, router, journal, clock=clock, sleep=clock.sleep, monotonic=clock
    )
    yield SimpleNamespace(
        clock=clock,
        policy=policy,
        box=box,
        store=store,
        adapter=adapter,
        service=service,
        router=router,
        journal=journal,
        path=tmp_path,
    )
    await service.stop()
    if not store.closed:
        await store.close()
