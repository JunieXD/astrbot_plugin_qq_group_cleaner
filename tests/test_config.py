import json
from dataclasses import fields
from pathlib import Path

import pytest

from qq_group_cleaner.config import CleanerError, Pace, Policy, parse_settings


def defaults(items):
    return {
        key: defaults(spec["items"]) if spec["type"] == "object" else spec["default"]
        for key, spec in items.items()
    }


def test_schema_defaults_match_runtime_and_group_template():
    schema = json.loads(Path("_conf_schema.json").read_text(encoding="utf-8"))
    raw = defaults(schema)
    assert not parse_settings(raw).enabled
    row = defaults(schema["groups"]["templates"]["group"]["items"])
    row["group_id"] = "100002"
    row["__template_key"] = "group"
    raw["groups"] = [row]
    settings = parse_settings(raw)
    assert settings.groups == (Policy("100002"),)
    assert settings.pace == Pace()
    assert set(raw["pace"]) == {f.name for f in fields(Pace)}


@pytest.mark.parametrize(
    "patch",
    [
        {"target": 490},
        {"trigger": 400},
        {"inactive_days": 0},
        {"newcomer_days": 0},
        {"trigger": True},
        {"target": "3.0"},
        {"mode": "auto"},
        {"order": "score"},
        {"protected_users": "100003"},
        {"advanced": {"max_qq_level": -1}},
        {"order": "自定义"},
        {"advanced": {"custom_order": ["群等级", "群等级"]}},
    ],
)
def test_invalid_group_config_fails_closed(patch):
    with pytest.raises(CleanerError):
        parse_settings({"groups": [{"group_id": "100002", **patch}]})


def test_invalid_pacing_and_duplicate_groups():
    with pytest.raises(CleanerError):
        parse_settings({"pace": {"min_delay": 100, "max_delay": 30}})
    with pytest.raises(CleanerError):
        parse_settings({"groups": [{"group_id": "100002"}] * 2})


def test_per_group_isolation_and_revision_change():
    settings = parse_settings(
        {
            "groups": [
                {"group_id": "100002", "inactive_days": 60},
                {"group_id": "100003", "inactive_days": 180},
            ]
        }
    )
    assert settings.group("100002").inactive_days == 60
    assert settings.group("100003").inactive_days == 180
    assert settings.revision != parse_settings({}).revision
