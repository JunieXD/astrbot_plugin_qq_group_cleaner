import asyncio
import os
import sqlite3
import time
from dataclasses import replace

import pytest
from conftest import ACCOUNT, ADMIN, GROUP

from qq_group_cleaner.config import CleanerError, Pace
from qq_group_cleaner.executor import in_window
from qq_group_cleaner.resources import InstanceLock, Journal
from qq_group_cleaner.rules import Member
from qq_group_cleaner.store import Store


async def test_intent_quota_unique_and_restart_recovery(env):
    plan = await env.service.build_plan(env.policy, env.adapter)
    candidate = Member(**plan["members"][0]["member"])
    await env.store.call("plan_state", plan["id"], "running")
    operation = await env.store.call("reserve", plan["id"], candidate, env.clock(), 20, 30)
    with pytest.raises(CleanerError):
        await env.store.call("reserve", plan["id"], candidate, env.clock(), 20, 30)
    await env.store.close()
    recovered = Store(env.path / "state.sqlite3")
    await recovered.call("open_db")
    try:
        assert (await recovered.call("operation", operation))["state"] == "unknown"
        assert (await recovered.call("plan", plan["id"]))["state"] == "cancelled"
        assert await recovered.call("quota", ACCOUNT, GROUP, env.clock()) == (1, 1)
    finally:
        await recovered.close()


async def test_retention_preserves_unresolved_operations_and_current_epoch_attempts(env):
    plan = await env.service.build_plan(env.policy, env.adapter)
    await env.store.call("plan_state", plan["id"], "running")
    candidate = Member(**plan["members"][0]["member"])
    operation = await env.store.call("reserve", plan["id"], candidate, env.clock(), 20, 30)
    await env.store.call("maintain", env.clock() + 400 * 86400)
    assert (await env.store.call("operation", operation))["state"] == "submitted"
    await env.store.call("result", operation, "reviewed_retained", "人工保留", env.clock())
    await env.store.call("maintain", env.clock() + 400 * 86400)
    assert (await env.store.call("operation", operation))["state"] == "reviewed_retained"


async def test_maintenance_preserves_all_old_history(env):
    database = env.path / "state.sqlite3"
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO events VALUES ('old-event-for-statistics', 1)")
        db.execute(
            "INSERT INTO audit(at,account,gid,kind,detail) VALUES (1,?,?,?,?)",
            (ACCOUNT, GROUP, "old-audit-for-statistics", "{}"),
        )
        for plan in ("old-unreferenced-plan", "old-referenced-plan"):
            db.execute(
                "INSERT INTO plans VALUES (?,?,?,1,2,'completed','{}',NULL)",
                (plan, ACCOUNT, GROUP),
            )
        db.execute(
            "INSERT INTO members VALUES (?,?,?,1,2,1,1,1)",
            (ACCOUNT, GROUP, "old-membership-for-statistics"),
        )
        db.execute(
            "INSERT INTO operations(plan,account,gid,uid,epoch,submitted,state,reason) "
            "VALUES (?,?,?,?,1,1,'confirmed','old completed operation')",
            ("old-referenced-plan", ACCOUNT, GROUP, "old-membership-for-statistics"),
        )
    tables = ("events", "audit", "plans", "operations")
    with sqlite3.connect(database) as db:
        before = {table: db.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall() for table in tables}
    await env.store.call("maintain", env.clock() + 400 * 86400)
    with sqlite3.connect(database) as db:
        after = {table: db.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall() for table in tables}
    assert after == before


async def test_startup_delay_persistent_and_stop_cancels_scheduler(env):
    env.box.settings = replace(env.box.settings, enabled=False)
    await env.service.start()
    until = await env.store.call("get", "startup_until")
    assert env.clock() + 30 <= until <= env.clock() + 90
    await env.service.stop()
    assert env.service.task.done()
    assert not env.adapter.kicks
    assert await env.store.call("get", "startup_until") == until


async def test_stop_cancels_active_private_command_job(env):
    task = asyncio.create_task(asyncio.Event().wait())
    env.service.jobs.add(task)
    await env.service.stop()
    assert task.cancelled()


async def test_clock_rollback_stops_writes(env):
    env.service.last_clock = (env.clock() + 500, env.clock())
    with pytest.raises(CleanerError, match="时间"):
        env.service.healthy()
    assert env.service.failure


def test_window_day_overnight_and_full_day(env):
    now = env.clock()  # 10:00 Beijing
    assert in_window(now, Pace())
    assert not in_window(now, Pace(start_hour=22, end_hour=6))
    assert in_window(now + 13 * 3600, Pace(start_hour=22, end_hour=6))
    assert in_window(now, Pace(start_hour=0, end_hour=24))


def test_instance_lock_excludes_second_owner_and_releases(tmp_path):
    path = tmp_path / "instance.lock"
    first = InstanceLock(path)
    try:
        with pytest.raises(CleanerError, match="实例"):
            InstanceLock(path)
    finally:
        first.close()
    InstanceLock(path).close()


def test_log_rotation_preserves_old_archives_and_checks_failure(tmp_path):
    journal = Journal(tmp_path)
    assert journal.handler.maxBytes == journal.screening_handler.maxBytes == 20 * 1024 * 1024
    assert journal.handler.backupCount == journal.screening_handler.backupCount == 7
    journal.handler.maxBytes = 120
    for i in range(30):
        journal.record("test", f"{i:02d} " + "x" * 60)
    assert len(list((tmp_path / "logs").glob("cleaner.log*"))) <= 8
    archive = tmp_path / "logs" / "cleaner.log.7"
    assert archive.exists()
    os.utime(archive, (time.time() - 15 * 86400,) * 2)
    journal.maintain()
    assert archive.exists()
    journal.handler.handleError(None)
    with pytest.raises(CleanerError, match="日志"):
        journal.check()
    journal.close()
    assert not journal.logger.handlers


async def test_forged_old_notice_does_not_confirm_a_new_operation(env):
    plan = await env.service.build_plan(env.policy, env.adapter)
    await env.store.call("plan_state", plan["id"], "running")
    candidate = Member(**plan["members"][0]["member"])
    operation = await env.store.call("reserve", plan["id"], candidate, env.clock(), 20, 30)
    await env.store.call(
        "observe",
        ACCOUNT,
        GROUP,
        candidate.user_id,
        "group_decrease",
        env.clock() - 100,
        env.clock(),
        "old-event",
        ACCOUNT,
        "kick",
    )
    assert (await env.store.call("operation", operation))["state"] == "submitted"


async def test_confirmation_rejects_foreign_group_binding_and_new_configuration(env):
    policy = replace(env.policy, mode="确认后清理")
    env.box.settings = replace(env.box.settings, groups=(policy,))
    plan = await env.service.build_plan(policy, env.adapter)
    with pytest.raises(CleanerError, match="不属于"):
        await env.service.confirm(plan["id"], replace(policy, group_id="999999"), env.adapter, ADMIN)
    env.box.settings = replace(env.box.settings, pace=replace(env.box.settings.pace, max_delay=120))
    with pytest.raises(CleanerError, match="配置"):
        await env.service.confirm(plan["id"], policy, env.adapter, ADMIN)
