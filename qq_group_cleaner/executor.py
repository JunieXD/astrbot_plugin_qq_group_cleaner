"""The only destructive path: delay, revalidate, durable intent, send once, verify."""

from __future__ import annotations

import asyncio
import random
from datetime import datetime, timedelta, timezone

from .config import CleanerError, Deferred
from .platform import read_priority
from .rules import Member, evaluate
from .service import scope

CHINA = timezone(timedelta(hours=8))


def wait_message(reason, until):
    end = datetime.fromtimestamp(until, CHINA).strftime("%m-%d %H:%M:%S")
    return f"正在等待{reason}，最早可于 {end} 继续（北京时间）。"


async def scheduled_wait(store, account, gid, *, first=True, connection_until=0):
    waits = [
        (await store.call("get", "startup_until", 0), "普通启动/重载缓冲"),
        (max(await store.call("get", "cooldown:" + account, 0), connection_until), "连接或异常恢复冷却"),
    ]
    if first:
        waits.append((await store.call("get", "batch:" + scope(account, gid), 0), "批次间隔"))
    return max(waits)


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
        if policy.bot_qq and policy.bot_qq != plan["account"]:
            raise CleanerError("群配置中的机器人绑定已改变，请重新预览。")
        if plan["expires"] <= now or current["state"] not in ("ready", "running"):
            raise CleanerError("计划已过期或停止，请重新预览。")
        if policy.mode == "仅预览" or (policy.mode == "确认后清理" and not current["approver"]):
            raise CleanerError("当前模式或确认状态不允许执行。")
        key = scope(plan["account"], plan["gid"])
        if key in s.memory_pauses or await self.store.call("get", "pause:" + key, ""):
            raise CleanerError("本群已暂停，恢复后请重新预览。")
        if await self.store.call("get", "account-pause:" + plan["account"], ""):
            raise CleanerError("此账号已暂停；请核对历史中的异常记录，再使用恢复命令。")
        if await self.store.call("unresolved_account", plan["account"]):
            raise CleanerError("存在结果不明的操作，需先核对或保留成员。")
        until, reason = await scheduled_wait(self.store, plan["account"], plan["gid"], first=first)
        if now < until:
            raise Deferred(wait_message(reason, until), until)
        if not in_window(now, settings.pace):
            raise Deferred("当前不在执行时段内。", now + 300)
        used_account, used_group = await self.store.call("quota", plan["account"], plan["gid"], now)
        s.journal.record(
            "执行额度核验",
            used_account=used_account,
            used_group=used_group,
            account_limit=settings.pace.account_daily_limit,
            group_limit=settings.pace.group_daily_limit,
        )
        if used_group >= settings.pace.group_daily_limit or used_account >= settings.pace.account_daily_limit:
            raise Deferred("最近 24 小时额度已用完，稍后检查。", now + 3600)
        return policy, settings

    async def execute(self, plan, adapter):
        with self.s.journal.span("批次执行", plan=plan["id"], account=plan["account"], gid=plan["gid"]):
            await self._execute_with_recovery(plan, adapter)

    async def _execute_with_recovery(self, plan, adapter):
        try:
            await self._execute(plan, adapter)
        finally:
            if adapter.recovery_until:
                await self.store.call(
                    "extend_deadline", "cooldown:" + adapter.account, adapter.recovery_until
                )

    async def _execute(self, plan, adapter):
        s = self.s
        if (adapter.account, adapter.platform_id) != (plan["account"], plan["platform"]):
            raise CleanerError("机器人账号绑定已改变，请重新预览。")
        lock = s.account_locks.setdefault(adapter.account, asyncio.Lock())
        async with lock:
            self.check_binding(plan, adapter)
            if adapter.recovery_until > s.clock():
                raise Deferred(wait_message("连接恢复冷却", adapter.recovery_until), adapter.recovery_until)
            policy, settings = await self.gate(plan, first=True)
            await self.store.call("plan_state", plan["id"], "running")
            await self.store.call(
                "set",
                "batch:" + scope(adapter.account, policy.group_id),
                s.clock() + settings.pace.batch_minutes * 60,
            )
            completed = set()
            try:
                for item in plan["members"]:
                    ready_at = max(
                        await self.store.call("get", "write-at:" + adapter.account, 0),
                        s.clock() + random.uniform(settings.pace.min_delay, settings.pace.max_delay),
                    )
                    await self.store.call("set", "write-at:" + adapter.account, ready_at)
                    s.journal.record(
                        "成员操作等待",
                        user=item["member"]["user_id"],
                        reason="随机操作间隔及已有预约",
                        ready_at=ready_at,
                        reason_selected=item["reason"],
                        score=item.get("score"),
                    )
                    await s.sleep(max(0, ready_at - s.clock()))
                    await self.gate(plan)
                    guard = s.router.shared_guard()

                    async def action():
                        with read_priority(1), s.journal.span("成员处理", user=item["member"]["user_id"]):
                            return await self.one(plan, adapter, Member(**item["member"]))

                    async def online():
                        with read_priority(1):
                            return await adapter.online()

                    if guard is None:
                        result = await action()
                    else:
                        try:
                            result = await guard.run(
                                account=adapter.platform_id,
                                online=online,
                                action=action,
                                config={
                                    "recovery_min_seconds": settings.pace.recovery_min_seconds,
                                    "recovery_max_seconds": settings.pace.recovery_max_seconds,
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
                        s.journal.record(
                            "批次提前结束",
                            reason="群人数已达到目标",
                            remaining=[
                                row["member"]["user_id"]
                                for row in plan["members"]
                                if row["member"]["user_id"] not in completed
                            ],
                        )
                        break
                    completed.add(item["member"]["user_id"])
            except BaseException as exc:
                s.journal.record(
                    "批次中断",
                    exception=exc,
                    remaining=[
                        item["member"]["user_id"]
                        for item in plan["members"]
                        if item["member"]["user_id"] not in completed
                    ],
                    reason="停止后续处理，当前成员是否已提交以操作记录为准",
                )
                raise
            finally:
                # Cancellation still completes the DB write via Store.call's shield.
                await self.store.call("plan_state", plan["id"], "finished")

    def check_binding(self, plan, adapter):
        if self.s.router.binding_stamp(adapter) != plan.get("binding"):
            raise CleanerError("机器人连接或接入配置已改变，旧计划失效，请重新预览。")

    def volatile_check(self, plan, adapter, uid, versions, verified_at):
        s = self.s
        s.healthy()
        self.check_binding(plan, adapter)
        if s.monotonic() - verified_at > 30:
            raise CleanerError("资料核验或接口排队耗时过长，本次取消，等待重新检查。")
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
        if policy.protect_muted:
            actors.add("0")  # OneBot uses user_id=0 for all-member mute notices.
        if current_plan["approver"]:
            actors.add(current_plan["approver"])
        versions = {uid: s.event_versions.get((account, gid, uid), 0) for uid in actors}
        if not await adapter.online():
            await self.store.call(
                "extend_deadline",
                "cooldown:" + account,
                s.clock()
                + random.uniform(settings.pace.recovery_min_seconds, settings.pace.recovery_max_seconds),
            )
            raise CleanerError("QQ 当前离线，已停止执行并进入恢复冷却。")
        await adapter.identity()
        self.check_binding(plan, adapter)
        verified_at = s.monotonic()
        if policy.mode == "确认后清理":
            if not await s.is_admin(adapter, gid, current_plan["approver"]):
                raise CleanerError("确认人已不再是本群管理员，原计划失效。")
        if not await s.is_admin(adapter, gid, account):
            raise CleanerError("机器人不是本群管理员，已停止执行。")
        fresh = await adapter.member(gid, original.user_id)
        fresh = (await self.store.call("merge", account, gid, [fresh], s.clock()))[0]
        protected, attempted = await self.store.call("exclusions", account, gid, s.clock())
        decision = evaluate(
            policy,
            fresh,
            s.clock(),
            account,
            fresh.user_id in protected or (fresh.user_id, fresh.epoch) in attempted,
        )
        s.journal.record(
            "执行前复核",
            original=original.__dict__,
            current=fresh.__dict__,
            eligible=decision.eligible,
            reason=decision.reason,
            score=decision.score,
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
            s.journal.record(
                "成员跳过", reason=decision.reason if not decision.eligible else "入群身份已变化"
            )
            return "skip"
        info = await adapter.group(gid)
        await s.check_speaking(policy, account, info)
        if policy.trigger > info.capacity:
            raise CleanerError("群容量发生变化，请调整人数配置。")
        if info.count <= policy.target:
            await self.store.call("set", "cycle:" + scope(account, gid), {})
            s.journal.record("成员未处理", reason="群人数已达到目标", count=info.count, target=policy.target)
            return "stop"
        if not await s.cycle(policy, account, info.count, plan["revision"], activate=False):
            raise CleanerError("清理周期已失效，请重新预览。")
        await self.gate(plan)
        try:
            self.volatile_check(plan, adapter, fresh.user_id, versions, verified_at)
        except CleanerError as exc:
            s.journal.record("成员跳过", exception=exc, reason=str(exc))
            return "skip"
        s.journal.record(
            "准备提交",
            user=fresh.user_id,
            reason=decision.reason,
            count=info.count,
            target=policy.target,
            score=decision.score,
            reject_add_request=False,
        )
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
        s.journal.record("写前意图已保存", operation=operation_id, user=fresh.user_id)

        def before_send():
            nonlocal sent
            self.volatile_check(plan, adapter, fresh.user_id, versions, verified_at)
            sent = True

        try:
            await adapter.kick(gid, fresh.user_id, before_send=before_send)
        except BaseException as exc:
            s.journal.record(
                "移出请求中断",
                exception=exc,
                operation=operation_id,
                transport_entered=sent,
                reason="不重复提交" if sent else "尚未发送，取消写前意图",
            )
            if not sent:
                await self.store.call("cancel_intent", operation_id, s.clock())
                s.journal.record("写前意图已取消", operation=operation_id, reason="尚未发送，已释放本次额度")
            else:
                await self.store.call(
                    "result", operation_id, "unknown", "请求中断或返回异常，不重复提交", s.clock()
                )
                await self.store.call(
                    "set",
                    "account-pause:" + account,
                    {"gid": gid, "reason": "管理请求中断或返回异常，需人工核对"},
                )
                s.journal.record(
                    "账号已暂停",
                    operation=operation_id,
                    state="unknown",
                    reason="请求进入传输后异常，结果未知，保留额度并等待人工核对",
                )
            raise
        s.journal.record(
            "移出接口已返回", operation=operation_id, reason="接口成功返回，仍需核验实际离群结果"
        )
        state = await self.verify(adapter, operation_id)
        if state == "unknown":
            await self.store.call(
                "set", "account-pause:" + account, {"gid": gid, "reason": "移出结果不明，需核对或保留"}
            )
            s.journal.record(
                "账号已暂停",
                operation=operation_id,
                state=state,
                reason="不能证实离群，等待核对或保留，不重发",
            )
            raise CleanerError("移出结果不明，已暂停账号；请查看历史，核对后再恢复。")
        return state

    async def verify(self, adapter, operation_id):
        with self.s.journal.span("操作核验", operation=operation_id, account=adapter.account):
            return await self._verify(adapter, operation_id)

    async def _verify(self, adapter, operation_id):
        s = self.s
        await s.sleep(10)
        op = await self.store.call("operation", operation_id)
        if op["state"] == "confirmed_removed":
            s.journal.record(
                "操作核验结果",
                state=op["state"],
                reason="已收到匹配的本账号移出通知",
                user=op["uid"],
                gid=op["gid"],
                plan=op["plan"],
            )
            return op["state"]
        try:
            _, members = await adapter.snapshot(op["gid"])
            if op["uid"] not in {m.user_id for m in members}:
                state = await self.store.call(
                    "result",
                    operation_id,
                    "observed_absent",
                    "两次一致名单与群人数证实已不在群，无法归因于本插件",
                    s.clock(),
                )
                s.journal.record(
                    "操作核验结果",
                    state=state,
                    user=op["uid"],
                    gid=op["gid"],
                    plan=op["plan"],
                    reason="两次一致名单证实不在群，无法归因于本插件",
                )
                return state
        except CleanerError as exc:
            s.journal.record("离群核验查询失败", exception=exc, user=op["uid"], gid=op["gid"])
        state = await self.store.call(
            "result", operation_id, "unknown", "尚不能证实成员已离群，不重复提交", s.clock()
        )
        s.journal.record(
            "操作核验结果",
            state=state,
            user=op["uid"],
            gid=op["gid"],
            plan=op["plan"],
            reason="尚不能证实成员已离群，不重复提交",
        )
        return state
