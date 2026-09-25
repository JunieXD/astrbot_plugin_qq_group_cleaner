import asyncio
from dataclasses import replace
from datetime import datetime

import pytest
from conftest import ACCOUNT, ADMIN, GROUP
from test_audit_logging import rows

from qq_group_cleaner.commands import Commands
from qq_group_cleaner.config import CleanerError, Deferred, Pace
from qq_group_cleaner.executor import (
    CHINA,
    Executor,
    PlanTimeUnavailable,
    execution_wait,
    next_window,
    window_end,
)
from qq_group_cleaner.resources import Journal
from qq_group_cleaner.rules import Member


def stamp(hour, minute=0, day=21):
    return datetime(2026, 9, day, hour, minute, tzinfo=CHINA).timestamp()


@pytest.mark.parametrize(
    "start,end,hour,opening,closing",
    [
        (8, 22, 7, stamp(8), stamp(22)),
        (8, 22, 8, stamp(8), stamp(22)),
        (8, 22, 22, stamp(8, day=22), stamp(22, day=22)),
        (22, 6, 21, stamp(22), stamp(6, day=22)),
        (22, 6, 23, stamp(23), stamp(6, day=22)),
        (22, 6, 5, stamp(5), stamp(6)),
        (22, 6, 6, stamp(22), stamp(6, day=22)),
        (20, 24, 0, stamp(20), stamp(0, day=22)),
        (0, 24, 23, stamp(23), float("inf")),
    ],
)
def test_day_overnight_and_full_day_boundaries(start, end, hour, opening, closing):
    pace = Pace(start_hour=start, end_hour=end)
    assert next_window(stamp(hour), pace) == opening
    assert window_end(opening, pace) == closing


async def reserve(env, plan, index, at, state="observed_absent"):
    await env.store.call("plan_state", plan["id"], "running")
    member = Member(**plan["members"][index]["member"])
    op = await env.store.call("reserve", plan["id"], member, at, 100, 100)
    if state != "submitted":
        await env.store.call("result", op, state, "test", at)
    return op


async def test_rolling_quota_uses_both_scopes_lowered_limits_and_exact_boundary(env):
    now = env.clock()
    plan = await env.service.build_plan(env.policy, env.adapter)
    for index, age in enumerate((300, 200, 100)):
        await reserve(env, plan, index, now - age)
    # Lowering the limit to one requires all three attempts to age out.
    assert await env.store.call("quota_ready_at", ACCOUNT, GROUP, now, 1, 100) == now + 86300
    assert await env.store.call("quota_ready_at", ACCOUNT, "999999", now, 1, 2) == now + 86200
    assert await env.store.call("quota_ready_at", "999998", GROUP, now, 1, 1) == now
    assert await env.store.call("quota_ready_at", ACCOUNT, GROUP, now + 86300, 1, 1) == now + 86300
    assert await env.store.call("quota", ACCOUNT, GROUP, now + 86300) == (0, 0)


async def test_cancelled_unsent_intent_releases_quota_deadline(env):
    now = env.clock()
    plan = await env.service.build_plan(env.policy, env.adapter)
    op = await reserve(env, plan, 0, now, "submitted")
    assert await env.store.call("quota_ready_at", ACCOUNT, GROUP, now, 1, 1) == now + 86400
    await env.store.call("cancel_intent", op, now)
    assert await env.store.call("quota_ready_at", ACCOUNT, GROUP, now, 1, 1) == now


async def test_wait_combines_quota_batch_recovery_and_next_window(env):
    plan = await env.service.build_plan(env.policy, env.adapter)
    await reserve(env, plan, 0, stamp(21, 59, day=20))
    pace = replace(env.box.settings.pace, start_hour=8, end_hour=22, account_daily_limit=1)
    await env.store.call("set", f"batch:{ACCOUNT}:{GROUP}", stamp(12))
    until, reason = await execution_wait(
        env.store, ACCOUNT, GROUP, env.clock(), pace, connection_until=stamp(13)
    )
    assert until == stamp(8, day=22)
    assert reason == "下一个完整执行时段"
    await env.store.call("set", "startup_until", stamp(23, day=22))
    until, reason = await execution_wait(env.store, ACCOUNT, GROUP, env.clock(), pace)
    assert until == stamp(8, day=23) and reason == "执行时段"


@pytest.mark.parametrize("wait", ["batch", "window", "quota", "recovery"])
async def test_wait_only_reads_count_and_manual_preview_still_works(env, wait):
    now = env.clock()
    deadline = now + 600
    pace = env.box.settings.pace
    if wait == "batch":
        await env.store.call("set", f"batch:{ACCOUNT}:{GROUP}", deadline)
    elif wait == "window":
        pace = replace(pace, start_hour=11, end_hour=22)
        deadline = stamp(11)
    elif wait == "recovery":
        env.adapter.recovery_until = deadline
    else:
        plan = await env.service.build_plan(env.policy, env.adapter)
        await reserve(env, plan, 0, now - 85800)
        pace = replace(pace, account_daily_limit=1)
    policy = replace(env.policy, max_qq_level=20)
    env.box.settings = replace(env.box.settings, pace=pace, groups=(policy,))
    env.adapter.snapshot_calls = 0
    env.adapter.details.clear()
    await env.service.check_group(policy)
    assert not env.adapter.snapshot_calls and not env.adapter.details and not env.adapter.kicks
    status = await env.store.call("get", f"status:{ACCOUNT}:{GROUP}")
    assert status["count"] == 5 and status["at"] == now
    wait_status = await env.store.call("get", f"wait-status:{GROUP}")
    assert wait_status["until"] == deadline
    # Long waits retain lightweight count checks, short waits wake at the deadline.
    assert env.service.next_check[GROUP] == (
        deadline if wait != "window" else pytest.approx(now + 1950, abs=150)
    )
    reply = await Commands(env.service).status(policy, env.adapter)
    assert "暂停原因" not in reply and "正在等待" in reply
    preview = await env.service.build_plan(policy, env.adapter, manual=True)
    assert preview["state"] == "preview" and env.adapter.snapshot_calls == 1
    assert env.adapter.details and not env.adapter.kicks


async def test_automatic_list_is_fresh_when_wait_ends(env):
    old = await env.service.build_plan(env.policy, env.adapter)
    deadline = env.clock() + 600
    await env.store.call("set", f"batch:{ACCOUNT}:{GROUP}", deadline)
    await env.service.check_group(env.policy)
    assert env.adapter.snapshot_calls == 1 and not env.adapter.kicks
    uid = old["members"][0]["member"]["user_id"]
    env.adapter.people[uid] = replace(env.adapter.people[uid], last_sent=int(deadline))
    env.clock.now = deadline
    await env.service.check_group(env.policy)
    latest = await env.store.call("latest", ACCOUNT, GROUP)
    assert latest["id"] != old["id"]
    assert uid not in env.adapter.kicks and len(env.adapter.kicks) == 2
    assert (await env.store.call("plan", old["id"]))["state"] == "cancelled"
    assert await env.store.call("get", f"wait-status:{GROUP}") == {}


@pytest.mark.parametrize("confirmed", [False, True])
async def test_manual_list_is_preserved_while_waiting_and_never_replaced_when_expired(env, confirmed):
    policy = replace(env.policy, mode="确认后清理")
    env.box.settings = replace(env.box.settings, groups=(policy,))
    plan = await env.service.build_plan(policy, env.adapter, manual=True)
    if confirmed:
        await env.service.confirm(plan["id"], policy, env.adapter, ADMIN)
    await env.store.call("set", f"batch:{ACCOUNT}:{GROUP}", env.clock() + 3600)
    await env.service.check_group(policy)
    assert (await env.store.call("latest", ACCOUNT, GROUP))["id"] == plan["id"]
    assert env.adapter.snapshot_calls == 1 and not env.adapter.kicks
    assert env.service.next_check[GROUP] == plan["expires"]
    env.clock.now = plan["expires"]
    await env.service.check_group(policy)
    assert (await env.store.call("latest", ACCOUNT, GROUP))["state"] == "cancelled"
    assert env.adapter.snapshot_calls == 1 and not env.adapter.kicks
    assert "重新预览并确认" in (await env.store.call("get", f"wait-status:{GROUP}"))["reason"]


async def test_confirmed_members_are_not_replaced_after_wait(env):
    policy = replace(env.policy, mode="确认后清理")
    env.box.settings = replace(
        env.box.settings, groups=(policy,), pace=replace(env.box.settings.pace, batch_size=1)
    )
    plan = await env.service.build_plan(policy, env.adapter, manual=True)
    await env.service.confirm(plan["id"], policy, env.adapter, ADMIN)
    deadline = env.clock() + 600
    await env.store.call("set", f"batch:{ACCOUNT}:{GROUP}", deadline)
    await env.service.check_group(policy)
    uid = plan["members"][0]["member"]["user_id"]
    env.adapter.people[uid] = replace(env.adapter.people[uid], last_sent=int(deadline))
    env.clock.now = deadline
    await env.service.check_group(policy)
    assert not env.adapter.kicks and env.adapter.snapshot_calls == 1
    result = next(row for row in env.journal.records if row["event"] == "批次结果" and row["skipped"])
    assert result["skipped"] == 1 and result["unprocessed"] == 0


@pytest.mark.parametrize("remaining,removed", [(120, 0), (150, 1)])
async def test_plan_time_budget_stops_before_starting_first_or_next_member(env, remaining, removed):
    plan = await env.service.build_plan(env.policy, env.adapter)
    env.clock.now = plan["expires"] - remaining
    with pytest.raises(PlanTimeUnavailable):
        await Executor(env.service).execute(plan, env.adapter)
    assert len(env.adapter.kicks) == removed
    result = next(row for row in env.journal.records if row["event"] == "批次结果")
    assert result["submitted"] == result["removed"] == removed
    assert result["unprocessed"] == 3 - removed
    assert result["outcome"] == "waiting"
    assert (await env.store.call("plan", plan["id"]))["state"] not in ("ready", "running")


async def test_window_closing_stops_second_member_and_schedules_next_opening(env):
    pace = replace(env.box.settings.pace, start_hour=8, end_hour=22)
    env.box.settings = replace(env.box.settings, pace=pace)
    env.clock.now = stamp(21, 57) + 30
    plan = await env.service.build_plan(env.policy, env.adapter)
    with pytest.raises(PlanTimeUnavailable) as caught:
        await Executor(env.service).execute(plan, env.adapter)
    assert caught.value.until == stamp(8, day=22)
    assert len(env.adapter.kicks) == 1


@pytest.mark.parametrize("cause", ["expires", "window", "recovery", "read_budget"])
async def test_wait_inside_shared_queue_is_not_classified_as_platform_failure(env, cause):
    env.box.settings = replace(
        env.box.settings, pace=replace(env.box.settings.pace, start_hour=8, end_hour=22)
    )
    if cause == "window":
        env.clock.now = stamp(21, 50)
    plan = await env.service.build_plan(env.policy, env.adapter)
    failures = []
    skipped = []

    class Guard:
        deferred_error = RuntimeError

        async def run(self, **kwargs):
            if cause == "expires":
                env.clock.now = plan["expires"]
            elif cause == "window":
                env.clock.now = stamp(22)
            elif cause == "recovery":
                await env.store.call("set", "cooldown:" + ACCOUNT, env.clock() + 500)
            else:

                async def wait():
                    raise Deferred("本小时资料读取已达上限", env.clock() + 500)

                env.adapter.online = wait
            try:
                return await kwargs["action"]()
            except self.deferred_error as exc:
                skipped.append(exc)
                raise
            except Exception as exc:
                failures.append(exc)
                raise

    env.router.guard = Guard()
    with pytest.raises(Deferred):
        await Executor(env.service).execute(plan, env.adapter)
    assert len(skipped) == 1 and not failures and not env.adapter.kicks
    assert not await env.store.call("history", ACCOUNT, GROUP)


@pytest.mark.parametrize("outcome", ["success", "skip", "unknown", "cancel", "unsent", "target"])
async def test_batch_summary_accounts_for_every_planned_member(env, outcome):
    plan = await env.service.build_plan(env.policy, env.adapter)
    if outcome == "skip":
        env.adapter.people["200001"] = replace(env.adapter.people["200001"], last_sent=int(env.clock()))
    elif outcome == "unknown":
        env.adapter.behavior = "timeout"
    elif outcome == "cancel":

        async def cancel(seconds):
            raise asyncio.CancelledError()

        env.service.sleep = cancel
    elif outcome == "unsent":

        async def unsent(uid):
            raise CleanerError("发送前取消")

        env.adapter.kick_hook = unsent
    elif outcome == "target":
        env.adapter.people = {uid: env.adapter.people[uid] for uid in (ACCOUNT, "200001")}
    if outcome in ("unknown", "unsent", "cancel"):
        with pytest.raises(asyncio.CancelledError if outcome == "cancel" else CleanerError):
            await Executor(env.service).execute(plan, env.adapter)
    else:
        await Executor(env.service).execute(plan, env.adapter)
    result = next(row for row in env.journal.records if row["event"] == "批次结果")
    assert result["planned"] == result["submitted"] + result["skipped"] + result["unprocessed"] == 3
    assert result["submitted"] == len(env.adapter.kicks)
    assert result["unresolved"] == (1 if outcome == "unknown" else 0)
    assert result["skipped"] == (1 if outcome == "skip" else 0)
    assert result["removed"] == (3 if outcome == "success" else 2 if outcome == "skip" else 0)


def test_wait_logs_are_info_without_stacks_but_real_errors_keep_stacks(tmp_path):
    journal = Journal(tmp_path)
    try:
        with pytest.raises(Deferred), journal.span("计划检查", plan="test"):
            exc = Deferred("正在等待批次间隔", 12345)
            journal.record("批次暂缓", exception=exc)
            raise exc
        with pytest.raises(ValueError), journal.span("计划检查", plan="broken"):
            raise ValueError("损坏的数据")
        recorded = rows(journal)
        waits = [row for row in recorded if row["event"] in ("批次暂缓", "计划检查等待")]
        assert len(waits) == 2
        assert all(
            row["level"] == "INFO" and "exception" not in row and row["retry_at"] == 12345 for row in waits
        )
        failed = next(row for row in recorded if row["event"] == "计划检查失败")
        assert failed["level"] == "ERROR" and failed["exception"][0]["frames"]
    finally:
        journal.close()


async def test_configuration_change_rechecks_schedule_without_clearing_reservations(env):
    deadline = env.clock() + 600
    await env.store.call("set", f"batch:{ACCOUNT}:{GROUP}", deadline)
    await env.store.call("set", "write-at:" + ACCOUNT, deadline + 1)
    env.service.next_check[GROUP] = env.clock() + 3600
    env.box.settings = replace(env.box.settings, pace=replace(env.box.settings.pace, end_hour=24))
    env.service.dispatch()
    assert GROUP in env.service.workers
    await env.service.workers[GROUP]
    assert env.service.wake.is_set()
    assert env.service.next_check[GROUP] == deadline
    assert await env.store.call("get", f"batch:{ACCOUNT}:{GROUP}") == deadline
    assert await env.store.call("get", "write-at:" + ACCOUNT) == deadline + 1
    assert not env.adapter.snapshot_calls
