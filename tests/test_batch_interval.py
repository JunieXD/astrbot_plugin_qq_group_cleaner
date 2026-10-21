import asyncio
import time
from dataclasses import replace

import pytest
from conftest import ACCOUNT, GROUP

from qq_group_cleaner.config import CleanerError
from qq_group_cleaner.executor import Executor
from qq_group_cleaner.rules import Member
from qq_group_cleaner.store import Store

KEY = f"batch:{ACCOUNT}:{GROUP}"
OWNER = f"batch-owner:{ACCOUNT}:{GROUP}"


async def plan(env):
    return await env.service.build_plan(env.policy, env.adapter)


async def reserve(env, current, index=0):
    await env.store.call("plan_state", current["id"], "running")
    candidate = Member(**current["members"][index]["member"])
    return await env.store.call("reserve", current["id"], candidate, env.clock(), 100, 100, 3600)


async def test_reload_during_the_first_random_wait_does_not_reserve_a_batch(env):
    current = await plan(env)

    async def cancel(seconds):
        assert await env.store.call("get", KEY, 0) == 0
        raise asyncio.CancelledError()

    env.service.sleep = cancel
    with pytest.raises(asyncio.CancelledError):
        await Executor(env.service).execute(current, env.adapter)
    assert not env.adapter.kicks and not await env.store.call("history", ACCOUNT, GROUP)
    assert await env.store.call("get", KEY, 0) == 0


async def test_batch_interval_starts_at_first_submission_and_is_not_extended_per_member(env):
    current = await plan(env)
    start = env.clock()
    await Executor(env.service).execute(current, env.adapter)
    assert len(env.adapter.kicks) == 3
    assert await env.store.call("get", KEY) == start + 30 + 3600
    assert (await env.store.call("get", OWNER))["plan"] == current["id"]


async def test_cancellation_after_a_submission_preserves_the_batch_interval(env):
    current = await plan(env)

    async def cancel(seconds):
        if seconds == 10:
            raise asyncio.CancelledError()
        env.clock.now += seconds

    env.service.sleep = cancel
    with pytest.raises(asyncio.CancelledError):
        await Executor(env.service).execute(current, env.adapter)
    assert len(env.adapter.kicks) == 1
    assert await env.store.call("get", KEY) > env.clock()
    await env.store.call("repair_legacy_empty_batches", env.clock())
    assert await env.store.call("get", KEY) > env.clock()


async def test_proven_unsent_first_intent_releases_only_its_own_interval(env):
    current = await plan(env)
    previous = env.clock() - 100
    await env.store.call("set", KEY, previous)

    async def fail(uid):
        raise CleanerError("发送前取消")

    env.adapter.kick_hook = fail
    with pytest.raises(CleanerError):
        await Executor(env.service).execute(current, env.adapter)
    assert await env.store.call("get", KEY) == previous
    assert await env.store.call("get", OWNER, None) is None
    assert await env.store.call("quota", ACCOUNT, GROUP, env.clock()) == (0, 0)


async def test_unsent_later_intent_does_not_release_the_first_submitted_members_interval(env):
    current = await plan(env)

    async def fail_second(uid):
        if env.adapter.kicks:
            raise CleanerError("第二人发送前取消")

    env.adapter.kick_hook = fail_second
    with pytest.raises(CleanerError):
        await Executor(env.service).execute(current, env.adapter)
    assert len(env.adapter.kicks) == 1
    assert await env.store.call("get", KEY) > env.clock()
    assert await env.store.call("quota", ACCOUNT, GROUP, env.clock()) == (1, 1)


async def test_cancelling_an_intent_cannot_shorten_a_newer_or_foreign_reservation(env):
    current = await plan(env)
    operation = await reserve(env, current)
    newer = env.clock() + 7200
    await env.store.call("set", KEY, newer)
    await env.store.call("cancel_intent", operation, env.clock())
    assert await env.store.call("get", KEY) == newer


async def test_all_skipped_batch_does_not_cause_a_one_second_rescreen_loop(env):
    async def changed(seconds):
        for uid, person in list(env.adapter.people.items()):
            env.adapter.people[uid] = replace(person, last_sent=int(env.clock()))

    env.clock.hook = changed
    await env.service.check_group(env.policy)
    assert not env.adapter.kicks
    assert await env.store.call("get", KEY, 0) == 0
    assert 1800 <= env.service.next_check[GROUP] - env.clock() <= 2100


async def test_legacy_empty_reservation_is_repaired_with_an_audit_record(env):
    await env.store.call("set", KEY, env.clock() + 3600)
    await env.store.call("set", "startup_until", env.clock() + 20)
    await env.store.call("set", "cooldown:" + ACCOUNT, env.clock() + 30)
    await env.store.call("set", "write-at:" + ACCOUNT, env.clock() + 40)
    result = await env.store.call("repair_legacy_empty_batches", env.clock())
    assert len(result) == 1 and result[0]["gid"] == GROUP
    assert await env.store.call("get", KEY) == 0
    assert await env.store.call("get", "startup_until") == env.clock() + 20
    assert await env.store.call("get", "cooldown:" + ACCOUNT) == env.clock() + 30
    assert await env.store.call("get", "write-at:" + ACCOUNT) == env.clock() + 40
    assert any(r["event"] == "释放旧版空批次间隔" for r in env.journal.records)
    assert await env.store.call("repair_legacy_empty_batches", env.clock()) == []


@pytest.mark.parametrize("state", ["submitted", "unknown", "observed_absent", "reviewed_retained"])
async def test_legacy_repair_never_releases_a_recent_operation_even_after_lowering_the_limit(env, state):
    current = await plan(env)
    await env.store.call("plan_state", current["id"], "running")
    candidate = Member(**current["members"][0]["member"])
    # Legacy operations had no owner metadata. Count all retained outcome states.
    op = await env.store.call("reserve", current["id"], candidate, env.clock() - 3600, 100, 100)
    await env.store.call("result", op, state, "test", env.clock())
    until = env.clock() + 3600
    await env.store.call("set", KEY, until)
    assert await env.store.call("repair_legacy_empty_batches", env.clock()) == []
    assert await env.store.call("get", KEY) == until


async def test_legacy_repair_is_per_group(env):
    current = await plan(env)
    op = await reserve(env, current)
    await env.store.call("result", op, "observed_absent", "test", env.clock())
    other = f"batch:{ACCOUNT}:100003"
    await env.store.call("set", other, env.clock() + 3600)
    await env.store.call("repair_legacy_empty_batches", env.clock())
    assert await env.store.call("get", other) == 0
    assert await env.store.call("get", KEY) > env.clock()


async def test_startup_automatically_releases_a_provably_unused_legacy_batch(env):
    await env.store.call("set", KEY, time.time() + 3600)
    await env.store.close()
    recovered = Store(env.path / "state.sqlite3")
    await recovered.call("open_db")
    try:
        assert await recovered.call("get", KEY) == 0
    finally:
        await recovered.close()
