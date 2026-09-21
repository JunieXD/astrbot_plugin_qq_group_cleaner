"""Planning and lifecycle. Every destructive action goes through Executor."""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import secrets
import time
from collections import Counter
from dataclasses import asdict, replace

from .config import CleanerError, Deferred, Policy
from .platform import PlatformError, read_priority
from .rules import evaluate, number


def scope(account, gid):
    return f"{account}:{gid}"


class CleanerService:
    def __init__(
        self,
        settings,
        store,
        router,
        journal,
        *,
        clock=time.time,
        sleep=asyncio.sleep,
        monotonic=time.monotonic,
    ):
        self.settings = settings
        self.store = store
        self.router = router
        self.journal = journal
        self.store.journal = journal
        self.clock = clock
        self.sleep = sleep
        self.monotonic = monotonic
        self.stopped = False
        self.failure = ""
        self.group_locks = {}
        self.account_locks = {}
        self.event_versions = {}
        self.memory_pauses = set()
        self.task = None
        self.jobs = set()
        self.workers = {}
        self.wake = asyncio.Event()
        self.next_check = {}
        self.command_next = {}
        self.last_clock = (self.clock(), self.monotonic())

    def group_lock(self, gid):
        return self.group_locks.setdefault(gid, asyncio.Lock())

    def healthy(self):
        if self.stopped or self.failure or not self.store.healthy:
            raise CleanerError(self.failure or "插件已停止或存储异常，请检查日志后重载。")
        wall, monotonic = self.last_clock
        current, current_mono = self.clock(), self.monotonic()
        if abs((current - wall) - (current_mono - monotonic)) > 120:
            self.failure = "系统时间发生跳变，已停止清理，请校准时间后重载插件。"
            raise CleanerError(self.failure)
        self.last_clock = (current, current_mono)
        self.journal.check()

    async def start(self):
        now = self.clock()
        previous = await self.store.call("get", "last_wall", now)
        if now < previous - 120:
            self.failure = "系统时间早于上次运行，请校准时间后重载插件。"
        # Restart cannot shorten a previously reserved delay.
        pace = self.settings().pace
        await self.store.call(
            "extend_deadline",
            "startup_until",
            now + random.uniform(pace.startup_min_seconds, pace.startup_max_seconds),
        )
        self.task = asyncio.create_task(self.loop(), name="qq-cleaner-scheduler")
        self.journal.record(
            "服务启动",
            revision=self.settings().revision,
            settings=asdict(self.settings()),
            failure=self.failure,
        )

    async def stop(self):
        self.stopped = True
        self.wake.set()
        tasks = set(self.jobs)
        tasks.update(self.workers.values())
        if self.task:
            tasks.add(self.task)
        tasks.discard(asyncio.current_task())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.router.persist_recovery()
        self.journal.record("服务停止", reason="卸载、重载或关闭，已等待活动任务退出")

    async def authorize(self, policy, actor, event_platform, event_account):
        revision = self.settings().revision
        with read_priority(2):
            adapter = await self.router.resolve(policy)
            if adapter.platform_id != event_platform or adapter.account != event_account:
                raise CleanerError("请私聊负责这个群的机器人账号。")
            allowed = await self.is_admin(adapter, policy.group_id, actor)
        if self.settings().revision != revision or self.settings().group(policy.group_id) != policy:
            raise CleanerError("授权期间配置已改变，请按当前配置重新操作。")
        if not allowed:
            raise CleanerError("只有这个群当前的群主或管理员可以操作和查看名单。")
        return adapter

    async def is_admin(self, adapter, gid, uid):
        member = await adapter.member(gid, uid)
        merged = (await self.store.call("merge", adapter.account, gid, [member], self.clock()))[0]
        notice = await self.store.call("get", f"role-notice:{adapter.account}:{gid}:{uid}", {})
        # Positive notices protect targets, but never grant command permissions by themselves.
        return (
            member.role in ("owner", "admin")
            and merged.membership_known
            and notice.get("role") not in ("member", "absent")
        )

    async def cycle(self, policy, account, count, revision, activate=True):
        key = "cycle:" + scope(account, policy.group_id)
        state = await self.store.call("get", key, {})
        active = state.get("revision") == revision and state.get("expires", 0) > self.clock()
        if count <= policy.target:
            await self.store.call("set", key, {})
            return False
        if count >= policy.trigger and activate:
            if not active:
                await self.store.call("set", key, {"revision": revision, "expires": self.clock() + 7 * 86400})
            return True
        return active

    async def build_plan(self, policy: Policy, adapter, *, manual=False):
        plan_id = secrets.token_hex(6)
        with self.journal.span(
            "计划检查", plan=plan_id, account=adapter.account, gid=policy.group_id, manual=manual
        ):
            return await self._build_plan(policy, adapter, manual=manual, plan_id=plan_id)

    async def _build_plan(self, policy, adapter, *, manual, plan_id):
        self.healthy()
        settings = self.settings()
        gid, account = policy.group_id, adapter.account
        if settings.group(gid) != policy or (policy.bot_qq and policy.bot_qq != account):
            raise CleanerError("检查前群配置或机器人绑定已改变，请重新检查。")
        if not await adapter.online():
            raise PlatformError("QQ 当前离线，暂缓检查。")
        binding = self.router.binding_stamp(adapter)
        info = await adapter.group(gid)
        self.journal.record(
            "群资料",
            count=info.count,
            capacity=info.capacity,
            all_muted=info.all_muted,
            policy=asdict(policy),
            revision=settings.revision,
        )
        await self.check_speaking(policy, account, info)
        if policy.trigger > info.capacity:
            raise CleanerError("开始人数超过实际群容量，请调整这个群的配置。")
        triggered = await self.cycle(
            policy,
            account,
            info.count,
            settings.revision,
            settings.enabled and policy.enabled and policy.mode != "仅预览",
        )
        triggered = triggered or info.count >= policy.trigger
        if not triggered and not manual:
            self.journal.record(
                "本次不生成名单",
                reason="人数未达到开始线",
                count=info.count,
                trigger=policy.trigger,
                target=policy.target,
            )
            await self.store.call("set", "check-error:" + gid, "")
            await self.store.call(
                "set",
                "status:" + scope(account, gid),
                {"at": self.clock(), "count": info.count, "text": "人数未达到开始线"},
            )
            return None
        info, members = await adapter.snapshot(gid)
        self.journal.record(
            "一致成员名单已取得",
            count=info.count,
            capacity=info.capacity,
            listed=len(members),
            all_muted=info.all_muted,
        )
        await self.check_speaking(policy, account, info)
        # Recompute using the count belonging to the snapshot, not the earlier count.
        triggered = await self.cycle(
            policy,
            account,
            info.count,
            settings.revision,
            settings.enabled and policy.enabled and policy.mode != "仅预览",
        )
        triggered = triggered or info.count >= policy.trigger
        members = await self.store.call("merge", account, gid, members, self.clock())
        protected, attempted = await self.store.call("exclusions", account, gid, self.clock())
        cache_key = "qq-cache:" + scope(account, gid)
        cache = await self.store.call("get", cache_key, {}) if policy.needs_qq_level else {}
        refreshed_cache, enriched = {}, []
        looked_up = waiting = 0
        for member in members:
            excluded = member.user_id in protected or (member.user_id, member.epoch) in attempted
            preliminary = evaluate(policy, member, self.clock(), account, excluded, defer_qq=True)
            if policy.needs_qq_level:
                self.journal.record(
                    "成员初筛",
                    screening=True,
                    user=member.user_id,
                    member=asdict(member),
                    excluded=excluded,
                    eligible=preliminary.eligible,
                    reason=preliminary.reason,
                )
            if preliminary.eligible and policy.needs_qq_level:
                entry = cache.get(member.user_id, {})
                if entry.get("epoch") != member.epoch or entry.get("expires", 0) <= self.clock():
                    if looked_up >= 20:
                        self.journal.record(
                            "资料补查等待", user=member.user_id, reason="本轮20人补查额度已用完"
                        )
                        waiting += 1
                        enriched.append(replace(member, qq_level=None))
                        continue
                    looked_up += 1
                    try:
                        detail = await adapter.member(gid, member.user_id)
                    except PlatformError as exc:
                        self.journal.record(
                            "成员资料补查失败",
                            exception=exc,
                            user=member.user_id,
                            failure_cache_seconds=6 * 3600,
                        )
                        # Preserve completed work, and avoid the same bad UID blocking every later scan.
                        cache[member.user_id] = {
                            "epoch": member.epoch,
                            "expires": self.clock() + 6 * 3600,
                            "level": None,
                        }
                        await self.store.call("set", cache_key, cache)
                        raise
                    detail = (await self.store.call("merge", account, gid, [detail], self.clock()))[0]
                    entry = {
                        "epoch": detail.epoch,
                        "expires": self.clock() + 7 * 86400,
                        "level": detail.qq_level,
                    }
                    cache[member.user_id] = entry
                    await self.store.call("set", cache_key, cache)
                    member = detail
                    self.journal.record(
                        "成员资料补查完成",
                        user=member.user_id,
                        qq_level=detail.qq_level,
                        cache_expires=entry["expires"],
                    )
                else:
                    self.journal.record(
                        "使用QQ等级缓存",
                        user=member.user_id,
                        qq_level=entry["level"],
                        cache_expires=entry["expires"],
                    )
                refreshed_cache[member.user_id] = entry
                member = replace(member, qq_level=entry["level"])
            enriched.append(member)
        if policy.needs_qq_level:
            await self.store.call("set", cache_key, refreshed_cache)
        reasons, candidates, decisions = Counter(), [], {}
        for member in enriched:
            excluded = member.user_id in protected or (member.user_id, member.epoch) in attempted
            evaluated_at = self.clock()
            decision = evaluate(policy, member, evaluated_at, account, excluded)
            decisions[member.user_id] = (decision, evaluated_at, excluded)
            if decision.eligible:
                candidates.append((decision.sort_key, member, decision))
            else:
                reasons[decision.reason] += 1
        candidates.sort(key=lambda row: row[0])
        need = max(0, info.count - policy.target) if triggered else 0
        selected = candidates[: min(need, settings.pace.batch_size)] if not waiting else []
        ranks = {m.user_id: index for index, (_, m, _) in enumerate(candidates, 1)}
        selected_ids = {m.user_id for _, m, _ in selected}
        for member in enriched:
            decision, evaluated_at, excluded = decisions[member.user_id]
            if member.user_id in selected_ids:
                selection_reason = "进入本批名单"
            elif not decision.eligible:
                selection_reason = decision.reason
            elif waiting:
                selection_reason = "等待QQ等级补查完成，本轮不安排清理"
            elif not triggered:
                selection_reason = "人数未触发清理"
            else:
                selection_reason = "排序在本批名额之外"
            self.journal.record(
                "成员筛选",
                screening=True,
                user=member.user_id,
                member=asdict(member),
                evaluated_at=evaluated_at,
                excluded=excluded,
                eligible=decision.eligible,
                reason=decision.reason,
                score=decision.score,
                sort_key=decision.sort_key,
                rank=ranks.get(member.user_id),
                selected=member.user_id in selected_ids,
                selection_reason=selection_reason,
            )
        state = "preview"
        if settings.enabled and policy.enabled and selected:
            state = {"仅预览": "preview", "确认后清理": "pending", "自动清理": "ready"}[policy.mode]
            if manual and policy.mode == "自动清理":
                state = "preview"
        now = self.clock()
        if self.router.binding_stamp(adapter) != binding or self.settings().revision != settings.revision:
            raise CleanerError("检查期间连接或配置改变，请重新预览。")
        payload = {
            "id": plan_id,
            "account": account,
            "platform": adapter.platform_id,
            "binding": binding,
            "gid": gid,
            "revision": settings.revision,
            "created": now,
            "expires": now + 1800,
            "count": info.count,
            "capacity": info.capacity,
            "target": policy.target,
            "triggered": triggered,
            "eligible": len(candidates),
            "waiting": waiting,
            "reasons": dict(reasons),
            "policy": asdict(policy),
            "members": [
                {"member": asdict(m), "reason": decision.reason, "score": decision.score}
                for _, m, decision in selected
            ],
        }
        await self.store.call("save_plan", payload, state)
        await self.store.call("set", "check-error:" + gid, "")
        await self.store.call(
            "set",
            "status:" + scope(account, gid),
            {"at": now, "count": info.count, "text": "等待资料补全" if waiting else "检查完成"},
        )
        self.journal.record(
            "计划已生成",
            state=state,
            count=info.count,
            eligible=len(candidates),
            waiting=waiting,
            reasons=dict(reasons),
            triggered=triggered,
            selected=[m.user_id for _, m, _ in selected],
            expires=payload["expires"],
        )
        return await self.store.call("plan", payload["id"])

    async def check_speaking(self, policy, account, info):
        info.check_speaking(policy.protect_muted)
        if policy.protect_muted:
            notice = await self.store.call("get", f"ban-notice:{account}:{policy.group_id}:0", {})
            if notice.get("active"):
                raise PlatformError(
                    "已收到全员禁言通知，暂缓清理；若解除通知遗漏，确认解除后用“恢复”重新核验。"
                )

    async def confirm(self, plan_id, policy, adapter, actor):
        self.healthy()
        settings = self.settings()
        plan = await self.store.call("plan", plan_id)
        if (
            plan["gid"] != policy.group_id
            or plan["account"] != adapter.account
            or plan["platform"] != adapter.platform_id
        ):
            raise CleanerError("确认码不属于这个群或机器人。")
        if plan["revision"] != settings.revision or not settings.enabled or not policy.enabled:
            raise CleanerError("配置已改变或未启用，请重新预览。")
        if policy.mode != "确认后清理" or not plan["members"]:
            raise CleanerError("这个计划不能确认；请使用确认后清理模式并重新预览。")
        await self.store.call("approve", plan_id, actor, self.clock())
        self.journal.record(
            "计划已确认", plan=plan_id, actor=actor, account=adapter.account, gid=policy.group_id
        )
        self.next_check[policy.group_id] = 0
        self.wake.set()

    async def pause(self, account, gid, actor):
        key = scope(account, gid)
        self.memory_pauses.add(key)
        await self.store.call("set", "pause:" + key, "管理员手动暂停")
        await self.store.call("audit", self.clock(), account, gid, "暂停", {"actor": actor})
        self.journal.record("群清理已暂停", actor=actor, account=account, gid=gid, reason="管理员手动暂停")

    async def resume(self, account, gid, actor):
        if await self.store.call("unresolved_account", account):
            raise CleanerError("此账号还有结果不明的操作，请先使用“核对”或“保留”。")
        if await self.store.call("get", "account-pause:" + account, ""):
            if await self.store.call("pause_owner", account) != gid:
                raise CleanerError("账号因其他群的操作暂停，请由那个群的管理员核对后恢复。")
        ban = await self.store.call("get", f"ban-notice:{account}:{gid}:0", {})
        if ban.get("active"):
            version = self.event_versions.get((account, gid, "0"), 0)
            # A lift notice may have been lost while QQ/AstrBot was disconnected.
            # Only an explicit administrator recovery can reconcile that persisted notice.
            with read_priority(2):
                adapter = await self.router.resolve(self.settings().group(gid))
                if adapter.account != account:
                    raise CleanerError("恢复期间机器人绑定改变，请重新操作。")
                binding = self.router.binding_stamp(adapter)
                (await adapter.group(gid)).check_speaking(True)
                await self.sleep(2)
                (await adapter.group(gid)).check_speaking(True)
                if self.router.binding_stamp(adapter) != binding:
                    raise CleanerError("恢复期间连接改变，请稍后重试。")
                if self.event_versions.get((account, gid, "0"), 0) != version:
                    raise CleanerError("恢复期间收到新的禁言状态，请重新检查。")
            await self.store.call("clear_group_ban", account, gid, ban, actor, self.clock())
        await self.store.call("set", "pause:" + scope(account, gid), "")
        await self.store.call("set", "account-pause:" + account, "")
        pace = self.settings().pace
        await self.store.call(
            "extend_deadline",
            "cooldown:" + account,
            self.clock() + random.uniform(pace.recovery_min_seconds, pace.recovery_max_seconds),
        )
        self.memory_pauses.discard(scope(account, gid))
        latest = await self.store.call("latest", account, gid)
        if latest:
            await self.store.call("plan_state", latest["id"], "cancelled")
        self.next_check[gid] = 0
        await self.store.call("audit", self.clock(), account, gid, "恢复", {"actor": actor})
        self.journal.record(
            "群清理已恢复", actor=actor, account=account, gid=gid, reason="管理员恢复，旧计划作废并保留冷却"
        )
        self.wake.set()

    async def observe(self, raw):
        """Only positive activity evidence; never infer silence from missing local messages."""
        if self.stopped or self.failure:
            return
        get = raw.get if isinstance(raw, dict) else lambda k, d=None: getattr(raw, k, d)
        account, gid, uid = (str(get(k, "")) for k in ("self_id", "group_id", "user_id"))
        group_ban = get("post_type") == "notice" and get("notice_type") == "group_ban"
        if not all(number(v) for v in (account, gid)) or not (number(uid) or (group_ban and uid == "0")):
            return
        try:
            policy = self.settings().group(gid)
        except CleanerError:
            return
        if policy.bot_qq and policy.bot_qq != account:
            return
        kind = (
            "message"
            if get("post_type") == "message" and get("message_type") == "group"
            else get("notice_type")
        )
        if kind not in ("message", "group_increase", "group_decrease", "group_admin", "group_ban"):
            return
        now = self.clock()
        occurred = number(get("time"))
        if not occurred or occurred > now + 300:
            self.failure = "收到时间异常的群事件，已暂停清理，请检查 NapCat 与系统时间。"
            self.journal.record(
                "群事件异常",
                reason=self.failure,
                account=account,
                gid=gid,
                user=uid,
                event_type=kind,
                occurred=occurred,
                received=now,
            )
            return
        duration = number(get("duration"), zero=True) if group_ban else 0
        if group_ban and (get("sub_type") not in ("ban", "lift_ban") or duration is None):
            self.failure = "收到无法识别的禁言通知，已暂停清理，请检查 NapCat。"
            self.journal.record(
                "群事件异常",
                reason=self.failure,
                account=account,
                gid=gid,
                user=uid,
                event_type=kind,
                subtype=get("sub_type"),
                duration=duration,
            )
            return
        key = (account, gid, uid)
        self.event_versions[key] = self.event_versions.get(key, 0) + 1
        if len(self.event_versions) > 100000:
            self.failure = "活动保护记录达到容量上限，请检查群配置后重载。"
            self.journal.record("群事件异常", reason=self.failure, account=account, gid=gid, user=uid)
            return
        identity = [account, gid, uid, kind, occurred, get("message_id"), get("operator_id"), get("sub_type")]
        if group_ban:
            identity.append(duration)
        fingerprint = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
        try:
            outcome = await self.store.call(
                "observe",
                account,
                gid,
                uid,
                kind,
                occurred,
                now,
                fingerprint,
                str(get("operator_id", "")),
                str(get("sub_type", "")),
                duration,
            )
            self.journal.record(
                "群事件处理",
                screening=kind == "message",
                account=account,
                gid=gid,
                user=uid,
                event_type=kind,
                occurred=occurred,
                received=now,
                operator=str(get("operator_id", "")),
                subtype=str(get("sub_type", "")),
                duration=duration,
                fingerprint=fingerprint,
                result=outcome,
            )
            if isinstance(outcome, dict):
                for op in outcome["confirmed_operations"]:
                    self.journal.record(
                        "通知确认移出",
                        operation=op["id"],
                        plan=op["plan"],
                        account=account,
                        gid=gid,
                        user=uid,
                        state="confirmed_removed",
                        reason="收到匹配本次入群和提交时间的本账号移出通知",
                    )
        except CleanerError as exc:
            self.failure = str(exc)
            self.journal.record(
                "群事件记录失败", exception=exc, account=account, gid=gid, user=uid, event_type=kind
            )
        except BaseException as exc:
            self.journal.record(
                "群事件处理中断", exception=exc, account=account, gid=gid, user=uid, event_type=kind
            )
            raise

    async def check_group(self, policy):
        with self.journal.span("群检查", check=secrets.token_hex(6), gid=policy.group_id):
            await self._check_group(policy)

    async def _check_group(self, policy):
        from .executor import Executor

        gid = policy.group_id
        async with self.group_lock(gid):
            try:
                self.healthy()
                # A job may have waited for a concurrent command; use the current policy.
                policy = self.settings().group(gid)
                if not self.settings().enabled or not policy.enabled:
                    self.journal.record("群检查跳过", reason="总开关或群规则已关闭")
                    return
                adapter = await self.router.resolve(policy)
                if await self.store.call(
                    "get", "account-pause:" + adapter.account, ""
                ) or await self.store.call("unresolved_account", adapter.account):
                    raise CleanerError("此账号存在暂停或待核对操作，请核对后恢复。")
                if await self.store.call("get", "pause:" + scope(adapter.account, gid), ""):
                    raise CleanerError("本群已由管理员暂停。")
                plan = await self.store.call("latest", adapter.account, gid)
                ready = plan and plan["state"] == "ready" and plan["expires"] > self.clock()
                if not ready:
                    plan = await self.build_plan(policy, adapter)
                if plan and plan["state"] == "ready":
                    await Executor(self).execute(plan, adapter)
                self.next_check[gid] = self.clock() + random.uniform(1800, 2100)
                self.journal.record("下次检查已安排", retry_at=self.next_check[gid])
            except Deferred as exc:
                self.next_check[gid] = max(self.clock() + 15, exc.until)
                await self.store.call("set", "check-error:" + gid, str(exc))
                self.journal.record("等待执行", str(exc), exception=exc, retry_at=self.next_check[gid])
            except CleanerError as exc:
                self.journal.record("检查暂缓", str(exc), exception=exc, retry_at=self.clock() + 3600)
                await self.store.call("set", "check-error:" + gid, str(exc))
                self.next_check[gid] = self.clock() + 3600

    def dispatch(self):
        # At most four groups work concurrently. Account locks still serialize removals.
        # Oldest due groups come first, so a large group cannot monopolize every budget window.
        for gid, task in list(self.workers.items()):
            if task.done():
                if not task.cancelled() and task.exception():
                    self.failure = "群检查任务异常，已停止清理，请查看日志并重载。"
                    self.journal.record("任务异常", exception=task.exception(), gid=gid)
                del self.workers[gid]
        if self.failure or not self.settings().enabled or self.stopped:
            return
        policies = sorted(self.settings().groups, key=lambda p: self.next_check.get(p.group_id, 0))
        for policy in policies:
            if len(self.workers) >= 4:
                break
            gid = policy.group_id
            if (
                policy.enabled
                and gid not in self.workers
                and not self.group_lock(gid).locked()
                and self.next_check.get(gid, 0) <= self.clock()
            ):
                self.workers[gid] = asyncio.create_task(self.check_group(policy), name="qq-cleaner-group")

    async def loop(self):
        maintenance_at = 0
        while not self.stopped:
            self.wake.clear()
            try:
                self.healthy()
                now = self.clock()
                await self.store.call("set", "last_wall", now)
                if now >= maintenance_at:
                    await self.store.call("maintain", now)
                    self.journal.maintain()
                    maintenance_at = now + 86400
                self.dispatch()
            except CleanerError as exc:
                self.journal.record("调度暂停", str(exc), exception=exc)
            except Exception as exc:
                self.failure = "插件遇到内部异常，已停止清理，请查看日志并重载。"
                self.journal.record("内部异常", exception=exc)
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=15)
            except asyncio.TimeoutError:
                pass
