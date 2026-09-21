import asyncio
import sqlite3
from dataclasses import replace
from types import SimpleNamespace

import pytest
from conftest import ACCOUNT, ADMIN, GROUP, Clock, member
from test_platform import Bot

from qq_group_cleaner.config import CleanerError, Deferred, Policy
from qq_group_cleaner.executor import Executor
from qq_group_cleaner.platform import Adapter, PlatformError, Router, read_priority
from qq_group_cleaner.rules import DAY, Member, evaluate
from qq_group_cleaner.store import Store


async def test_platform_activity_does_not_rewind_when_cache_falls_back(env):
    old = env.adapter.people["200001"]
    recent = replace(old, last_sent=int(env.clock() - DAY))
    await env.store.call("merge", ACCOUNT, GROUP, [recent], env.clock())
    fallback = (await env.store.call("merge", ACCOUNT, GROUP, [old], env.clock()))[0]
    assert fallback.activity == recent.last_sent
    assert not evaluate(env.policy, fallback, env.clock(), ACCOUNT).eligible


async def test_departed_member_cannot_be_resurrected_by_old_roster(env):
    old = env.adapter.people["200001"]
    before = (await env.store.call("merge", ACCOUNT, GROUP, [old], env.clock()))[0]
    await env.store.call(
        "observe",
        ACCOUNT,
        GROUP,
        old.user_id,
        "group_decrease",
        env.clock(),
        env.clock(),
        "leave",
        ADMIN,
        "kick",
    )
    for _ in range(3):
        stale = (await env.store.call("merge", ACCOUNT, GROUP, [old], env.clock()))[0]
        assert not stale.membership_known and stale.epoch == before.epoch
        assert not evaluate(env.policy, stale, env.clock(), ACCOUNT).eligible
    await env.store.call(
        "observe", ACCOUNT, GROUP, old.user_id, "group_increase", env.clock() + 60, env.clock() + 60, "rejoin"
    )
    rejoined = replace(old, joined=int(env.clock() + 58))  # QQ join time differs slightly from notice time.
    fresh = (await env.store.call("merge", ACCOUNT, GROUP, [rejoined], env.clock() + 60))[0]
    assert fresh.membership_known and fresh.epoch == before.epoch + 1


async def test_join_timestamp_missing_or_rewound_never_creates_another_epoch(env):
    old = env.adapter.people["200001"]
    baseline = (await env.store.call("merge", ACCOUNT, GROUP, [old], env.clock()))[0]
    for stamp in (None, old.joined - 100, old.joined, old.joined + 99999999999, old.joined):
        merged = (await env.store.call("merge", ACCOUNT, GROUP, [replace(old, joined=stamp)], env.clock()))[0]
        assert merged.epoch == baseline.epoch
    newer = replace(old, joined=old.joined + DAY)
    merged = (await env.store.call("merge", ACCOUNT, GROUP, [newer], env.clock()))[0]
    assert merged.epoch == baseline.epoch + 1 and merged.activity == env.clock()
    assert not evaluate(env.policy, merged, env.clock(), ACCOUNT).eligible


async def test_qq_lookup_failure_keeps_progress_and_later_members_can_continue(env):
    policy = replace(env.policy, order="QQ等级低优先")
    env.box.settings = replace(env.box.settings, groups=(policy,))

    async def fail_one(uid):
        if uid == "200002":
            raise PlatformError("这个成员的 UID 查询失败")

    env.adapter.detail_hook = fail_one
    with pytest.raises(PlatformError):
        await env.service.build_plan(policy, env.adapter)
    cached = await env.store.call("get", f"qq-cache:{ACCOUNT}:{GROUP}")
    assert cached["200001"]["level"] == 10 and cached["200002"]["level"] is None
    env.adapter.details.clear()
    env.clock.now += 3601
    plan = await env.service.build_plan(policy, env.adapter)
    assert env.adapter.details == ["200003"]
    assert plan["waiting"] == 0 and plan["eligible"] == 2


async def test_large_group_enrichment_progress_survives_more_than_a_day(env):
    policy = replace(env.policy, order="QQ等级低优先")
    env.box.settings = replace(env.box.settings, groups=(policy,))
    for index in range(25):
        uid = str(300000 + index)
        env.adapter.people[uid] = member(uid, env.clock())
    await env.service.build_plan(policy, env.adapter)
    assert len(env.adapter.details) == 20
    env.clock.now += 25 * 3600
    env.adapter.details.clear()
    plan = await env.service.build_plan(policy, env.adapter)
    assert len(env.adapter.details) == 8 and not plan["waiting"]


async def test_connection_changed_during_delay_invalidates_confirmation(env):
    plan = await env.service.build_plan(env.policy, env.adapter)

    async def reconnect(_seconds):
        env.clock.hook = None
        env.router.binding = "a-new-websocket"
        env.adapter.recovery_until = env.clock() + 600

    env.clock.hook = reconnect
    with pytest.raises(CleanerError, match="连接"):
        await Executor(env.service).execute(plan, env.adapter)
    assert not env.adapter.kicks
    assert await env.store.call("get", "cooldown:" + ACCOUNT) == env.adapter.recovery_until


async def test_reconnect_after_intent_before_network_does_not_submit(env):
    plan = await env.service.build_plan(env.policy, env.adapter)

    async def reconnect(uid):
        env.router.binding = "another-connection"

    env.adapter.kick_hook = reconnect
    with pytest.raises(CleanerError, match="连接"):
        await Executor(env.service).execute(plan, env.adapter)
    assert not env.adapter.kicks
    assert await env.store.call("quota", ACCOUNT, GROUP, env.clock()) == (0, 0)


async def test_slow_api_queue_invalidates_last_checks(env):
    plan = await env.service.build_plan(env.policy, env.adapter)

    async def slow_queue(uid):
        env.clock.now += 31

    env.adapter.kick_hook = slow_queue
    with pytest.raises(CleanerError, match="耗时过长"):
        await Executor(env.service).execute(plan, env.adapter)
    assert not env.adapter.kicks


async def test_pending_result_in_one_group_blocks_account_even_without_pause_flag(env):
    plan = await env.service.build_plan(env.policy, env.adapter)
    await env.store.call("plan_state", plan["id"], "running")
    candidate = Member(**plan["members"][0]["member"])
    await env.store.call("reserve", plan["id"], candidate, env.clock(), 20, 30)
    # A crash can leave an intent without having written a separate account pause flag.
    another = dict(plan, id="other-plan", gid="100099")
    await env.store.call("save_plan", another, "running")
    with pytest.raises(CleanerError, match="结果不明"):
        await env.store.call("reserve", another["id"], candidate, env.clock(), 20, 30)


async def test_a_group_admin_cannot_clear_another_groups_account_pause(env):
    await env.store.call("set", "account-pause:" + ACCOUNT, {"gid": "100099", "reason": "请求异常"})
    with pytest.raises(CleanerError, match="其他群"):
        await env.service.resume(ACCOUNT, GROUP, ADMIN)
    assert await env.store.call("get", "account-pause:" + ACCOUNT)


async def test_demotion_notice_overrides_stale_admin_permissions(env):
    await env.store.call("merge", ACCOUNT, GROUP, [env.adapter.people[ADMIN]], env.clock())
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
    assert env.adapter.people[ADMIN].role == "owner"  # Intentionally stale platform response.
    with pytest.raises(CleanerError, match="管理员"):
        await env.service.authorize(env.policy, ADMIN, env.adapter.platform_id, ACCOUNT)
    original = env.adapter.people[ADMIN]
    env.adapter.people[ADMIN] = replace(original, joined=original.joined + DAY)
    assert not await env.service.is_admin(env.adapter, GROUP, ADMIN)


async def test_promotion_notice_protects_target_despite_stale_member_role(env):
    await env.service.observe(
        {
            "post_type": "notice",
            "notice_type": "group_admin",
            "sub_type": "set",
            "self_id": ACCOUNT,
            "group_id": GROUP,
            "user_id": "200001",
            "time": int(env.clock()),
        }
    )
    plan = await env.service.build_plan(env.policy, env.adapter)
    assert "200001" not in {item["member"]["user_id"] for item in plan["members"]}
    assert not await env.service.is_admin(
        env.adapter, GROUP, "200001"
    )  # A notice alone never grants authority.


async def test_confirmed_plan_is_cancelled_when_demotion_arrives_before_execution(env):
    policy = replace(env.policy, mode="确认后清理")
    env.box.settings = replace(env.box.settings, groups=(policy,))
    plan = await env.service.build_plan(policy, env.adapter)
    await env.service.confirm(plan["id"], policy, env.adapter, ADMIN)
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
    with pytest.raises(CleanerError, match="确认人"):
        await Executor(env.service).execute(plan, env.adapter)
    assert not env.adapter.kicks


async def test_intent_rechecks_new_exemption_and_activity_in_same_transaction(env):
    plan = await env.service.build_plan(env.policy, env.adapter)
    await env.store.call("plan_state", plan["id"], "running")
    candidate = Member(**plan["members"][0]["member"])
    await env.store.call("protect", ACCOUNT, GROUP, candidate.user_id, 0, ADMIN, env.clock())
    with pytest.raises(CleanerError, match="保护名单"):
        await env.store.call("reserve", plan["id"], candidate, env.clock(), 20, 30)
    candidate = Member(**plan["members"][1]["member"])
    await env.store.call(
        "merge", ACCOUNT, GROUP, [replace(candidate, last_sent=int(env.clock()))], env.clock()
    )
    with pytest.raises(CleanerError, match="活动已变化"):
        await env.store.call("reserve", plan["id"], candidate, env.clock(), 20, 30)


async def test_background_budget_cannot_consume_final_check_and_command_reserves(env):
    for _ in range(120):
        await env.store.call("reserve_read", "test-platform", env.clock(), 120)
    with pytest.raises(Deferred):
        await env.store.call("reserve_read", "test-platform", env.clock(), 120)
    api = Adapter(
        "test-platform",
        Bot({"get_status": {"online": True}}),
        store=env.store,
        sleep=env.clock.sleep,
        clock=env.clock,
    )
    with read_priority(1):
        assert await api.online()
    with read_priority(2):
        assert await api.online()
    assert len(await env.store.call("get", "reads:test-platform")) == 122
    # The reservations belong to the store, not a particular adapter instance.
    replacement = Adapter("test-platform", api.bot, store=env.store, sleep=env.clock.sleep, clock=env.clock)
    with pytest.raises(Deferred):
        await replacement.online()


async def test_independent_groups_continue_while_one_is_waiting(env):
    other = replace(env.policy, group_id="100099")
    env.box.settings = replace(env.box.settings, groups=(env.policy, other))
    started = []
    waiting = asyncio.Event()

    async def check(policy):
        started.append(policy.group_id)
        if policy.group_id == GROUP:
            await waiting.wait()

    env.service.check_group = check
    env.service.dispatch()
    await asyncio.sleep(0)
    assert set(started) == {GROUP, other.group_id}
    assert len(env.service.workers) == 2
    await env.service.stop()
    assert all(task.done() for task in env.service.workers.values())


async def test_stopping_from_tracked_task_never_awaits_itself(env):
    env.service.jobs.add(asyncio.current_task())
    await env.service.stop()
    assert env.service.stopped
    env.service.jobs.clear()


def test_database_connection_closes_even_if_checkpoint_fails(tmp_path):
    store = Store(tmp_path / "fake.sqlite3")
    closed = []

    def execute(_sql):
        raise sqlite3.OperationalError("disk error")

    store.db = SimpleNamespace(execute=execute, close=lambda: closed.append(True))
    with pytest.raises(sqlite3.Error):
        store.close_db()
    assert closed == [True]
    store.worker.shutdown()


async def test_database_semantics_upgrade_preserves_existing_state(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.execute("INSERT INTO state VALUES ('preserved', 'true')")
        db.execute("PRAGMA user_version=1")
    db.close()
    store = Store(path)
    try:
        await store.call("open_db")
        assert await store.call("get", "preserved") is True
    finally:
        await store.close()
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
    db.close()


async def test_websocket_replacement_changes_plan_stamp_and_pins_api_identity():
    clock = Clock()
    bot = Bot({"get_login_info": {"user_id": ACCOUNT}, "get_status": {"online": True}})
    bot._wsr_api_clients = {ACCOUNT: object()}
    api = Adapter("p", bot, sleep=clock.sleep, clock=clock)
    stamp = api.connection_stamp()
    await api.identity()
    assert bot.calls[-1][1]["self_id"] == ACCOUNT
    bot._wsr_api_clients[ACCOUNT] = object()
    assert api.connection_stamp() != stamp
    assert clock() + 300 <= api.recovery_until <= clock() + 900
    bot._wsr_api_clients["999999"] = object()
    with pytest.raises(PlatformError, match="多个 QQ"):
        await api.identity()


async def test_qq_offline_online_without_websocket_change_still_invalidates_stamp():
    clock = Clock()
    bot = Bot({"get_status": {"online": False}})
    api = Adapter("p", bot, sleep=clock.sleep, clock=clock)
    await api.online()
    stamp = api.connection_stamp()
    bot.responses["get_status"] = {"online": True}
    assert await api.online()
    assert api.connection_stamp() != stamp and api.recovery_until > clock()


async def test_a_known_other_qq_offline_does_not_block_selected_qq():
    clock = Clock()

    def platform(pid, account):
        bot = Bot({"get_login_info": {"user_id": account}})
        bot._wsr_api_clients = {account: object()}
        return SimpleNamespace(meta=lambda: SimpleNamespace(id=pid, name="aiocqhttp"), bot=bot)

    platforms = [platform("p1", ACCOUNT), platform("p2", "999999")]
    router = Router(SimpleNamespace(platform_manager=SimpleNamespace(get_insts=lambda: platforms)))
    for p in platforms:
        router.adapters[p.meta().id] = Adapter(p.meta().id, p.bot, sleep=clock.sleep, clock=clock)
    policy = Policy(GROUP, bot_qq=ACCOUNT)
    selected = await router.resolve(policy)
    platforms[1].bot._wsr_api_clients.clear()
    assert await router.resolve(policy) is selected
    platforms[1].bot._wsr_api_clients[ACCOUNT] = object()
    with pytest.raises(PlatformError, match="重复连接"):
        router.binding_stamp(selected)
