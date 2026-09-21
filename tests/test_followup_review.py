import asyncio
import sqlite3
import threading
from dataclasses import asdict, replace

import pytest
from conftest import ACCOUNT, ADMIN, GROUP
from test_platform import Bot

from qq_group_cleaner.commands import Commands
from qq_group_cleaner.config import CleanerError
from qq_group_cleaner.executor import Executor
from qq_group_cleaner.platform import Adapter
from qq_group_cleaner.rules import Member, evaluate
from qq_group_cleaner.store import Store


def notice(env, uid, kind="group_ban", subtype="ban", **extra):
    return {
        "post_type": "notice",
        "notice_type": kind,
        "sub_type": subtype,
        "self_id": ACCOUNT,
        "group_id": GROUP,
        "user_id": uid,
        "operator_id": ADMIN,
        "time": int(env.clock()),
        "duration": 3600,
        **extra,
    }


async def one_member_plan(env):
    env.box.settings = replace(env.box.settings, pace=replace(env.box.settings.pace, batch_size=1))
    return await env.service.build_plan(env.box.settings.groups[0], env.adapter)


async def test_config_changed_during_account_resolution_cannot_authorize_old_bot(env):
    async def resolve(policy):
        env.box.settings = replace(env.box.settings, groups=(replace(policy, bot_qq="999999"),))
        return env.adapter

    env.router.resolve = resolve
    await env.service.check_group(env.policy)
    assert not env.adapter.kicks
    assert not await env.store.call("latest", ACCOUNT, GROUP)


async def test_old_policy_cannot_be_stamped_with_new_configuration(env):
    env.box.settings = replace(env.box.settings, groups=(replace(env.policy, inactive_days=365),))
    with pytest.raises(CleanerError, match="配置"):
        await env.service.build_plan(env.policy, env.adapter)


async def test_config_change_during_private_authorization_does_not_apply_old_binding(env):
    async def change(uid):
        env.box.settings = replace(env.box.settings, groups=(replace(env.policy, bot_qq="999999"),))

    env.adapter.detail_hook = change
    with pytest.raises(CleanerError, match="配置"):
        await Commands(env.service).run(f"保护 {GROUP} 200001", ADMIN, "test-platform", ACCOUNT)
    assert "200001" not in (await env.store.call("exclusions", ACCOUNT, GROUP, env.clock()))[0]


@pytest.mark.parametrize("uid", ["200001", "0"])
async def test_ban_notice_during_delay_overrides_unmuted_cache(env, uid):
    plan = await one_member_plan(env)

    async def ban(seconds):
        env.clock.hook = None
        await env.service.observe(notice(env, uid))

    env.clock.hook = ban
    try:
        await Executor(env.service).execute(plan, env.adapter)
    except CleanerError:
        pass
    assert not env.adapter.kicks


@pytest.mark.parametrize("uid", ["200001", "0"])
async def test_ban_notice_after_intent_cancels_without_using_quota(env, uid):
    plan = await one_member_plan(env)

    async def ban(target):
        await env.service.observe(notice(env, uid))

    env.adapter.kick_hook = ban
    with pytest.raises(CleanerError):
        await Executor(env.service).execute(plan, env.adapter)
    assert not env.adapter.kicks
    assert await env.store.call("quota", ACCOUNT, GROUP, env.clock()) == (0, 0)


async def test_group_unban_ordering_and_member_ban_expiry(env):
    await env.service.observe(notice(env, "0"))
    await env.service.observe(notice(env, "0", subtype="lift_ban", time=int(env.clock()) - 1, duration=0))
    with pytest.raises(CleanerError, match="全员禁言"):
        await env.service.build_plan(env.policy, env.adapter)
    env.clock.now += 1
    await env.service.observe(notice(env, "0", subtype="lift_ban", duration=0))
    assert await env.service.build_plan(env.policy, env.adapter)
    await env.service.observe(notice(env, "200001", duration=600))
    original = env.adapter.people["200001"]
    current = (await env.store.call("merge", ACCOUNT, GROUP, [original], env.clock()))[0]
    assert not evaluate(env.policy, current, env.clock(), ACCOUNT).eligible
    env.clock.now += 601
    current = (await env.store.call("merge", ACCOUNT, GROUP, [original], env.clock()))[0]
    assert evaluate(env.policy, current, env.clock(), ACCOUNT).eligible


async def test_member_and_group_ban_evidence_survives_database_reopen(env):
    await env.service.observe(notice(env, "200001"))
    await env.service.observe(notice(env, "0"))
    await env.store.close()
    recovered = Store(env.path / "state.sqlite3")
    await recovered.call("open_db")
    try:
        current = (
            await recovered.call("merge", ACCOUNT, GROUP, [env.adapter.people["200001"]], env.clock())
        )[0]
        assert not evaluate(env.policy, current, env.clock(), ACCOUNT).eligible
        assert (await recovered.call("get", f"ban-notice:{ACCOUNT}:{GROUP}:0"))["active"]
    finally:
        await recovered.close()


async def test_disabled_ban_protection_still_allows_explicit_policy(env):
    policy = replace(env.policy, protect_muted=False)
    env.box.settings = replace(env.box.settings, groups=(policy,))
    await env.service.observe(notice(env, "0"))
    await env.service.observe(notice(env, "200001"))
    plan = await one_member_plan(env)
    await Executor(env.service).execute(plan, env.adapter)
    assert env.adapter.kicks == ["200001"]


async def test_missing_group_unban_can_be_reconciled_only_by_explicit_resume(env):
    await env.service.observe(notice(env, "0"))
    env.adapter.all_muted = True
    with pytest.raises(CleanerError, match="全员禁言"):
        await env.service.resume(ACCOUNT, GROUP, ADMIN)
    env.adapter.all_muted = False
    await env.service.resume(ACCOUNT, GROUP, ADMIN)
    assert await env.service.build_plan(env.policy, env.adapter)
    assert await env.store.call("get", "cooldown:" + ACCOUNT) > env.clock()


async def test_new_ban_during_explicit_recovery_cannot_be_cleared(env):
    await env.service.observe(notice(env, "0"))

    async def ban(seconds):
        env.clock.hook = None
        await env.service.observe(notice(env, "0"))

    env.clock.hook = ban
    with pytest.raises(CleanerError, match="禁言状态"):
        await env.service.resume(ACCOUNT, GROUP, ADMIN)
    assert (await env.store.call("get", f"ban-notice:{ACCOUNT}:{GROUP}:0"))["active"]


async def test_recovery_does_not_ignore_a_ban_in_the_same_second(env):
    env.clock.now += 0.5
    await env.service.observe(notice(env, "0"))
    await env.service.resume(ACCOUNT, GROUP, ADMIN)
    await env.service.observe(notice(env, "0"))
    with pytest.raises(CleanerError, match="全员禁言"):
        await env.service.build_plan(env.policy, env.adapter)


async def test_event_already_queued_before_transport_task_starts_prevents_send(env):
    plan = await one_member_plan(env)
    bot = Bot({"set_group_kick": None})
    api = Adapter("test-platform", bot, clock=env.clock, sleep=env.clock.sleep)

    async def kick(gid, uid, *, before_send):
        # A received group event is ready to run when wait_for schedules the network coroutine.
        event = asyncio.create_task(env.service.observe(notice(env, uid)))
        try:
            await api.kick(gid, uid, before_send=before_send)
        finally:
            await event

    env.adapter.kick = kick
    with pytest.raises(CleanerError):
        await Executor(env.service).execute(plan, env.adapter)
    assert not bot.calls
    assert await env.store.call("quota", ACCOUNT, GROUP, env.clock()) == (0, 0)


def raw_member(person):
    return {
        **asdict(person),
        "join_time": person.joined,
        "last_sent_time": person.last_sent,
        "level": person.group_level,
        "shut_up_timestamp": person.muted_until,
    }


@pytest.mark.parametrize("change", ["activity", "role", "title", "mute", "level", "join"])
async def test_first_snapshot_protection_is_not_discarded_by_second_cache(env, change):
    old = raw_member(env.adapter.people["200001"])
    fresh = dict(old)
    fresh.update(
        {
            "activity": {"last_sent_time": int(env.clock())},
            "role": {"role": "admin"},
            "title": {"title": "群贡献者"},
            "mute": {"shut_up_timestamp": int(env.clock()) + 3600},
            "level": {"level": 10},
            "join": {"join_time": int(env.clock())},
        }[change]
    )
    lists = iter([[fresh], [old]])
    api = Adapter(
        "test-platform",
        Bot(
            {
                "get_group_member_list": lambda: next(lists),
                "get_group_detail_info": {"member_count": 1, "max_member_count": 500, "group_all_shut": 0},
            }
        ),
        clock=env.clock,
        sleep=env.clock.sleep,
    )
    if change == "join":
        with pytest.raises(CleanerError):
            await api.snapshot(GROUP)
        return
    _, people = await api.snapshot(GROUP)
    merged = (await env.store.call("merge", ACCOUNT, GROUP, people, env.clock()))[0]
    assert not evaluate(replace(env.policy, protect_level=10), merged, env.clock(), ACCOUNT).eligible
    if change == "activity":
        later = (await env.store.call("merge", ACCOUNT, GROUP, [Member.from_api(old)], env.clock()))[0]
        assert not evaluate(env.policy, later, env.clock(), ACCOUNT).eligible


async def test_retaining_member_rolls_back_resolution_if_protection_cannot_commit(env):
    plan = await one_member_plan(env)
    candidate = Member(**plan["members"][0]["member"])
    await env.store.call("plan_state", plan["id"], "running")
    operation = await env.store.call("reserve", plan["id"], candidate, env.clock(), 20, 30)

    def reject_exemption():
        env.store.db.execute("""CREATE TRIGGER reject_exemption BEFORE INSERT ON exemptions
            BEGIN SELECT RAISE(ABORT, 'simulated disk write failure'); END""")

    env.store.reject_exemption = reject_exemption
    await env.store.call("reject_exemption")
    with pytest.raises(CleanerError):
        await Commands(env.service).run(f"保留 {GROUP} {candidate.user_id}", ADMIN, "test-platform", ACCOUNT)
    env.store.healthy = True  # Read-only inspection after the deliberately injected failure.
    assert (await env.store.call("operation", operation))["state"] == "submitted"


async def test_cancelled_database_failure_marks_storage_unhealthy(env):
    entered, release = threading.Event(), threading.Event()

    def fail():
        entered.set()
        release.wait(5)
        raise sqlite3.OperationalError("disk full")

    env.store.injected_failure = fail
    task = asyncio.create_task(env.store.call("injected_failure"))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        assert not env.store.healthy
        assert task.cancelled()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_repeated_cancellation_still_waits_for_database_commit(env):
    entered, release = threading.Event(), threading.Event()

    def commit():
        entered.set()
        release.wait(5)
        env.store.set("completed-after-cancel", True)

    env.store.injected_commit = commit
    task = asyncio.create_task(env.store.call("injected_commit"))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()
    assert await env.store.call("get", "completed-after-cancel") is True
