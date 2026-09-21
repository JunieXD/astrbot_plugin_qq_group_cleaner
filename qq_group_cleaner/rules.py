"""Pure, explainable decisions. Missing platform facts never become zero scores."""

from __future__ import annotations

from dataclasses import dataclass

from .config import Policy

DAY = 86400


def number(value, *, zero=False) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        text = str(value)
        if not text.isascii() or not text.isdecimal():
            return None
        parsed = int(text)
        return parsed if parsed >= (0 if zero else 1) else None
    except (ValueError, TypeError):
        return None


@dataclass(frozen=True)
class Member:
    user_id: str
    role: str
    joined: int | None
    last_sent: int | None
    group_level: int | None = None
    qq_level: int | None = None
    title: str | None = None
    muted_until: int | None = None
    robot: bool = False
    activity: float = 0
    epoch: int = 0

    @classmethod
    def from_api(cls, raw: dict) -> Member:
        return cls(
            user_id=str(raw.get("user_id", "")),
            role=str(raw.get("role", "")),
            joined=number(raw.get("join_time")),
            last_sent=number(raw.get("last_sent_time")),
            group_level=number(raw.get("level")),
            qq_level=number(raw.get("qq_level")),
            title=raw.get("title") if isinstance(raw.get("title"), str) else None,
            muted_until=number(raw.get("shut_up_timestamp"), zero=True),
            robot=raw.get("is_robot") is True,
        )


@dataclass(frozen=True)
class Decision:
    eligible: bool
    reason: str
    sort_key: tuple = ()


def evaluate(
    policy: Policy,
    member: Member,
    now: float,
    self_id: str,
    protected: bool = False,
    *,
    defer_qq: bool = False,
) -> Decision:
    def keep(reason):
        return Decision(False, reason)

    if member.user_id == self_id or member.role in ("owner", "admin"):
        return keep("群主、管理员或机器人自身")
    if member.role != "member":
        return keep("群身份不明，保留")
    if protected or member.user_id in policy.protected_users:
        return keep("在保护名单或本次入群已有处理记录")
    if member.robot:
        return keep("平台标记的机器人")
    if not member.joined or not member.last_sent:
        return keep("入群时间或发言时间缺失，不能证明长期未发言")
    if max(member.joined, member.last_sent, member.activity) > now + 300:
        return keep("成员时间数据异常")
    if now - member.joined < policy.newcomer_days * DAY:
        return keep("仍在新成员保护期")
    last_active = max(member.last_sent, member.activity, member.joined)
    days = int((now - last_active) / DAY)
    if days < policy.inactive_days:
        return keep(f"最近活动距今 {max(0, days)} 天，未达到 {policy.inactive_days} 天")
    if policy.protect_title and member.title is None:
        return keep("专属头衔信息缺失")
    if policy.protect_title and member.title:
        return keep("有群专属头衔")
    if policy.protect_muted and member.muted_until is None:
        return keep("禁言状态不明")
    if policy.protect_muted and member.muted_until > now:
        return keep("正在被禁言")
    if policy.needs_group_level and member.group_level is None:
        return keep("群等级未知（接口的 0 不作为低等级）")
    if policy.protect_level and member.group_level >= policy.protect_level:
        return keep("达到群等级保护线")
    if policy.max_group_level and member.group_level > policy.max_group_level:
        return keep("群等级高于候选上限")
    if not defer_qq:
        if policy.needs_qq_level and member.qq_level is None:
            return keep("QQ 等级未知，需候选资料核验")
        if policy.max_qq_level and member.qq_level > policy.max_qq_level:
            return keep("QQ 等级高于候选上限")
    values = {
        "inactive": last_active,
        "inactive_bucket": sum(days >= b for b in (90, 180, 365)),
        "group_level": member.group_level,
        "qq_level": member.qq_level,
        "joined": member.joined,
    }
    # Ascending tuple: old last-active first, large inactivity bucket first, low levels first.
    values["inactive_bucket"] = -values["inactive_bucket"]
    fields = [f for f in policy.sort_fields if not (defer_qq and f == "qq_level")]
    key = tuple(values[f] for f in fields) + (int(member.user_id),)
    details = [f"{days} 天未发言"]
    if policy.needs_group_level:
        details.append(f"群等级 {member.group_level}")
    if policy.needs_qq_level and not defer_qq:
        details.append(f"QQ 等级 {member.qq_level}")
    return Decision(True, "，".join(details), key)
