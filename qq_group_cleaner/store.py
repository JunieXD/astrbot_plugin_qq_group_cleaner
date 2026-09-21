"""Durable decisions and write-ahead intents, serialized on one SQLite thread."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

from .config import CleanerError, Deferred
from .rules import Member


class Store:
    def __init__(self, path: Path):
        self.path = path
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="qq-cleaner-db")
        self.pending = 0
        self.healthy = True
        self.closed = False
        self.closing = False
        self.close_lock = asyncio.Lock()

    async def call(self, method: str, *args):
        if self.closed or ((self.closing or not self.healthy) and method != "close_db"):
            raise CleanerError("审计存储不可用，已停止清理，请检查磁盘和插件日志。")
        self.pending += 1
        if self.pending > 512:
            self.pending -= 1
            self.healthy = False
            raise CleanerError("活动记录积压，已停止清理，请重载插件后检查状态。")
        future = asyncio.get_running_loop().run_in_executor(self.worker, getattr(self, method), *args)
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            # Finish any outstanding commit before the old instance can release its lock.
            await asyncio.shield(future)
            raise
        except (sqlite3.Error, OSError) as exc:
            self.healthy = False
            raise CleanerError("审计存储写入失败，已停止清理，请检查磁盘和插件日志。") from exc
        finally:
            self.pending -= 1

    async def close(self):
        async with self.close_lock:
            if self.closed:
                return
            self.closing = True
            try:
                await self.call("close_db")
            finally:
                self.closed = True
                self.worker.shutdown(wait=True)

    def open_db(self):
        self.db = sqlite3.connect(self.path, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA busy_timeout=5000")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, 2):
            raise CleanerError("数据库来自更新的插件版本，请升级插件，不要删除数据库。")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS members (
                account TEXT, gid TEXT, uid TEXT, joined INTEGER, epoch INTEGER NOT NULL,
                activity REAL NOT NULL, event_at REAL NOT NULL, present INTEGER NOT NULL,
                PRIMARY KEY(account,gid,uid));
            CREATE TABLE IF NOT EXISTS exemptions (
                account TEXT, gid TEXT, uid TEXT, expires REAL NOT NULL, actor TEXT NOT NULL,
                PRIMARY KEY(account,gid,uid));
            CREATE TABLE IF NOT EXISTS plans (
                id TEXT PRIMARY KEY, account TEXT, gid TEXT, created REAL, expires REAL,
                state TEXT, payload TEXT NOT NULL, approver TEXT);
            CREATE INDEX IF NOT EXISTS plans_group ON plans(account,gid,created);
            CREATE TABLE IF NOT EXISTS operations (
                id INTEGER PRIMARY KEY, plan TEXT, account TEXT, gid TEXT, uid TEXT,
                epoch INTEGER, submitted REAL, state TEXT, reason TEXT,
                UNIQUE(account,gid,uid,epoch));
            CREATE INDEX IF NOT EXISTS operations_quota ON operations(account,submitted);
            CREATE TABLE IF NOT EXISTS audit (
                id INTEGER PRIMARY KEY, at REAL, account TEXT, gid TEXT, kind TEXT, detail TEXT);
            CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, at REAL);
            PRAGMA user_version=2;
        """)
        with self.db:
            self.db.execute(
                "UPDATE operations SET state='unknown',reason='插件在操作提交后中断' WHERE state='submitted'"
            )
            self.db.execute("UPDATE plans SET state='cancelled' WHERE state IN ('ready','running','pending')")

    def close_db(self):
        if hasattr(self, "db"):
            try:
                self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                self.db.close()

    def get(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def _set(self, key, value):
        self.db.execute(
            "INSERT INTO state VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value, ensure_ascii=False)),
        )

    def set(self, key, value):
        with self.db:
            self._set(key, value)

    def _audit(self, now, account, gid, kind, detail):
        self.db.execute(
            "INSERT INTO audit(at,account,gid,kind,detail) VALUES(?,?,?,?,?)",
            (now, account, gid, kind, json.dumps(detail, ensure_ascii=False)),
        )

    def audit(self, now, account, gid, kind, detail):
        with self.db:
            self._audit(now, account, gid, kind, detail)

    def _clear_role_before_join(self, account, gid, uid, joined):
        key = f"role-notice:{account}:{gid}:{uid}"
        notice = self.get(key, {})
        if joined > notice.get("at", 0):
            self._set(key, {})

    def merge(self, account: str, gid: str, members: list[Member], now=None) -> list[Member]:
        now = time.time() if now is None else now
        result = []
        with self.db:
            for member in members:
                row = self.db.execute(
                    "SELECT * FROM members WHERE account=? AND gid=? AND uid=?",
                    (account, gid, member.user_id),
                ).fetchone()
                epoch = row["epoch"] if row else 1
                activity = row["activity"] if row else 0
                event_at = row["event_at"] if row else 0
                joined = row["joined"] if row else None
                present = row["present"] if row else 1
                incoming = member.joined
                valid_join = incoming is not None and 0 < incoming <= now + 300
                known = valid_join
                if present == 2:  # An increase notice awaits a matching platform join timestamp.
                    known = valid_join and incoming >= event_at - 300
                    if known:
                        joined, present = incoming, 1
                elif present == 0:
                    # A cached old member must never resurrect a membership that has ended.
                    known = (
                        valid_join and incoming >= event_at - 300 and (joined is None or incoming > joined)
                    )
                    if known:
                        joined, present, epoch = incoming, 1, epoch + 1
                        activity = max(activity, now)
                        self._clear_role_before_join(account, gid, member.user_id, incoming)
                elif valid_join:
                    if joined is None:
                        joined = incoming
                    elif incoming > joined:
                        joined, epoch = incoming, epoch + 1
                        activity = max(activity, now)  # New membership or uncertain timestamp correction.
                        self._clear_role_before_join(account, gid, member.user_id, incoming)
                    elif incoming < joined:
                        known = False  # Do not rewind canonical identity or release attempt deduplication.
                if member.last_sent and 0 < member.last_sent <= now + 300:
                    activity = max(activity, member.last_sent)
                self.db.execute(
                    """INSERT INTO members VALUES(?,?,?,?,?,?,?,?)
                    ON CONFLICT(account,gid,uid) DO UPDATE SET joined=excluded.joined,
                    epoch=excluded.epoch,activity=excluded.activity,present=excluded.present""",
                    (account, gid, member.user_id, joined, epoch, activity, event_at, present),
                )
                role_notice = self.get(f"role-notice:{account}:{gid}:{member.user_id}", {})
                role = "admin" if role_notice.get("role") == "admin" else member.role
                result.append(
                    replace(member, epoch=epoch, activity=activity, membership_known=known, role=role)
                )
        return result

    def observe(self, account, gid, uid, kind, occurred, received, fingerprint, operator="", subtype=""):
        with self.db:
            if fingerprint:
                inserted = self.db.execute(
                    "INSERT OR IGNORE INTO events VALUES(?,?)", (fingerprint, received)
                )
                if not inserted.rowcount:
                    return
            row = self.db.execute(
                "SELECT * FROM members WHERE account=? AND gid=? AND uid=?", (account, gid, uid)
            ).fetchone()
            epoch = row["epoch"] if row else 1
            joined = row["joined"] if row else None
            activity = row["activity"] if row else 0
            event_at = row["event_at"] if row else 0
            present = row["present"] if row else 1
            if kind in ("message", "group_increase", "group_admin"):
                activity = max(activity, occurred, received)  # late notices still protect
            if kind in ("group_increase", "group_decrease") and occurred >= event_at:
                event_at = occurred
                present = 2 if kind == "group_increase" else 0
                if present == 2:
                    epoch += 1
            notice_key = f"role-notice:{account}:{gid}:{uid}"
            previous_notice = self.get(notice_key, {})
            if occurred >= previous_notice.get("at", 0):
                if kind == "group_admin" and subtype in ("set", "unset"):
                    self._set(notice_key, {"at": occurred, "role": "admin" if subtype == "set" else "member"})
                elif kind == "group_decrease":
                    self._set(notice_key, {"at": occurred, "role": "absent"})
                elif kind == "group_increase":
                    self._set(notice_key, {"at": occurred})
            self.db.execute(
                """INSERT INTO members VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(account,gid,uid)
                DO UPDATE SET joined=excluded.joined,epoch=excluded.epoch,activity=excluded.activity,
                event_at=excluded.event_at,present=excluded.present""",
                (account, gid, uid, joined, epoch, activity, event_at, present),
            )
            if kind == "group_decrease" and operator == account and subtype == "kick":
                self.db.execute(
                    """UPDATE operations SET state='confirmed_removed',reason='收到本账号移出成员通知'
                    WHERE account=? AND gid=? AND uid=? AND state IN ('submitted','unknown')
                    AND submitted<=? AND epoch=?""",
                    (account, gid, uid, occurred + 2, row["epoch"] if row else -1),
                )

    def exclusions(self, account, gid, now):
        rows = self.db.execute(
            "SELECT uid FROM exemptions WHERE account=? AND gid=? AND (expires=0 OR expires>?)",
            (account, gid, now),
        ).fetchall()
        protected = {r[0] for r in rows}
        attempts = {
            (r[0], r[1])
            for r in self.db.execute(
                "SELECT uid,epoch FROM operations WHERE account=? AND gid=?", (account, gid)
            )
        }
        return protected, attempts

    def protect(self, account, gid, uid, expires, actor, now):
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO exemptions VALUES(?,?,?,?,?)", (account, gid, uid, expires, actor)
            )
            self._audit(now, account, gid, "保护", {"user": uid, "expires": expires, "actor": actor})

    def unprotect(self, account, gid, uid, actor, now):
        with self.db:
            self.db.execute("DELETE FROM exemptions WHERE account=? AND gid=? AND uid=?", (account, gid, uid))
            # Removing protection starts a seven-day grace period, never an immediate kick.
            self.db.execute(
                "INSERT INTO exemptions VALUES(?,?,?,?,?)", (account, gid, uid, now + 7 * 86400, actor)
            )
            self._audit(now, account, gid, "取消保护，缓冲7天", {"user": uid, "actor": actor})

    def save_plan(self, payload, state):
        with self.db:
            self.db.execute(
                "UPDATE plans SET state='cancelled' WHERE account=? AND gid=? AND state IN ('pending','ready','preview')",
                (payload["account"], payload["gid"]),
            )
            self.db.execute(
                "INSERT INTO plans VALUES(?,?,?,?,?,?,?,NULL)",
                (
                    payload["id"],
                    payload["account"],
                    payload["gid"],
                    payload["created"],
                    payload["expires"],
                    state,
                    json.dumps(payload, ensure_ascii=False),
                ),
            )

    def plan(self, plan_id):
        row = self.db.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
        if row is None:
            raise CleanerError("计划不存在，请重新预览。")
        result = json.loads(row["payload"])
        return {**result, "state": row["state"], "approver": row["approver"]}

    def latest(self, account, gid):
        row = self.db.execute(
            "SELECT id FROM plans WHERE account=? AND gid=? ORDER BY created DESC LIMIT 1", (account, gid)
        ).fetchone()
        return self.plan(row[0]) if row else None

    def approve(self, plan_id, actor, now):
        with self.db:
            changed = self.db.execute(
                "UPDATE plans SET state='ready',approver=? WHERE id=? AND state='pending' AND expires>?",
                (actor, plan_id, now),
            ).rowcount
            if not changed:
                raise CleanerError("计划已失效或已确认，请重新预览。")

    def plan_state(self, plan_id, state):
        with self.db:
            self.db.execute("UPDATE plans SET state=? WHERE id=?", (state, plan_id))

    def quota(self, account, gid, now):
        row = self.db.execute(
            "SELECT count(*),coalesce(sum(gid=?),0) FROM operations WHERE account=? AND submitted>?",
            (gid, account, now - 86400),
        ).fetchone()
        return row[0], row[1]

    def reserve_read(self, platform, now, limit):
        with self.db:
            key = "reads:" + platform
            times = [stamp for stamp in self.get(key, []) if stamp > now - 3600]
            if len(times) >= limit:
                raise Deferred("本小时资料读取已达上限，稍后再检查。", min(times) + 3601)
            times.append(now)
            self._set(key, times)

    def unresolved(self, account, gid):
        return [
            dict(r)
            for r in self.db.execute(
                "SELECT * FROM operations WHERE account=? AND gid=? AND state IN ('submitted','unknown') ORDER BY id",
                (account, gid),
            )
        ]

    def unresolved_account(self, account):
        return [
            dict(r)
            for r in self.db.execute(
                "SELECT * FROM operations WHERE account=? AND state IN ('submitted','unknown')", (account,)
            )
        ]

    def pause_owner(self, account):
        pause = self.get("account-pause:" + account, "")
        if isinstance(pause, dict):
            return pause.get("gid")
        # v0.1.0 stored a string, always after creating the corresponding operation.
        row = self.db.execute(
            "SELECT gid FROM operations WHERE account=? ORDER BY submitted DESC,id DESC LIMIT 1", (account,)
        ).fetchone()
        return row[0] if row else None

    def reserve(self, plan_id, member, now, group_limit, account_limit):
        """Intent, attempt quota and uniqueness commit together BEFORE the network write."""
        with self.db:
            plan = self.plan(plan_id)
            if plan["state"] != "running" or plan["expires"] <= now:
                raise CleanerError("计划已失效，请重新预览。")
            account, gid = plan["account"], plan["gid"]
            used_account, used_group = self.quota(account, gid, now)
            if used_account >= account_limit or used_group >= group_limit:
                raise CleanerError("已达到最近 24 小时清理上限，等待额度恢复。")
            if self.unresolved_account(account):
                raise CleanerError("有结果不明的操作，需先核对或保留该成员。")
            if not any(
                item["member"]["user_id"] == member.user_id and item["member"]["epoch"] == member.epoch
                for item in plan["members"]
            ):
                raise CleanerError("成员不在本批已授权名单中。")
            current = self.db.execute(
                "SELECT * FROM members WHERE account=? AND gid=? AND uid=?", (account, gid, member.user_id)
            ).fetchone()
            if (
                not current
                or current["present"] != 1
                or current["epoch"] != member.epoch
                or current["joined"] != member.joined
                or current["activity"] > member.activity
            ):
                raise CleanerError("成员身份或活动已变化，取消本次提交。")
            if member.user_id in self.exclusions(account, gid, now)[0]:
                raise CleanerError("成员刚被加入保护名单，取消本次提交。")
            try:
                cursor = self.db.execute(
                    """INSERT INTO operations(plan,account,gid,uid,epoch,submitted,state,reason)
                    VALUES(?,?,?,?,?,?,'submitted','等待结果核验')""",
                    (plan_id, account, gid, member.user_id, member.epoch, now),
                )
            except sqlite3.IntegrityError as exc:
                raise CleanerError("这个成员本次入群已经有操作记录，不会重复提交。") from exc
            self._audit(
                now,
                account,
                gid,
                "提交意图",
                {"operation": cursor.lastrowid, "plan": plan_id, "user": member.user_id},
            )
            return cursor.lastrowid

    def operation(self, operation_id):
        return dict(self.db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone())

    def cancel_intent(self, operation_id, now):
        with self.db:
            op = self.operation(operation_id)
            self._audit(
                now, op["account"], op["gid"], "发送前取消", {"operation": operation_id, "user": op["uid"]}
            )
            self.db.execute("DELETE FROM operations WHERE id=? AND state='submitted'", (operation_id,))

    def result(self, operation_id, state, reason, now):
        with self.db:
            op = self.operation(operation_id)
            if op["state"] == "confirmed_removed":
                return op["state"]
            self.db.execute(
                "UPDATE operations SET state=?,reason=? WHERE id=?", (state, reason, operation_id)
            )
            self._audit(
                now,
                op["account"],
                op["gid"],
                state,
                {"operation": operation_id, "user": op["uid"], "reason": reason},
            )
            return state

    def history(self, account, gid):
        return [
            dict(row)
            for row in self.db.execute(
                "SELECT * FROM operations WHERE account=? AND gid=? ORDER BY id DESC LIMIT 20", (account, gid)
            )
        ]

    def maintain(self, now):
        with self.db:
            self.db.execute("DELETE FROM events WHERE at<?", (now - 7 * 86400,))
            self.db.execute("DELETE FROM audit WHERE at<?", (now - 180 * 86400,))
            self.db.execute(
                "DELETE FROM plans WHERE created<? AND id NOT IN (SELECT plan FROM operations)",
                (now - 30 * 86400,),
            )
            # Keep every attempt for the current membership, including reviewed/uncertain attempts.
            self.db.execute(
                """DELETE FROM operations WHERE submitted<? AND state NOT IN ('unknown','submitted')
                AND EXISTS (SELECT 1 FROM members m WHERE m.account=operations.account AND m.gid=operations.gid
                AND m.uid=operations.uid AND m.epoch!=operations.epoch)""",
                (now - 180 * 86400,),
            )
        self.db.execute("PRAGMA wal_checkpoint(PASSIVE)")
