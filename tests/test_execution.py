import asyncio
from dataclasses import replace

import pytest
from conftest import ACCOUNT, ADMIN, GROUP

from qq_group_cleaner.config import CleanerError, Deferred
from qq_group_cleaner.executor import Executor
from qq_group_cleaner.platform import PlatformError


async def plan_for(env):
    return await env.service.build_plan(env.box.settings.group(GROUP), env.adapter)


async def run(env):
    await Executor(env.service).execute(await plan_for(env), env.adapter)


async def test_success_stops_at_target_and_never_rejects_rejoin(env):
    await run(env)
    assert len(env.adapter.people) == env.policy.target
    assert len(env.adapter.kicks) == 3
    assert ACCOUNT not in env.adapter.kicks and ADMIN not in env.adapter.kicks
    rows = await env.store.call("history", ACCOUNT, GROUP)
    assert all(r["state"] == "observed_absent" for r in rows)
    assert await env.store.call("quota", ACCOUNT, GROUP, env.clock()) == (3, 3)
    assert sum(t >= 30 for t in env.clock.waits) == 3


async def test_zero_write_in_preview_or_disabled_mode(env):
    for settings in [
        replace(env.box.settings, enabled=False),
        replace(env.box.settings, groups=(replace(env.policy, mode="仅预览"),)),
    ]:
        env.box.settings = settings
        plan = await plan_for(env)
        assert plan["state"] == "preview"
        with pytest.raises(CleanerError):
            await Executor(env.service).execute(plan, env.adapter)
    assert env.adapter.kicks == []


async def test_manual_preview_in_auto_mode_is_readonly(env):
    plan = await env.service.build_plan(env.policy, env.adapter, manual=True)
    assert plan["state"] == "preview"
    assert env.adapter.kicks == []


async def test_confirmation_and_changed_admin(env):
    env.box.settings = replace(env.box.settings, groups=(replace(env.policy, mode="确认后清理"),))
    policy = env.box.settings.groups[0]
    plan = await plan_for(env)
    assert plan["state"] == "pending"
    with pytest.raises(CleanerError):
        await Executor(env.service).execute(plan, env.adapter)
    await env.service.confirm(plan["id"], policy, env.adapter, ADMIN)
    env.adapter.people[ADMIN] = replace(env.adapter.people[ADMIN], role="member")
    with pytest.raises(CleanerError, match="确认人"):
        await Executor(env.service).execute(plan, env.adapter)
    assert not env.adapter.kicks


async def test_confirmation_role_change_during_last_revalidation(env):
    policy = replace(env.policy, mode="确认后清理")
    env.box.settings = replace(
        env.box.settings, groups=(policy,), pace=replace(env.box.settings.pace, batch_size=1)
    )
    plan = await plan_for(env)
    await env.service.confirm(plan["id"], policy, env.adapter, ADMIN)

    async def hook(uid):
        await env.service.observe(
            {
                "post_type": "notice",
                "notice_type": "group_admin",
                "sub_type": "unset",
                "self_id": ACCOUNT,
                "group_id": GROUP,
                "user_id": ADMIN,
                "time": int(env.clock()),
            }
        )

    env.adapter.kick_hook = hook
    with pytest.raises(CleanerError, match="身份变化"):
        await Executor(env.service).execute(plan, env.adapter)
    assert not env.adapter.kicks


@pytest.mark.parametrize("change", ["message", "promote", "rejoin", "protect", "count"])
async def test_changes_during_random_delay_skip_without_replacements(env, change):
    env.box.settings = replace(env.box.settings, pace=replace(env.box.settings.pace, batch_size=1))
    plan = await plan_for(env)
    uid = plan["members"][0]["member"]["user_id"]

    async def hook(seconds):
        env.clock.hook = None
        if change == "message":
            await env.service.observe(
                {
                    "post_type": "message",
                    "message_type": "group",
                    "group_id": GROUP,
                    "self_id": ACCOUNT,
                    "user_id": uid,
                    "time": int(env.clock()),
                    "message_id": 1,
                }
            )
        elif change == "promote":
            env.adapter.people[uid] = replace(env.adapter.people[uid], role="admin")
        elif change == "rejoin":
            env.adapter.people[uid] = replace(env.adapter.people[uid], joined=int(env.clock()))
        elif change == "protect":
            await env.store.call("protect", ACCOUNT, GROUP, uid, 0, ADMIN, env.clock())
        else:
            env.adapter.people = {uid: env.adapter.people[uid], ACCOUNT: env.adapter.people[ACCOUNT]}

    env.clock.hook = hook
    await Executor(env.service).execute(plan, env.adapter)
    assert env.adapter.kicks == []


async def test_activity_race_after_intent_cancels_unsubmitted_quota(env):
    env.box.settings = replace(env.box.settings, pace=replace(env.box.settings.pace, batch_size=1))

    async def hook(uid):
        await env.service.observe(
            {
                "post_type": "message",
                "message_type": "group",
                "group_id": GROUP,
                "self_id": ACCOUNT,
                "user_id": uid,
                "time": int(env.clock()),
                "message_id": 10,
            }
        )

    env.adapter.kick_hook = hook
    with pytest.raises(CleanerError, match="核验期间"):
        await run(env)
    assert not env.adapter.kicks
    assert await env.store.call("quota", ACCOUNT, GROUP, env.clock()) == (0, 0)


@pytest.mark.parametrize("behavior", ["timeout", "silent"])
async def test_ambiguous_result_pauses_and_does_not_retry(env, behavior):
    env.adapter.behavior = behavior
    with pytest.raises(CleanerError):
        await run(env)
    assert len(env.adapter.kicks) == 1
    rows = await env.store.call("unresolved", ACCOUNT, GROUP)
    assert len(rows) == 1 and rows[0]["state"] == "unknown"
    assert await env.store.call("quota", ACCOUNT, GROUP, env.clock()) == (1, 1)
    with pytest.raises(CleanerError):
        await env.service.resume(ACCOUNT, GROUP, ADMIN)
    with pytest.raises(CleanerError):
        await run(env)
    assert len(env.adapter.kicks) == 1


async def test_matching_kick_event_confirms_own_action(env):
    env.box.settings = replace(env.box.settings, pace=replace(env.box.settings.pace, batch_size=1))

    async def hook(seconds):
        if seconds == 10 and env.adapter.kicks:
            await env.service.observe(
                {
                    "post_type": "notice",
                    "notice_type": "group_decrease",
                    "sub_type": "kick",
                    "self_id": ACCOUNT,
                    "group_id": GROUP,
                    "user_id": env.adapter.kicks[-1],
                    "operator_id": ACCOUNT,
                    "time": int(env.clock() - 9),
                }
            )

    env.clock.hook = hook
    await run(env)
    assert (await env.store.call("history", ACCOUNT, GROUP))[0]["state"] == "confirmed_removed"


async def test_quotas_stop_batch_and_survive_service_recreation(env):
    env.box.settings = replace(env.box.settings, pace=replace(env.box.settings.pace, account_daily_limit=1))
    with pytest.raises(Deferred, match="额度"):
        await run(env)
    assert len(env.adapter.kicks) == 1
    assert await env.store.call("quota", ACCOUNT, "999999", env.clock()) == (1, 0)


@pytest.mark.parametrize("reason", ["pause", "config", "log", "db", "offline", "permission", "expire"])
async def test_fail_closed(env, reason):
    plan = await plan_for(env)
    if reason == "pause":
        await env.service.pause(ACCOUNT, GROUP, ADMIN)
    elif reason == "config":
        env.box.settings = replace(env.box.settings, enabled=False)
    elif reason == "log":
        env.journal.failure = CleanerError("日志不可写")
    elif reason == "db":
        env.store.healthy = False
    elif reason == "offline":
        env.adapter.connected = False
    elif reason == "permission":
        env.adapter.people[ACCOUNT] = replace(env.adapter.people[ACCOUNT], role="member")
    else:
        env.clock.now += 1900
    with pytest.raises(CleanerError):
        await Executor(env.service).execute(plan, env.adapter)
    assert not env.adapter.kicks


async def test_details_failure_does_not_count_as_absence_or_kick(env):
    async def fail(uid):
        raise PlatformError("成员缓存出错")

    env.adapter.detail_hook = fail
    with pytest.raises(CleanerError):
        await run(env)
    assert not env.adapter.kicks
    assert not await env.store.call("history", ACCOUNT, GROUP)


async def test_same_plan_cannot_execute_twice_concurrently(env):
    plan = await plan_for(env)
    results = await asyncio.gather(
        Executor(env.service).execute(plan, env.adapter),
        Executor(env.service).execute(plan, env.adapter),
        return_exceptions=True,
    )
    assert sum(isinstance(r, CleanerError) for r in results) == 1
    assert len(env.adapter.kicks) == len(set(env.adapter.kicks)) == 3


async def test_shared_guard_uses_platform_key_and_revalidates_after_its_wait(env):
    calls = []

    class Guard:
        deferred_error = Deferred

        async def run(self, **kwargs):
            calls.append(kwargs["account"])
            await env.service.pause(ACCOUNT, GROUP, ADMIN)
            return await kwargs["action"]()

    env.router.guard = Guard()
    with pytest.raises(CleanerError):
        await run(env)
    assert calls == [env.adapter.platform_id]
    assert not env.adapter.kicks


async def test_cancellation_after_sent_recovers_unknown_on_restart(env):
    entered = asyncio.Event()

    async def wait_forever(seconds):
        if seconds == 10:
            entered.set()
            await asyncio.Event().wait()
        else:
            env.clock.now += seconds

    env.service.sleep = wait_forever
    task = asyncio.create_task(run(env))
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(await env.store.call("unresolved", ACCOUNT, GROUP)) == 1
    await env.store.close()
    from qq_group_cleaner.store import Store

    recovered = Store(env.path / "state.sqlite3")
    await recovered.call("open_db")
    try:
        assert (await recovered.call("unresolved", ACCOUNT, GROUP))[0]["state"] == "unknown"
        assert await recovered.call("quota", ACCOUNT, GROUP, env.clock()) == (1, 1)
    finally:
        await recovered.close()
