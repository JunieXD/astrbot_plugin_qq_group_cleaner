"""A small product configuration, compiled once into immutable policies."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any


class CleanerError(Exception):
    """Safe, actionable text suitable for the administrator (no API payloads)."""


class Deferred(CleanerError):
    def __init__(self, message: str, until: float):
        super().__init__(message)
        self.until = until


MODES = ("仅预览", "确认后清理", "自动清理")
ORDERS = {
    "最久未发言优先": ("inactive",),
    "群等级低优先": ("group_level", "inactive"),
    "QQ等级低优先": ("qq_level", "inactive"),
    "综合排序": (),  # Its dependencies come from the enabled score weights.
}
SORT_FIELDS = {
    "未发言天数": "inactive",
    "未发言时长档位": "inactive_bucket",
    "群等级": "group_level",
    "QQ等级": "qq_level",
    "入群时间": "joined",
}


@dataclass(frozen=True)
class ScoreWeights:
    inactive: int = 70
    group_level: int = 30
    qq_level: int = 0

    @property
    def total(self) -> int:
        return self.inactive + self.group_level + self.qq_level


@dataclass(frozen=True)
class Policy:
    group_id: str
    enabled: bool = True
    mode: str = "仅预览"
    trigger: int = 490
    target: int = 470
    inactive_days: int = 90
    newcomer_days: int = 30
    protect_level: int = 0
    protected_users: tuple[str, ...] = ()
    order: str = "最久未发言优先"
    custom_order: tuple[str, ...] = ()
    max_group_level: int = 0
    max_qq_level: int = 0
    protect_title: bool = True
    protect_muted: bool = True
    bot_qq: str = ""
    score_weights: ScoreWeights = field(default_factory=ScoreWeights)

    @property
    def sort_fields(self) -> tuple[str, ...]:
        if self.order == "综合排序":
            return tuple(
                name
                for name in ("inactive", "group_level", "qq_level")
                if getattr(self.score_weights, name) > 0
            )
        return self.custom_order if self.order == "自定义" else ORDERS[self.order]

    @property
    def needs_group_level(self) -> bool:
        return bool(self.protect_level or self.max_group_level or "group_level" in self.sort_fields)

    @property
    def needs_qq_level(self) -> bool:
        return bool(self.max_qq_level or "qq_level" in self.sort_fields)


@dataclass(frozen=True)
class Pace:
    min_delay: int = 30
    max_delay: int = 90
    batch_size: int = 5
    group_daily_limit: int = 20
    account_daily_limit: int = 30
    batch_minutes: int = 60
    start_hour: int = 9
    end_hour: int = 22


@dataclass(frozen=True)
class Settings:
    enabled: bool = False
    groups: tuple[Policy, ...] = ()
    pace: Pace = field(default_factory=Pace)

    @property
    def revision(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()[:20]

    def group(self, group_id: str) -> Policy:
        for policy in self.groups:
            if policy.group_id == group_id:
                return policy
        raise CleanerError("这个群还没有配置，请在插件配置中添加群号。")


def integer(value: Any, label: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not str(value).isascii() or not str(value).isdecimal():
        raise CleanerError(f"{label}请填写整数。")
    result = int(value)
    if not low <= result <= high:
        raise CleanerError(f"{label}应在 {low}～{high} 之间。")
    return result


def identifier(value: Any, label: str, optional: bool = False) -> str:
    value = str(value).strip()
    if optional and not value:
        return ""
    integer(value, label, 10000, 9999999999999)
    return value


def boolean(raw: dict, key: str, default: bool) -> bool:
    value = raw.get(key, default)
    if not isinstance(value, bool):
        raise CleanerError(f"{key}应使用开关设置。")
    return value


def parse_settings(raw: dict) -> Settings:
    groups = []
    rows = raw.get("groups", [])
    if not isinstance(rows, list) or len(rows) > 30:
        raise CleanerError("群配置应为列表，最多配置 30 个群。")
    for index, row in enumerate(rows, 1):
        try:
            if not isinstance(row, dict):
                raise CleanerError("群配置格式不正确。")
            advanced = row.get("advanced", {})
            if not isinstance(advanced, dict):
                raise CleanerError("更多条件格式不正确。")
            gid = identifier(row.get("group_id", ""), "群号")
            mode = row.get("mode", "仅预览")
            order = row.get("order", "最久未发言优先")
            if mode not in MODES or order not in (*ORDERS, "自定义"):
                raise CleanerError("请选择有效的运行方式和清理顺序。")
            weights_raw = row.get("score_weights", {})
            if not isinstance(weights_raw, dict):
                raise CleanerError("综合排序权重格式不正确。")
            weights = ScoreWeights(
                **{
                    name: integer(weights_raw.get(name, getattr(ScoreWeights(), name)), label, 0, 100)
                    for name, label in (
                        ("inactive", "未发言时长权重"),
                        ("group_level", "群等级权重"),
                        ("qq_level", "QQ等级权重"),
                    )
                }
            )
            if order == "综合排序" and weights.total == 0:
                raise CleanerError("综合排序至少有一项权重大于0。")
            custom = advanced.get("custom_order", [])
            if not isinstance(custom, list) or any(k not in SORT_FIELDS for k in custom):
                raise CleanerError("自定义顺序可填：" + "、".join(SORT_FIELDS))
            if len(set(custom)) != len(custom) or (order == "自定义" and not custom):
                raise CleanerError("自定义顺序不能重复，选择自定义时至少填写一项。")
            protected = row.get("protected_users", [])
            if not isinstance(protected, list) or len(protected) > 2000:
                raise CleanerError("保护名单应为 QQ 号列表，最多 2000 人。")
            policy = Policy(
                group_id=gid,
                enabled=boolean(row, "enabled", True),
                mode=mode,
                trigger=integer(row.get("trigger", 490), "开始清理人数", 2, 100000),
                target=integer(row.get("target", 470), "停止清理人数", 1, 99999),
                inactive_days=integer(row.get("inactive_days", 90), "未发言天数", 7, 3650),
                newcomer_days=integer(row.get("newcomer_days", 30), "新成员保护天数", 7, 3650),
                protect_level=integer(row.get("protect_level", 0), "保护群等级", 0, 999),
                protected_users=tuple(sorted({identifier(u, "保护名单 QQ") for u in protected})),
                order=order,
                custom_order=tuple(SORT_FIELDS[k] for k in custom),
                max_group_level=integer(advanced.get("max_group_level", 0), "候选最高群等级", 0, 999),
                max_qq_level=integer(advanced.get("max_qq_level", 0), "候选最高 QQ 等级", 0, 999),
                protect_title=boolean(advanced, "protect_title", True),
                protect_muted=boolean(advanced, "protect_muted", True),
                bot_qq=identifier(advanced.get("bot_qq", ""), "机器人 QQ", optional=True),
                score_weights=weights,
            )
            if policy.target >= policy.trigger:
                raise CleanerError("停止人数必须小于开始人数。")
            groups.append(policy)
        except CleanerError as exc:
            raise CleanerError(f"第 {index} 个群：{exc}") from exc
    if len({p.group_id for p in groups}) != len(groups):
        raise CleanerError("一个群只能添加一条配置。")
    pace_raw = raw.get("pace", {})
    if not isinstance(pace_raw, dict):
        raise CleanerError("执行节奏格式不正确。")
    bounds = {
        "min_delay": (10, 600),
        "max_delay": (10, 600),
        "batch_size": (1, 10),
        "group_daily_limit": (1, 100),
        "account_daily_limit": (1, 100),
        "batch_minutes": (30, 1440),
        "start_hour": (0, 23),
        "end_hour": (0, 24),
    }
    labels = {
        "min_delay": "最短等待秒数",
        "max_delay": "最长等待秒数",
        "batch_size": "每批人数",
        "group_daily_limit": "每群每日上限",
        "account_daily_limit": "每账号每日上限",
        "batch_minutes": "批次间隔分钟",
        "start_hour": "开始小时",
        "end_hour": "结束小时",
    }
    pace = Pace(
        **{
            k: integer(pace_raw.get(k, getattr(Pace(), k)), labels[k], *limits)
            for k, limits in bounds.items()
        }
    )
    if pace.min_delay > pace.max_delay:
        raise CleanerError("最短等待不能大于最长等待。")
    if pace.start_hour == pace.end_hour:
        raise CleanerError("执行时段起止不能相同；全天请填 0～24。")
    return Settings(boolean(raw, "enabled", False), tuple(groups), pace)
