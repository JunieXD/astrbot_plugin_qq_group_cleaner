from dataclasses import replace

import pytest
from conftest import ACCOUNT, GROUP, Clock, member

from qq_group_cleaner.commands import render_plan
from qq_group_cleaner.config import CleanerError, Policy, ScoreWeights, parse_settings
from qq_group_cleaner.executor import Executor
from qq_group_cleaner.rules import DAY, evaluate


def policy(**changes):
    return Policy(GROUP, order="综合排序", inactive_days=30, **changes)


def test_weighted_ranking_trades_inactivity_against_level_instead_of_lexicographic_order():
    now = Clock()()
    older = member("200001", now, last_sent=int(now - 170 * DAY), group_level=70)
    lower = member("200002", now, last_sent=int(now - 90 * DAY), group_level=10)
    decisions = [evaluate(policy(), m, now, ACCOUNT) for m in (older, lower)]
    assert decisions[0].score == pytest.approx(63.444444)
    assert decisions[1].score == pytest.approx(50.333333)
    assert decisions[0].sort_key < decisions[1].sort_key
    # Both are in the old 90–179 day band, which would always prefer lower group level.
    group_first = policy(score_weights=ScoreWeights(0, 100, 0))
    assert (
        evaluate(group_first, lower, now, ACCOUNT).sort_key
        < evaluate(group_first, older, now, ACCOUNT).sort_key
    )


def test_weights_are_relative_and_scores_have_fixed_bounds():
    now = Clock()()
    person = member("200001", now, qq_level=None)
    assert (
        evaluate(policy(), person, now, ACCOUNT).score
        == evaluate(policy(score_weights=ScoreWeights(7, 3, 0)), person, now, ACCOUNT).score
    )
    inactive_only = policy(score_weights=ScoreWeights(100, 0, 0))
    threshold = replace(person, last_sent=int(now - 30 * DAY))
    saturated = replace(person, joined=int(now - 5000 * DAY), last_sent=int(now - 1000 * DAY))
    assert evaluate(inactive_only, threshold, now, ACCOUNT).score == 0
    assert evaluate(inactive_only, saturated, now, ACCOUNT).score == 100
    high_levels = replace(person, group_level=999, qq_level=999)
    assert evaluate(policy(score_weights=ScoreWeights(0, 50, 50)), high_levels, now, ACCOUNT).score == 0


def test_weighted_score_is_monotonic_and_ties_are_stable():
    now = Clock()()
    original = member("200001", now)
    baseline = evaluate(policy(), original, now, ACCOUNT)
    for improved in (
        replace(original, last_sent=original.last_sent - DAY),
        replace(original, group_level=original.group_level - 1),
    ):
        assert evaluate(policy(), improved, now, ACCOUNT).score > baseline.score
    duplicate = replace(original, user_id="200002")
    other = evaluate(policy(), duplicate, now, ACCOUNT)
    assert baseline.score == other.score and baseline.sort_key < other.sort_key


@pytest.mark.parametrize("weights", [ScoreWeights(), ScoreWeights(0, 100, 0), ScoreWeights(0, 0, 100)])
@pytest.mark.parametrize(
    "changes",
    [
        {"role": "admin"},
        {"last_sent": None},
        {"last_sent": int(Clock()() - DAY)},
        {"joined": int(Clock()() - 10 * DAY)},
        {"title": "贡献者"},
        {"muted_until": int(Clock()() + DAY)},
    ],
)
def test_high_score_cannot_bypass_protections(weights, changes):
    now = Clock()()
    result = evaluate(policy(score_weights=weights), member("200001", now, **changes), now, ACCOUNT)
    assert not result.eligible and result.score is None


def test_zero_weight_allows_missing_data_but_hard_conditions_still_require_it():
    now = Clock()()
    person = member("200001", now, qq_level=None, group_level=None)
    inactive_only = policy(score_weights=ScoreWeights(100, 0, 0))
    assert evaluate(inactive_only, person, now, ACCOUNT).eligible
    assert not evaluate(replace(inactive_only, protect_level=80), person, now, ACCOUNT).eligible
    assert not evaluate(replace(inactive_only, max_group_level=10), person, now, ACCOUNT).eligible
    assert not evaluate(replace(inactive_only, max_qq_level=20), person, now, ACCOUNT).eligible
    assert not evaluate(policy(), person, now, ACCOUNT).eligible  # Group-level weight is active.
    with_group = replace(person, group_level=10)
    assert evaluate(policy(), with_group, now, ACCOUNT).eligible
    with_qq = policy(score_weights=ScoreWeights(60, 30, 10))
    assert not evaluate(with_qq, with_group, now, ACCOUNT).eligible
    preliminary = evaluate(with_qq, with_group, now, ACCOUNT, defer_qq=True)
    assert preliminary.eligible and preliminary.score is None


@pytest.mark.parametrize(
    "weights",
    [
        {"inactive": 0, "group_level": 0, "qq_level": 0},
        {"inactive": -1},
        {"qq_level": 101},
        {"group_level": True},
        {"qq_level": "NaN"},
        {"inactive": 1.5},
        [],
    ],
)
def test_invalid_score_config_is_rejected(weights):
    with pytest.raises(CleanerError, match="权重"):
        parse_settings({"groups": [{"group_id": GROUP, "order": "综合排序", "score_weights": weights}]})


def test_existing_composite_config_gets_accepted_defaults_and_groups_remain_independent():
    settings = parse_settings(
        {
            "groups": [
                {"group_id": GROUP, "order": "综合排序"},
                {
                    "group_id": "100099",
                    "order": "综合排序",
                    "score_weights": {"inactive": 60, "qq_level": 10},
                },
            ]
        }
    )
    assert settings.groups[0].score_weights == ScoreWeights(70, 30, 0)
    assert not settings.groups[0].needs_qq_level
    assert settings.groups[1].needs_qq_level
    assert settings.groups[0].score_weights != settings.groups[1].score_weights
    simple = Policy(GROUP, score_weights=ScoreWeights(0, 0, 100))
    assert not simple.needs_qq_level  # Weights do not change other sorting modes.


async def test_default_weighted_preview_can_rank_all_119_candidates_without_qq_enrichment(env):
    current = replace(env.policy, order="综合排序", inactive_days=30, protect_level=80)
    env.box.settings = replace(env.box.settings, groups=(current,), enabled=False)
    for i in range(116):
        uid = str(300001 + i)
        env.adapter.people[uid] = member(uid, env.clock())
    env.adapter.people = {uid: replace(m, qq_level=None) for uid, m in env.adapter.people.items()}
    plan = await env.service.build_plan(current, env.adapter, manual=True)
    assert plan["eligible"] == 119 and plan["waiting"] == 0
    assert plan["state"] == "preview" and len(plan["members"]) == 5
    assert not env.adapter.details and not env.adapter.kicks
    assert not await env.store.call("get", f"qq-cache:{ACCOUNT}:{GROUP}")
    scores = [item["score"] for item in plan["members"]]
    assert scores == sorted(scores, reverse=True)
    text = render_plan(plan)
    assert "未发言 70 / 群等级 30 / QQ等级 0" in text
    assert "综合得分" in text and "符合条件 119 人" in text


async def test_positive_qq_weight_keeps_lookup_budget_and_incomplete_plan_protection(env):
    current = replace(env.policy, order="综合排序", score_weights=ScoreWeights(60, 30, 10))
    env.box.settings = replace(env.box.settings, groups=(current,))
    for i in range(25):
        uid = str(300001 + i)
        env.adapter.people[uid] = member(uid, env.clock())
    plan = await env.service.build_plan(current, env.adapter)
    assert len(env.adapter.details) == 20 and plan["waiting"] == 8
    assert not plan["members"] and plan["state"] == "preview"
    assert "资料已齐且符合条件" in render_plan(plan)
    assert "不计入下方已核验人数" in render_plan(plan)


async def test_changing_weights_invalidates_an_existing_execution_plan(env):
    current = replace(env.policy, order="综合排序")
    env.box.settings = replace(env.box.settings, groups=(current,))
    plan = await env.service.build_plan(current, env.adapter)
    env.box.settings = replace(
        env.box.settings, groups=(replace(current, score_weights=ScoreWeights(30, 70, 0)),)
    )
    with pytest.raises(CleanerError, match="配置"):
        await Executor(env.service).execute(plan, env.adapter)
    assert not env.adapter.kicks
