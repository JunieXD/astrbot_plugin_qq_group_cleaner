import json
from dataclasses import replace

import pytest
from conftest import ACCOUNT, GROUP, member

from qq_group_cleaner.cards import CardRule, CardRuleError, read_card_guard_config
from qq_group_cleaner.commands import Commands, render_plan
from qq_group_cleaner.config import CleanerError, Policy, parse_settings
from qq_group_cleaner.executor import Executor
from qq_group_cleaner.platform import merge_snapshot_evidence
from qq_group_cleaner.rules import DAY, Member, evaluate
from qq_group_cleaner.store import Store

PATTERN = r"(?>(?:20)?[0-9]{2}(?:[\p{Zs}\t]*级)?)[\p{Zs}\t\-－—–‐‑‒﹣~～,，_＿、:：;；/／|｜·•.．]+[^\s\-－—–‐‑‒﹣~～,，_＿、:：;；/／|｜·•.．][^\r\n]*"


def source(pattern=PATTERN, **options):
    return {"groups": [{"group_id": GROUP, "pattern": pattern, "advanced": options}]}


def configure(env):
    policy = replace(env.policy, inactive_days=90, card_rule=CardRule(PATTERN, True, True))
    env.box.settings = replace(env.box.settings, groups=(policy,))
    for uid in env.adapter.people:
        env.adapter.people[uid] = replace(env.adapter.people[uid], card="23-xxu-昵称")
    env.adapter.people["200001"] = replace(env.adapter.people["200001"], card="未改名片")
    return policy


def test_rule_source_is_per_group_and_changes_invalidate_old_plans():
    raw = {"groups": [{"group_id": GROUP, "require_invalid_card": True}, {"group_id": "100003"}]}
    one = parse_settings(raw, card_guard_config=source(ignore_case=True))
    assert one.group(GROUP).card_rule == CardRule(PATTERN, True, True)
    assert one.group("100003").card_rule is None
    two = parse_settings(raw, card_guard_config=source(r"\d+-\w+", ignore_case=True))
    assert one.revision != two.revision
    # A disabled feature does not depend on the card guard being configured.
    assert (
        parse_settings({"groups": [{"group_id": GROUP}]}, card_guard_config={}).group(GROUP).card_rule is None
    )


@pytest.mark.parametrize(
    "bad", [None, {}, {"groups": []}, source("["), source(""), source(ignore_case="true")]
)
def test_missing_or_invalid_shared_rules_never_disable_the_additional_condition(bad):
    with pytest.raises(CleanerError):
        parse_settings({"groups": [{"group_id": GROUP, "require_invalid_card": True}]}, card_guard_config=bad)


def test_config_file_bom_change_and_failure_are_handled(tmp_path):
    path = tmp_path / "card.json"
    path.write_text(json.dumps(source()), encoding="utf-8-sig")
    first = read_card_guard_config(path)
    path.write_text(json.dumps(source("changed-pattern")), encoding="utf-8-sig")
    assert read_card_guard_config(path) != first
    path.write_text("broken", encoding="utf-8")
    with pytest.raises(CardRuleError):
        read_card_guard_config(path)
    path.unlink()
    with pytest.raises(CardRuleError):
        read_card_guard_config(path)


@pytest.mark.parametrize(
    "card,days,eligible",
    [
        ("", 100, True),
        ("bad", 100, True),
        ("bad", 90, True),
        ("bad", 89, False),
        ("23-xxu-昵称", 100, False),
        ("  2023级 — XXU — 名字  ", 100, False),
        ("23\u3000学校", 100, False),
        (None, 100, False),
        ("x" * 513, 100, False),
    ],
)
def test_invalid_card_and_90_days_are_both_required(card, days, eligible):
    now = 1800000000
    policy = Policy(GROUP, inactive_days=90, card_rule=CardRule(PATTERN, True, True))
    candidate = member("200001", now, card=card, last_sent=int(now - days * DAY))
    assert evaluate(policy, candidate, now, ACCOUNT).eligible is eligible


def test_same_fullmatch_case_and_whitespace_semantics_as_card_guard():
    rule = CardRule(r"23-XXU", strip_spaces=True, ignore_case=True)
    assert rule.matches(" 23-xxu ")
    assert not rule.matches("prefix23-XXU") and not rule.matches("23-XXUsuffix")
    assert not replace(rule, strip_spaces=False).matches(" 23-XXU ")
    assert not replace(rule, ignore_case=False).matches("23-xxu")


def test_empty_card_never_falls_back_to_a_compliant_qq_nickname():
    raw = {"user_id": "200001", "nickname": "23-XXU", "card": ""}
    assert Member.from_api(raw).card == ""
    assert Member.from_api({k: v for k, v in raw.items() if k != "card"}).card is None


def test_regex_timeout_stops_processing_instead_of_treating_member_as_invalid(monkeypatch):
    class Timeout:
        def fullmatch(self, *args, **kwargs):
            raise TimeoutError()

    monkeypatch.setattr("qq_group_cleaner.cards.compiled", lambda *args: Timeout())
    policy = Policy(GROUP, card_rule=CardRule("pattern"))
    with pytest.raises(CleanerError, match="超时"):
        evaluate(policy, member("200001", 1800000000, card="anything"), 1800000000, ACCOUNT)


@pytest.mark.parametrize("first,second", [("23-XXU", "bad"), ("bad", "23-XXU"), (None, "bad")])
def test_disagreeing_snapshot_cards_do_not_authorize_removal(first, second):
    now = 1800000000
    candidate = member("200001", now)
    merged = merge_snapshot_evidence(replace(candidate, card=first), replace(candidate, card=second))
    assert merged.card is None
    assert not evaluate(Policy(GROUP, card_rule=CardRule(PATTERN)), merged, now, ACCOUNT).eligible


async def test_planning_preserves_scoring_and_does_not_query_qq_levels(env):
    policy = configure(env)
    policy = replace(policy, order="综合排序")
    env.box.settings = replace(env.box.settings, groups=(policy,))
    candidate = env.adapter.people["200001"]
    expected = evaluate(replace(policy, card_rule=None), candidate, env.clock(), ACCOUNT)
    plan = await env.service.build_plan(policy, env.adapter)
    assert plan["eligible"] == 1 and len(plan["members"]) == 1
    assert plan["members"][0]["score"] == expected.score
    assert "名片不符合规范" in plan["members"][0]["reason"]
    assert "附加条件" in render_plan(plan)
    assert "名片不合规" in await Commands(env.service).status(policy, env.adapter)
    assert not env.adapter.details
    await Executor(env.service).execute(plan, env.adapter)
    assert env.adapter.kicks == ["200001"]


def card_notice(env, card="23-XXU"):
    return {
        "self_id": ACCOUNT,
        "group_id": GROUP,
        "user_id": "200001",
        "post_type": "notice",
        "notice_type": "group_card",
        "card_new": card,
        "time": int(env.clock()),
    }


@pytest.mark.parametrize("change", ["fresh_card", "notice_with_stale_api", "missing_card"])
async def test_changes_while_waiting_are_rechecked_and_skipped_without_replacement(env, change):
    policy = configure(env)
    plan = await env.service.build_plan(policy, env.adapter)

    async def update(seconds):
        env.clock.hook = None
        if change == "notice_with_stale_api":
            await env.service.observe(card_notice(env))
        else:
            env.adapter.people["200001"] = replace(
                env.adapter.people["200001"], card="23-XXU" if change == "fresh_card" else None
            )

    env.clock.hook = update
    await Executor(env.service).execute(plan, env.adapter)
    assert not env.adapter.kicks and not await env.store.call("history", ACCOUNT, GROUP)


async def test_card_change_after_intent_prevents_send_and_releases_quota(env):
    policy = configure(env)
    plan = await env.service.build_plan(policy, env.adapter)

    async def change(uid):
        await env.service.observe(card_notice(env))

    env.adapter.kick_hook = change
    with pytest.raises(CleanerError, match="名片变化"):
        await Executor(env.service).execute(plan, env.adapter)
    assert not env.adapter.kicks
    assert await env.store.call("quota", ACCOUNT, GROUP, env.clock()) == (0, 0)


async def test_card_notice_protection_survives_reload_without_inventing_activity(env):
    configure(env)
    before = (await env.store.call("merge", ACCOUNT, GROUP, [env.adapter.people["200001"]], env.clock()))[0]
    await env.service.observe(card_notice(env))
    await env.store.close()
    recovered = Store(env.path / "state.sqlite3")
    await recovered.call("open_db")
    try:
        stale = (await recovered.call("merge", ACCOUNT, GROUP, [env.adapter.people["200001"]], env.clock()))[
            0
        ]
        assert stale.card is None and stale.activity == before.activity
        current = replace(env.adapter.people["200001"], card="23-XXU")
        current = (await recovered.call("merge", ACCOUNT, GROUP, [current], env.clock()))[0]
        assert current.card == "23-XXU"
    finally:
        await recovered.close()


async def test_other_groups_ignore_card_notices_when_condition_is_disabled(env):
    await env.service.observe(card_notice(env))
    assert not env.service.event_versions
    assert not await env.store.call("get", f"card-notice:{ACCOUNT}:{GROUP}:200001")


async def test_source_rule_change_invalidates_an_already_selected_plan(env):
    policy = configure(env)
    plan = await env.service.build_plan(policy, env.adapter)
    updated = replace(policy, card_rule=CardRule(r".*"))
    env.box.settings = replace(env.box.settings, groups=(updated,))
    with pytest.raises(CleanerError, match="配置已变更"):
        await Executor(env.service).execute(plan, env.adapter)
    assert not env.adapter.kicks


async def test_out_of_order_card_notice_cannot_override_a_newer_compliant_card(env):
    configure(env)
    await env.store.call("merge", ACCOUNT, GROUP, [env.adapter.people["200001"]], env.clock())
    await env.service.observe(card_notice(env))
    await env.service.observe({**card_notice(env, "bad"), "time": int(env.clock() - 10)})
    current = (await env.store.call("merge", ACCOUNT, GROUP, [env.adapter.people["200001"]], env.clock()))[0]
    assert current.card is None


async def test_an_old_membership_card_notice_does_not_override_a_rejoin(env):
    configure(env)
    await env.store.call("merge", ACCOUNT, GROUP, [env.adapter.people["200001"]], env.clock())
    await env.service.observe(card_notice(env))
    env.clock.now += 1
    await env.service.observe({**card_notice(env), "notice_type": "group_increase", "sub_type": "approve"})
    new = replace(env.adapter.people["200001"], joined=int(env.clock()))
    current = (await env.store.call("merge", ACCOUNT, GROUP, [new], env.clock()))[0]
    assert current.card == "未改名片" and current.membership_known
