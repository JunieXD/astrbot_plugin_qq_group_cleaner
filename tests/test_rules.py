from dataclasses import replace

import pytest
from conftest import ACCOUNT, GROUP, Clock, member

from qq_group_cleaner.config import Policy
from qq_group_cleaner.rules import DAY, Member, evaluate


def test_inactivity_is_required_even_with_low_levels():
    now = Clock()()
    decision = evaluate(Policy(GROUP), member("200001", now, last_sent=int(now - DAY)), now, ACCOUNT)
    assert not decision.eligible
    assert "未达到" in decision.reason


@pytest.mark.parametrize(
    "changes",
    [
        {"role": "owner"},
        {"role": "admin"},
        {"role": ""},
        {"user_id": ACCOUNT},
        {"joined": None},
        {"last_sent": None},
        {"title": None},
        {"title": "贡献者"},
        {"muted_until": None},
        {"muted_until": 9999999999},
        {"robot": True},
        {"last_sent": 9999999999},
        {"joined": 9999999999},
    ],
)
def test_protections(changes):
    now = Clock()()
    assert not evaluate(Policy(GROUP), member("200001", now, **changes), now, ACCOUNT).eligible


def test_recent_activity_newcomer_and_manual_exemption():
    now = Clock()()
    policy = Policy(GROUP)
    for candidate in [member("200001", now, activity=now), member("200001", now, joined=int(now - 10 * DAY))]:
        assert not evaluate(policy, candidate, now, ACCOUNT).eligible
    assert not evaluate(policy, member("200001", now), now, ACCOUNT, protected=True).eligible


def test_boundaries_and_group_level_protection():
    now = Clock()()
    policy = Policy(GROUP, protect_level=5)
    candidate = member("200001", now, last_sent=int(now - 90 * DAY), joined=int(now - 90 * DAY))
    assert evaluate(policy, candidate, now, ACCOUNT).eligible
    assert not evaluate(policy, replace(candidate, group_level=5), now, ACCOUNT).eligible
    assert not evaluate(policy, replace(candidate, group_level=None), now, ACCOUNT).eligible


@pytest.mark.parametrize("value", [0, "0", None, "", -1, "NaN", True, 1.5])
def test_invalid_levels_are_unknown(value):
    m = Member.from_api({"user_id": "200001", "level": value, "qq_level": value})
    assert m.group_level is None and m.qq_level is None


def test_unknown_unneeded_levels_do_not_prevent_inactivity_only_rules():
    now = Clock()()
    m = member("200001", now, group_level=None, qq_level=None)
    assert evaluate(Policy(GROUP), m, now, ACCOUNT).eligible
    assert not evaluate(Policy(GROUP, order="QQ等级低优先"), m, now, ACCOUNT).eligible


def test_ranking_changes_with_order_and_is_stable():
    now = Clock()()
    old = member("200001", now, last_sent=int(now - 200 * DAY), group_level=8)
    low = member("200002", now, group_level=1)

    def rank(policy):
        return sorted([low, old], key=lambda m: evaluate(policy, m, now, ACCOUNT).sort_key)

    assert rank(Policy(GROUP))[0] == old
    assert rank(Policy(GROUP, order="群等级低优先"))[0] == low
    custom = Policy(GROUP, order="自定义", custom_order=("inactive_bucket", "group_level"))
    assert rank(custom)[0] == old


def test_candidate_limits_are_conjunctive():
    now = Clock()()
    policy = Policy(GROUP, max_group_level=3, max_qq_level=10)
    m = member("200001", now)
    assert evaluate(policy, m, now, ACCOUNT).eligible
    assert not evaluate(policy, replace(m, qq_level=11), now, ACCOUNT).eligible
    assert not evaluate(policy, replace(m, group_level=4), now, ACCOUNT).eligible
