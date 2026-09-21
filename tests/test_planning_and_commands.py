from dataclasses import replace

import pytest
from conftest import ACCOUNT, ADMIN, GROUP, member

from qq_group_cleaner.commands import Commands, render_plan
from qq_group_cleaner.config import CleanerError
from qq_group_cleaner.executor import Executor
from qq_group_cleaner.rules import DAY


async def command(env, text, actor=ADMIN, platform="test-platform", account=ACCOUNT):
    env.service.command_next.clear()
    return await Commands(env.service).run(text, actor, platform, account)


async def test_commands_authorize_group_role_and_platform(env):
    for actor, platform, account in [
        ("200001", "test-platform", ACCOUNT),
        (ADMIN, "wrong", ACCOUNT),
        (ADMIN, "test-platform", "999999"),
    ]:
        with pytest.raises(CleanerError):
            await command(env, f"/群清理 预览 {GROUP}", actor, platform, account)
    assert not env.adapter.kicks
    assert "只读预览" in await command(env, f"/群清理 预览 {GROUP}")


async def test_confirmation_only_approves_existing_exact_batch(env):
    env.box.settings = replace(env.box.settings, groups=(replace(env.policy, mode="确认后清理"),))
    text = await command(env, f"/群清理 预览 {GROUP}")
    plan = await env.store.call("latest", ACCOUNT, GROUP)
    assert plan["id"] in text and "确认" in text
    await command(env, f"/群清理 确认 {GROUP} {plan['id']}")
    assert (await env.store.call("plan", plan["id"]))["approver"] == ADMIN
    with pytest.raises(CleanerError):
        await command(env, f"/群清理 确认 {GROUP} {plan['id']}")
    assert not env.adapter.kicks


async def test_preview_cooldown_and_help(env):
    assert "群清理" in await command(env, "/群清理")
    await command(env, f"/群清理 预览 {GROUP}")
    assert "间隔 1 分钟" in await command(env, f"/群清理 预览 {GROUP}")


async def test_protect_unprotect_pause_resume_and_explain(env):
    assert "永久" in await command(env, f"/群清理 保护 {GROUP} 200001")
    assert "保留" in await command(env, f"/群清理 解释 {GROUP} 200001")
    assert "7 天" in await command(env, f"/群清理 取消保护 {GROUP} 200001")
    protected, _ = await env.store.call("exclusions", ACCOUNT, GROUP, env.clock() + 6 * DAY)
    assert "200001" in protected
    protected, _ = await env.store.call("exclusions", ACCOUNT, GROUP, env.clock() + 8 * DAY)
    assert "200001" not in protected
    await command(env, f"/群清理 暂停 {GROUP}")
    assert "暂停" in await command(env, f"/群清理 状态 {GROUP}")
    await command(env, f"/群清理 恢复 {GROUP}")
    assert await env.store.call("get", "cooldown:" + ACCOUNT) > env.clock()


async def test_unknown_can_be_retained_without_repeating_write(env):
    env.adapter.behavior = "silent"
    plan = await env.service.build_plan(env.policy, env.adapter)
    with pytest.raises(CleanerError):
        await Executor(env.service).execute(plan, env.adapter)
    uid = env.adapter.kicks[0]
    text = await command(env, f"/群清理 核对 {GROUP}")
    assert "结果不明" in text and len(env.adapter.kicks) == 1
    await command(env, f"/群清理 保留 {GROUP} {uid}")
    assert not await env.store.call("unresolved", ACCOUNT, GROUP)
    assert uid in (await env.store.call("exclusions", ACCOUNT, GROUP, env.clock()))[0]
    await command(env, f"/群清理 恢复 {GROUP}")
    assert len(env.adapter.kicks) == 1


async def test_cycle_hysteresis_target_and_expiry(env):
    service = env.service
    revision = env.box.settings.revision
    assert await service.cycle(env.policy, ACCOUNT, 5, revision)
    assert await service.cycle(env.policy, ACCOUNT, 3, revision)
    assert not await service.cycle(env.policy, ACCOUNT, 2, revision)
    assert not await service.cycle(env.policy, ACCOUNT, 3, revision)
    assert await service.cycle(env.policy, ACCOUNT, 5, revision)
    env.clock.now += 8 * DAY
    assert not await service.cycle(env.policy, ACCOUNT, 3, revision)


async def test_qq_enrichment_bounded_and_complete_before_sorting(env):
    env.box.settings = replace(env.box.settings, groups=(replace(env.policy, order="QQ等级低优先"),))
    for i in range(31):
        uid = str(300001 + i)
        env.adapter.people[uid] = member(uid, env.clock(), qq_level=40 - i)
    plan = await env.service.build_plan(env.box.settings.groups[0], env.adapter)
    assert len(env.adapter.details) == 20
    assert plan["waiting"] == 14
    assert plan["members"] == [] and plan["state"] == "preview"
    plan = await env.service.build_plan(env.box.settings.groups[0], env.adapter)
    assert len(env.adapter.details) == 34
    assert plan["waiting"] == 0
    assert plan["state"] == "ready"
    assert plan["members"][0]["member"]["qq_level"] == 10


async def test_event_duplicates_out_of_order_and_membership_change(env):
    old = env.adapter.people["200001"]
    merged = (await env.store.call("merge", ACCOUNT, GROUP, [old]))[0]
    event = {
        "post_type": "message",
        "message_type": "group",
        "self_id": ACCOUNT,
        "group_id": GROUP,
        "user_id": "200001",
        "time": int(env.clock()),
        "message_id": 42,
    }
    await env.service.observe(event)
    await env.service.observe(event)
    await env.service.observe({**event, "time": int(env.clock() - DAY), "message_id": 43})
    current = (await env.store.call("merge", ACCOUNT, GROUP, [old]))[0]
    assert current.activity == env.clock()
    assert current.epoch == merged.epoch
    await env.service.observe(
        {**event, "post_type": "notice", "notice_type": "group_decrease", "sub_type": "leave"}
    )
    await env.service.observe(
        {**event, "post_type": "notice", "notice_type": "group_increase", "sub_type": "approve"}
    )
    current = (await env.store.call("merge", ACCOUNT, GROUP, [old]))[0]
    assert current.epoch > merged.epoch
    # Even a stale old member list cannot remove the positive rejoin activity evidence.
    assert current.activity == env.clock()


async def test_status_does_not_expose_internal_settings(env):
    text = await command(env, f"/群清理 状态 {GROUP}")
    assert "24小时" in text and "未发言" in text
    assert "sqlite" not in text and "platform_id" not in text


async def test_disabled_preview_does_not_start_cleanup_cycle(env):
    env.box.settings = replace(env.box.settings, enabled=False)
    plan = await env.service.build_plan(env.policy, env.adapter, manual=True)
    assert plan["state"] == "preview"
    assert not await env.store.call("get", f"cycle:{ACCOUNT}:{GROUP}")
    assert "只读预览" in render_plan(plan)
