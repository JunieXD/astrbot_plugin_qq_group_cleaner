import asyncio
import json
import sqlite3
from dataclasses import replace

import pytest
from conftest import ACCOUNT, GROUP
from test_platform import adapter

from qq_group_cleaner.config import CleanerError
from qq_group_cleaner.executor import Executor
from qq_group_cleaner.platform import PlatformError
from qq_group_cleaner.resources import Journal
from qq_group_cleaner.rules import Member


def rows(journal, name="cleaner.log"):
    return [json.loads(line) for line in (journal.directory / name).read_text(encoding="utf-8").splitlines()]


def test_repeated_events_and_long_reasons_are_preserved_as_individual_json_records(tmp_path):
    journal = Journal(tmp_path)
    try:
        detail = "完整原因" * 1000 + "\n不是另一条日志"
        for _ in range(2):
            journal.record("成员跳过", detail, gid=GROUP, user="200001")
        recorded = rows(journal)
        assert len(recorded) == 2
        assert all(row["detail"] == detail and row["gid"] == GROUP for row in recorded)
        assert all(row["at"].endswith("+08:00") for row in recorded)
    finally:
        journal.close()


async def test_parallel_group_contexts_do_not_mix_and_are_reset_after_exception(tmp_path):
    journal = Journal(tmp_path)
    try:

        async def job(gid):
            with journal.span("检查", gid=gid, check=gid):
                await asyncio.sleep(0)
                journal.record("检查中", expected_group=gid)
                raise ValueError("数据格式错误")

        await asyncio.gather(job("100002"), job("100003"), return_exceptions=True)
        journal.record("无群上下文")
        recorded = rows(journal)
        assert all(row["gid"] == row["expected_group"] for row in recorded if row["event"] == "检查中")
        assert len([row for row in recorded if row["event"] == "检查失败"]) == 2
        assert "gid" not in recorded[-1]
    finally:
        journal.close()


def test_exception_chain_has_frames_and_messages_without_credentials_or_source_lines(tmp_path):
    journal = Journal(tmp_path)
    try:
        try:
            try:
                raise ConnectionError(
                    "10054 wss://server/ws?access_token=private1 Bearer private2 api_key=private3"
                )
            except ConnectionError as exc:
                raise PlatformError("请求失败\n检查连接 abk_private4") from exc
        except PlatformError as exc:
            journal.record("接口失败", exception=exc)
        raw = (journal.directory / "cleaner.log").read_text(encoding="utf-8")
        assert all(secret not in raw for secret in ("private1", "private2", "private3", "private4"))
        chain = rows(journal)[0]["exception"]
        assert [error["type"] for error in chain] == ["PlatformError", "ConnectionError"]
        assert "10054" in chain[1]["message"]
        assert chain[1]["frames"][-1]["line"] > 0
        assert set(chain[1]["frames"][-1]) == {"file", "line", "function"}
    finally:
        journal.close()


async def test_all_members_have_actual_decisions_and_rank_selection_matches_plan(env):
    env.box.settings = replace(env.box.settings, pace=replace(env.box.settings.pace, batch_size=1))
    plan = await env.service.build_plan(env.policy, env.adapter)
    decisions = [row for row in env.journal.records if row["event"] == "成员筛选"]
    assert len(decisions) == len(env.adapter.people)
    assert all(row["plan"] == plan["id"] and row["gid"] == GROUP for row in decisions)
    assert {row["user"] for row in decisions if row["selected"]} == {plan["members"][0]["member"]["user_id"]}
    assert sum(row["eligible"] for row in decisions) == plan["eligible"]
    assert sum(row["selection_reason"] == "排序在本批名额之外" for row in decisions) == 2
    assert next(row for row in decisions if row["user"] == ACCOUNT)["reason"] == "群主、管理员或机器人自身"


async def test_successful_execution_logs_each_target_and_verified_result(env):
    plan = await env.service.build_plan(env.policy, env.adapter)
    await Executor(env.service).execute(plan, env.adapter)
    submitted = [row for row in env.journal.records if row["event"] == "写前意图已保存"]
    verified = [row for row in env.journal.records if row["event"] == "操作核验结果"]
    assert len(submitted) == len(verified) == len(env.adapter.kicks) == 3
    assert {row["user"] for row in verified} == set(env.adapter.kicks)
    assert {row["operation"] for row in verified} == {row["operation"] for row in submitted}
    assert all(row["plan"] == plan["id"] and row["state"] == "observed_absent" for row in verified)
    assert all(row["reason"] for row in verified)


async def test_inflight_failure_records_whether_request_was_sent_and_remaining_members(env):
    plan = await env.service.build_plan(env.policy, env.adapter)
    env.adapter.behavior = "timeout"
    with pytest.raises(PlatformError):
        await Executor(env.service).execute(plan, env.adapter)
    failure = next(row for row in env.journal.records if row["event"] == "移出请求中断")
    assert failure["transport_entered"] is True and failure["operation"]
    assert failure["user"] == env.adapter.kicks[0]
    assert failure["reason"] == "不重复提交"
    assert next(row for row in env.journal.records if row["event"] == "批次中断")["remaining"]


async def test_failed_absence_check_records_exception_before_unknown_result(env):
    plan = await env.service.build_plan(env.policy, env.adapter)

    async def failed_snapshot(gid):
        raise PlatformError("名单读取失败")

    env.adapter.snapshot = failed_snapshot
    with pytest.raises(CleanerError, match="移出结果不明"):
        await Executor(env.service).execute(plan, env.adapter)
    failure = next(row for row in env.journal.records if row["event"] == "离群核验查询失败")
    assert str(failure["exception"]) == "名单读取失败"
    assert next(row for row in env.journal.records if row["event"] == "操作核验结果")["state"] == "unknown"


async def test_cancellation_during_member_wait_is_recorded_without_a_submission(env):
    plan = await env.service.build_plan(env.policy, env.adapter)

    async def cancelled(seconds):
        raise asyncio.CancelledError

    env.service.sleep = cancelled
    with pytest.raises(asyncio.CancelledError):
        await Executor(env.service).execute(plan, env.adapter)
    assert any(row["event"] == "批次执行取消" for row in env.journal.records)
    assert not env.adapter.kicks
    assert not any(row["event"] == "写前意图已保存" for row in env.journal.records)


async def test_recent_activity_skip_has_reason_and_updated_evidence(env):
    env.box.settings = replace(env.box.settings, pace=replace(env.box.settings.pace, batch_size=1))
    plan = await env.service.build_plan(env.policy, env.adapter)
    uid = plan["members"][0]["member"]["user_id"]

    async def changed(seconds):
        env.clock.hook = None
        env.adapter.people[uid] = replace(env.adapter.people[uid], last_sent=int(env.clock()))

    env.clock.hook = changed
    await Executor(env.service).execute(plan, env.adapter)
    skipped = next(row for row in env.journal.records if row["event"] == "成员跳过")
    assert skipped["user"] == uid and "未达到" in skipped["reason"]
    assert not env.adapter.kicks


async def test_activity_log_excludes_chat_body_and_records_duplicate_reason(env):
    raw = {
        "post_type": "message",
        "message_type": "group",
        "self_id": ACCOUNT,
        "group_id": GROUP,
        "user_id": "200001",
        "time": int(env.clock()),
        "message_id": 123,
        "message": "private chat body",
        "raw_message": "private chat body",
    }
    await env.service.observe(raw)
    await env.service.observe(raw)
    recorded = [row for row in env.journal.records if row["event"] == "群事件处理"]
    assert len(recorded) == 2 and recorded[-1]["result"] == "重复事件，已忽略"
    assert "private chat body" not in json.dumps(recorded)


async def test_below_threshold_check_records_count_reason_without_member_queries(env):
    policy = replace(env.policy, trigger=100, target=90)
    env.box.settings = replace(env.box.settings, groups=(policy,))
    assert await env.service.build_plan(policy, env.adapter) is None
    record = next(row for row in env.journal.records if row["event"] == "本次不生成名单")
    assert record["count"] == 5 and record["trigger"] == 100
    assert record["reason"] == "人数未达到开始线" and record["plan"]
    assert not env.adapter.snapshot_calls


async def test_api_error_logs_action_ids_retcode_and_redacted_server_message(tmp_path):
    journal = Journal(tmp_path)
    api = adapter(
        {"get_group_member_info": {"status": "failed", "retcode": 1400, "wording": "token=private"}}
    )
    api.journal = journal
    try:
        with pytest.raises(PlatformError):
            await api.member(GROUP, "200001")
        recorded = rows(journal)
        status = next(row for row in recorded if row["event"] == "接口返回状态")
        assert status["retcode"] == 1400 and status["api"] == "get_group_member_info"
        assert status["group_id"] == GROUP and status["user_id"] == "200001"
        assert "private" not in json.dumps(recorded)
        assert recorded[-1]["event"] == "平台接口失败" and recorded[-1]["exception"]
    finally:
        journal.close()


async def test_api_group_level_does_not_overwrite_log_severity(tmp_path):
    journal = Journal(tmp_path)
    api = adapter({"get_group_member_info": {"user_id": "200001", "level": "0"}})
    api.journal = journal
    try:
        await api.member(GROUP, "200001")
        summary = next(row for row in rows(journal) if row["event"] == "接口数据摘要")
        assert summary["level"] == "INFO" and summary["response"]["level"] == "0"
    finally:
        journal.close()


async def test_matching_kick_notice_logs_confirmed_operation_identity(env):
    plan = await env.service.build_plan(env.policy, env.adapter)
    candidate = Member(**plan["members"][0]["member"])
    await env.store.call("plan_state", plan["id"], "running")
    operation = await env.store.call("reserve", plan["id"], candidate, env.clock(), 20, 30)
    await env.service.observe(
        {
            "post_type": "notice",
            "notice_type": "group_decrease",
            "sub_type": "kick",
            "self_id": ACCOUNT,
            "group_id": GROUP,
            "user_id": candidate.user_id,
            "operator_id": ACCOUNT,
            "time": int(env.clock()),
        }
    )
    recorded = next(row for row in env.journal.records if row["event"] == "通知确认移出")
    assert recorded["operation"] == operation and recorded["plan"] == plan["id"]
    assert recorded["state"] == "confirmed_removed"


def test_failed_host_logger_cannot_leave_a_broken_file_handler_healthy(tmp_path, monkeypatch):
    journal = Journal(tmp_path)

    def fail(*args, **kwargs):
        raise OSError("host logging unavailable")

    monkeypatch.setattr("qq_group_cleaner.resources.logging.Logger.error", fail)
    try:
        journal.handler.handleError(None)
        with pytest.raises(CleanerError, match="日志"):
            journal.check()
    finally:
        journal.close()


async def test_storage_failure_records_original_database_error(env):
    def fail():
        raise sqlite3.OperationalError("database is locked")

    env.store.fail = fail
    with pytest.raises(CleanerError):
        await env.store.call("fail")
    recorded = next(row for row in env.journal.records if row["event"] == "数据库操作失败")
    assert recorded["method"] == "fail"
    assert str(recorded["exception"]) == "database is locked"


async def test_screening_log_rotation_failure_stops_removal(env, monkeypatch):
    journal = Journal(env.path)
    env.service.journal = journal
    journal.screening_handler.maxBytes = 1

    def fail():
        raise OSError("file locked")

    monkeypatch.setattr(journal.screening_handler, "doRollover", fail)
    try:
        plan = await env.service.build_plan(env.policy, env.adapter)
        with pytest.raises(CleanerError, match="日志"):
            await Executor(env.service).execute(plan, env.adapter)
        assert not env.adapter.kicks
    finally:
        env.service.journal = env.journal
        journal.close()


def test_close_attempts_both_handlers_even_if_one_flush_fails(tmp_path, monkeypatch):
    journal = Journal(tmp_path)

    def fail():
        raise OSError("flush failure")

    monkeypatch.setattr(journal.screening_handler, "flush", fail)
    with pytest.raises(OSError):
        journal.close()
    assert not journal.logger.handlers and not journal.screening_logger.handlers
    assert journal.handler.stream is None and journal.screening_handler.stream is None
