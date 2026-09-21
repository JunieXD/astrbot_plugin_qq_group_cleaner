"""Private administrator commands, separated from AstrBot decorators."""

from __future__ import annotations

from datetime import datetime

from .config import identifier, integer
from .executor import CHINA, Executor
from .platform import read_priority
from .rules import evaluate
from .service import scope

HELP = """群清理：先在插件配置中添加群，默认仅预览。
私聊使用（把“群号”换成实际群号）：
/群清理 预览 群号
/群清理 确认 群号 确认码
/群清理 状态 群号
/群清理 暂停 群号
/群清理 恢复 群号
更多：解释 群号 QQ号；保护 群号 QQ号 [天数，0为永久]；取消保护 群号 QQ号；历史 群号；核对 群号；保留 群号 QQ号。
只有该群当前的群主和管理员可以查看、操作；观察模式不会移出成员。"""

STATES = {
    "submitted": "等待核验",
    "unknown": "结果不明",
    "confirmed_removed": "已证实由本账号移出",
    "observed_absent": "已确认不在群（无法归因）",
    "reviewed_retained": "人工保留，不再重复提交",
}


def date_text(stamp):
    return datetime.fromtimestamp(stamp, CHINA).strftime("%m-%d %H:%M")


def render_plan(plan):
    if not plan:
        return "人数未达到开始清理线。"
    lines = [f"群 {plan['gid']}：{plan['count']}/{plan['capacity']} 人，目标 {plan['target']} 人。"]
    if not plan["triggered"]:
        lines.append("尚未达到开始线，本次不安排清理。")
    if plan["waiting"]:
        lines.append(f"还有 {plan['waiting']} 人的 QQ 等级待补查。本轮不安排清理，后续检查会继续补全。")
    lines.append(f"符合条件 {plan['eligible']} 人，本批名单 {len(plan['members'])} 人：")
    for item in plan["members"]:
        lines.append(f"• {item['member']['user_id']}：{item['reason']}")
    if plan["reasons"]:
        lines.append("保留原因（人数最多的三项）：")
        lines.extend(
            f"• {reason}：{count} 人"
            for reason, count in sorted(plan["reasons"].items(), key=lambda p: -p[1])[:3]
        )
    if plan["state"] == "pending":
        lines.append(
            f"确认本批：/群清理 确认 {plan['gid']} {plan['id']}\n有效至 {date_text(plan['expires'])}，只执行以上名单。"
        )
    elif plan["state"] == "ready":
        lines.append("已进入执行队列；会遵守随机等待、时段与每日上限。")
    else:
        lines.append("这是只读预览。需要执行时，在插件配置中启用，并选择确认后清理或自动清理。")
    return "\n".join(lines)


class Commands:
    def __init__(self, service):
        self.s = service

    async def run(self, text, actor, platform_id, self_id):
        s = self.s
        parts = text.strip().lstrip("/").split()
        if parts and parts[0] in ("群清理", "qgclean"):
            parts.pop(0)
        if not parts or parts == ["帮助"]:
            return HELP
        action = parts[0]
        shapes = {
            "状态": (2, 2),
            "预览": (2, 2),
            "确认": (3, 3),
            "暂停": (2, 2),
            "恢复": (2, 2),
            "解释": (3, 3),
            "保护": (3, 4),
            "取消保护": (3, 3),
            "历史": (2, 2),
            "核对": (2, 2),
            "保留": (3, 3),
        }
        if (
            len(text) > 512
            or action not in shapes
            or not shapes[action][0] <= len(parts) <= shapes[action][1]
        ):
            return HELP
        gid = identifier(parts[1], "群号")
        policy = s.settings().group(gid)
        # Bound command-triggered lookups, including unauthorized attempts.
        key = (platform_id, actor)
        if s.command_next.get(key, 0) > s.clock():
            return "请稍等几秒再操作。"
        if len(s.command_next) > 2000:
            s.command_next = {k: v for k, v in s.command_next.items() if v > s.clock()}
        s.command_next[key] = s.clock() + 5
        adapter = await s.authorize(policy, actor, platform_id, self_id)
        account = adapter.account
        if action == "暂停":
            await s.pause(account, gid, actor)
            return "已暂停本群，尚未提交的操作会停止。恢复后会重新生成计划。"
        if action in ("保护", "取消保护"):
            uid = identifier(parts[2], "QQ号")
            version_key = (account, gid, uid)
            s.event_versions[version_key] = s.event_versions.get(version_key, 0) + 1
            if action == "保护":
                days = integer(parts[3] if len(parts) == 4 else 0, "保护天数", 0, 3650)
                await s.store.call(
                    "protect", account, gid, uid, s.clock() + days * 86400 if days else 0, actor, s.clock()
                )
                return f"已保护 {uid}，" + (f"有效 {days} 天。" if days else "永久有效。")
            await s.store.call("unprotect", account, gid, uid, actor, s.clock())
            return "已取消命令添加的保护，仍保留 7 天缓冲期。配置中的保护名单需在配置页修改。"
        if action == "状态":
            return await self.status(policy, adapter)
        if action == "历史":
            history = await s.store.call("history", account, gid)
            return (
                "\n".join(
                    f"{date_text(row['submitted'])} {row['uid']}：{STATES.get(row['state'], row['state'])}"
                    for row in history
                )
                or "本群还没有提交过清理操作。"
            )
        if s.group_lock(gid).locked():
            return "本群正在检查或执行，请稍后再试。暂停和保护命令仍可使用。"
        async with s.group_lock(gid):
            if action == "预览":
                preview_key = "preview-at:" + scope(account, gid)
                if await s.store.call("get", preview_key, 0) > s.clock():
                    return "刚检查过这个群，请至少间隔 1 分钟后再预览。"
                await s.store.call("set", preview_key, s.clock() + 60)
                plan = await s.build_plan(policy, adapter, manual=True)
                return render_plan(plan)
            if action == "确认":
                await s.confirm(parts[2], policy, adapter, actor)
                return (
                    "已确认这批名单。机器人会在允许的时段、额度和等待条件满足后执行；名单变化只跳过，不补人。"
                )
            if action == "恢复":
                await s.resume(account, gid, actor)
                return "已恢复。先等待 5～15 分钟，再按当前配置重新检查；旧计划已作废。"
            if action == "解释":
                uid = identifier(parts[2], "QQ号")
                member = await adapter.member(gid, uid)
                member = (await s.store.call("merge", account, gid, [member], s.clock()))[0]
                protected, attempted = await s.store.call("exclusions", account, gid, s.clock())
                decision = evaluate(
                    policy, member, s.clock(), account, uid in protected or (uid, member.epoch) in attempted
                )
                return (
                    ("符合候选条件：" if decision.eligible else "本次保留：")
                    + decision.reason
                    + "。人数、排序和额度仍决定是否进入本批名单。"
                )
            if action == "核对":
                unresolved = await s.store.call("unresolved", account, gid)
                if not unresolved:
                    return "本群没有结果不明的操作。"
                results = []
                for op in unresolved[:10]:
                    with read_priority(2):
                        state = await Executor(s).verify(adapter, op["id"])
                    results.append(f"{op['uid']}：{STATES[state]}")
                return "\n".join(results) + "\n核对不会重发移出请求。确认处理完毕后使用恢复命令。"
            if action == "保留":
                uid = identifier(parts[2], "QQ号")
                records = await s.store.call("unresolved", account, gid)
                matched = [op for op in records if op["uid"] == uid]
                if not matched:
                    return "这个成员没有待核对的清理操作；需要普通保护请使用保护命令。"
                for op in matched:
                    await s.store.call(
                        "result", op["id"], "reviewed_retained", f"管理员 {actor} 决定保留，不重发", s.clock()
                    )
                await s.store.call("protect", account, gid, uid, 0, actor, s.clock())
                return "已记为人工保留并加入永久保护名单，不会重发请求。处理完其他异常后可恢复本群。"
        return HELP

    async def status(self, policy, adapter):
        s, account, gid = self.s, adapter.account, policy.group_id
        settings = s.settings()
        key = scope(account, gid)
        status = await s.store.call("get", "status:" + key, {})
        group_pause = await s.store.call("get", "pause:" + key, "")
        account_pause = await s.store.call("get", "account-pause:" + account, "")
        if isinstance(account_pause, dict):
            account_pause = (
                account_pause["reason"] if account_pause.get("gid") == gid else "账号因其他群的操作暂停"
            )
        check_error = await s.store.call("get", "check-error:" + gid, "")
        unresolved = await s.store.call("unresolved", account, gid)
        used_account, used_group = await s.store.call("quota", account, gid, s.clock())
        waiting_until = max(
            await s.store.call("get", "startup_until", 0),
            await s.store.call("get", "batch:" + key, 0),
            await s.store.call("get", "cooldown:" + account, 0),
        )
        latest = await s.store.call("latest", account, gid)
        lines = [
            f"群 {gid} · {policy.mode} · {'已启用' if settings.enabled and policy.enabled else '未启用'}",
            f"达到 {policy.trigger} 人开始，降到 {policy.target} 人停止。",
            f"最近检查：{date_text(status['at']) if status else '尚未检查'}；人数：{status.get('count', '未知')}。",
            f"{policy.inactive_days} 天未发言；新成员保护 {policy.newcomer_days} 天；{policy.order}。",
            f"最近24小时已提交：本群 {used_group}/{settings.pace.group_daily_limit}，账号 {used_account}/{settings.pace.account_daily_limit}。",
            f"本群待核对：{len(unresolved)} 条。",
        ]
        if latest:
            lines.append(
                f"最近计划：{latest['id']}，候选 {latest['eligible']} 人，待补资料 {latest['waiting']} 人。"
            )
        if waiting_until > s.clock():
            lines.append(f"最早下一批时间：{date_text(waiting_until)}（仍需满足执行时段和额度）。")
        if s.failure or group_pause or account_pause or check_error:
            lines.append("暂停原因：" + (s.failure or group_pause or account_pause or check_error))
        else:
            lines.append("状态：" + status.get("text", "等待检查"))
        lines.append("与其他插件共享操作队列。" if s.router.shared_guard() else "操作节奏仅覆盖本插件。")
        return "\n".join(lines)
