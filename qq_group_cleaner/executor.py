"""The only destructive path: delay, revalidate, durable intent, send once, verify."""

from __future__ import annotations

import asyncio
import random
from datetime import datetime, timedelta, timezone

from .config import CleanerError, Deferred
from .rules import Member, evaluate
from .service import scope

CHINA = timezone(timedelta(hours=8))


def in_window(now, pace):
    hour = datetime.fromtimestamp(now, CHINA).hour
    if pace.start_hour < pace.end_hour:
        return pace.start_hour <= hour < pace.end_hour
    return hour >= pace.start_hour or hour < pace.end_hour


class Executor:
    def __init__(self, service):
        self.s = service
        self.store = service.store

    async def gate(self, plan, *, first=False):
        s = self.s
        s.healthy()
        settings = s.settings()
        policy = settings.group(plan["gid"])
        now = s.clock()
        current = await self.store.call("plan", plan["id"])
        if not settings.enabled or not policy.enabled or plan["revision"] != settings.revision:
            raise CleanerError("插件未启用或配置已变更，请重新预览。")
        if plan["expires"] <= now or current["state"] not in ("ready", "running"):
            raise CleanerError("计划已过期或停止，请重新预览。")
        if policy.mode == "仅预览" or (policy.mode == "确认后清理" and not current["approver"]):
            raise CleanerError("当前模式或确认状态不允许执行。")
        key = scope(plan["account"], plan["gid"])
        if key in s.memory_pauses or await self.store.call("get", "pause:" + key, ""):
            raise CleanerError("本群已暂停，恢复后请重新预览。")
        if await self.store.call("get", "account-pause:" + plan["account"], ""):
            raise CleanerError("此账号已暂停；请核对历史中的异常记录，再使用恢复命令。")
        if await self.store.call("unresolved", plan["account"], plan["gid"]):
            raise CleanerError("存在结果不明的操作，需先核对或保留成员。")
        until = max(
            await self.store.call("get", "startup_until", 0),
            await self.store.call("get", "cooldown:" + plan["account"], 0),
        )
        if first:
            until = max(until, await self.store.call("get", "batch:" + key, 0))
        if now < until:
            raise Deferred("正在等待启动冷却或下一批间隔。", until)
        if not in_window(now, settings.pace):
            raise Deferred("当前不在执行时段内。", now + 300)
        used_account, used_group = await self.store.call("quota", plan["account"], plan["gid"], now)
        if used_group >= settings.pace.group_daily_limit or used_account >= settings.pace.account_daily_limit:
            raise Deferred("最近 24 小时额度已用完，稍后检查。", now + 3600)
        return policy, settings

    async def execute(self, plan, adapter):
        s = self.s
        if (adapter.account, adapter.platform_id) != (plan["account"], plan["platform"]):
            raise CleanerError("机器人账号绑定已改变，请重新预览。")
        lock = s.account_locks.setdefault(adapter.account, asyncio.Lock())
        async with lock:
            policy, settings = await self.gate(plan, first=True)
            await self.store.call("plan_state", plan["id"], "running")
            await self.store.call(
                "set",
                "batch:" + scope(adapter.account, policy.group_id),
                s.clock() + settings.pace.batch_minutes * 60,
            )
            try:
                for item in plan["members"]:
                    ready_at = max(
                        await self.store.call("get", "write-at:" + adapter.account, 0),
                        s.clock() + random.uniform(settings.pace.min_delay, settings.pace.max_delay),
                    )
                    await self.store.call("set", "write-at:" + adapter.account, ready_at)
                    await s.sleep(max(0, ready_at - s.clock()))
                    await self.gate(plan)
                    guard = s.router.shared_guard()

                    async def action():
                        return await self.one(plan, adapter, Member(**item["member"]))

                    if guard is None:
                        result = await action()
                    else:
                        try:
                            result = await guard.run(
                                account=adapter.platform_id,
                                online=adapter.online,
                                action=action,
                                config={
                                    "recovery_min_seconds": 300,
                                    "recovery_max_seconds": 900,
                                    "failure_threshold": 1,
                                    "failure_cooldown_seconds": 3600,
                                },
                                delay=(0, 0),
                                gap=(settings.pace.min_delay, settings.pace.max_delay),
                                key=None,  # Our SQLite intent is the durable source of truth.
                            )
                        except guard.deferred_error as exc:
                            raise Deferred("共享操作队列正在冷却，稍后重新检查。", s.clock() + 3600) from exc
                    if result == "stop":
                        break
            finally:
                # Cancellation still completes the DB write via Store.call's shield.
                await self.store.call("plan_state", plan["id"], "finished")

    def volatile_check(self, plan, adapter, uid, versions):
        s = self.s
        s.healthy()
        if s.settings().revision != plan["revision"] or s.clock() >= plan["expires"]:
            raise CleanerError("等待期间配置或计划有效期发生变化，已取消。")
        if scope(adapter.account, plan["gid"]) in s.memory_pauses:
            raise CleanerError("管理员已暂停，已取消。")
        if not in_window(s.clock(), s.settings().pace):
            raise CleanerError("执行时段已结束，已取消。")
        for actor in versions:
            key = (adapter.account, plan["gid"], actor)
            if s.event_versions.get(key, 0) != versions[actor]:
                raise CleanerError("核验期间出现发言或身份变化，本次跳过。")

    async def one(self, plan, adapter, original):
        s, gid, account = self.s, plan["gid"], adapter.account
        policy, settings = await self.gate(plan)
        current_plan = await self.store.call("plan", plan["id"])
        actors = {original.user_id, account}
        if current_plan["approver"]:
            actors.add(current_plan["approver"])
        versions = {uid: s.event_versions.get((account, gid, uid), 0) for uid in actors}
        if not await adapter.online():
            await self.store.call("set", "cooldown:" + account, s.clock() + random.uniform(300, 900))
            raise CleanerError("QQ 当前离线，已停止执行并进入恢复冷却。")
        await adapter.identity()
        if policy.mode == "确认后清理":
            approver = await adapter.member(gid, current_plan["approver"])
            if approver.role not in ("owner", "admin"):
                raise CleanerError("确认人已不再是本群管理员，原计划失效。")
        bot = await adapter.member(gid, account)
        if bot.role not in ("owner", "admin"):
            raise CleanerError("机器人不是本群管理员，已停止执行。")
        fresh = await adapter.member(gid, original.user_id)
        fresh = (await self.store.call("merge", account, gid, [fresh]))[0]
        protected, attempted = await self.store.call("exclusions", account, gid, s.clock())
        decision = evaluate(
            policy,
            fresh,
            s.clock(),
            account,
            fresh.user_id in protected or (fresh.user_id, fresh.epoch) in attempted,
        )
        if fresh.epoch != original.epoch or fresh.joined != original.joined or not decision.eligible:
            await self.store.call(
                "audit",
                s.clock(),
                account,
                gid,
                "跳过",
                {
                    "plan": plan["id"],
                    "user": fresh.user_id,
                    "reason": decision.reason if not decision.eligible else "入群身份已变化",
                },
            )
            return "skip"
        info = await adapter.group(gid)
        info.check_speaking(policy.protect_muted)
        if policy.trigger > info.capacity:
            raise CleanerError("群容量发生变化，请调整人数配置。")
        if info.count <= policy.target:
            await self.store.call("set", "cycle:" + scope(account, gid), {})
            return "stop"
        if not await s.cycle(policy, account, info.count, plan["revision"], activate=False):
            raise CleanerError("清理周期已失效，请重新预览。")
        await self.gate(plan)
        try:
            self.volatile_check(plan, adapter, fresh.user_id, versions)
        except CleanerError:
            return "skip"
        s.journal.record("准备提交", plan["id"])
        s.healthy()
        operation_id = await self.store.call(
            "reserve",
            plan["id"],
            fresh,
            s.clock(),
            settings.pace.group_daily_limit,
            settings.pace.account_daily_limit,
        )
        sent = False

        def before_send():
            nonlocal sent
            self.volatile_check(plan, adapter, fresh.user_id, versions)
            sent = True

        try:
            await adapter.kick(gid, fresh.user_id, before_send=before_send)
        except BaseException:
            if not sent:
                await self.store.call("cancel_intent", operation_id, s.clock())
            else:
                await self.store.call(
                    "result", operation_id, "unknown", "请求中断或返回异常，不重复提交", s.clock()
                )
                await self.store.call("set", "account-pause:" + account, "管理请求中断或返回异常，需人工核对")
            raise
        state = await self.verify(adapter, operation_id)
        if state == "unknown":
            await self.store.call("set", "account-pause:" + account, "移出结果不明，需核对或保留")
            raise CleanerError("移出结果不明，已暂停账号；请查看历史，核对后再恢复。")
        s.journal.record("操作核验完成", f"{plan['id']} {state}")
        return state

    async def verify(self, adapter, operation_id):
        s = self.s
        await s.sleep(10)
        op = await self.store.call("operation", operation_id)
        if op["state"] == "confirmed_removed":
            return op["state"]
        try:
            _, members = await adapter.snapshot(op["gid"])
            if op["uid"] not in {m.user_id for m in members}:
                return await self.store.call(
                    "result",
                    operation_id,
                    "observed_absent",
                    "两次一致名单与群人数证实已不在群，无法归因于本插件",
                    s.clock(),
                )
        except CleanerError:
            pass
        return await self.store.call(
            "result", operation_id, "unknown", "尚不能证实成员已离群，不重复提交", s.clock()
        )
