"""Wake lifecycle, dialogue continuations and scope scheduling state."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Sequence
from enum import StrEnum
from uuid import uuid4

from nanobot.channels.telegram.social.db.connection import transaction
from nanobot.channels.telegram.social.db.messages import Messages
from nanobot.channels.telegram.social.db.scopes import Scopes
from nanobot.channels.telegram.social.db.views import Views
from nanobot.channels.telegram.social.models import DialogueContext, ProactiveCandidate, WakeJob


class WakeStatus(StrEnum):
    SCHEDULED = "scheduled"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    UNSENT = "unsent"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


ACTIVE = (WakeStatus.SCHEDULED, WakeStatus.QUEUED, WakeStatus.RUNNING)

# Every status write goes through Wakes._move; terminal states have no exits.
TRANSITIONS: dict[WakeStatus, frozenset[WakeStatus]] = {
    WakeStatus.SCHEDULED: frozenset({WakeStatus.QUEUED, WakeStatus.RUNNING, WakeStatus.CANCELLED, WakeStatus.EXPIRED}),
    WakeStatus.QUEUED: frozenset({WakeStatus.RUNNING, WakeStatus.SCHEDULED, WakeStatus.CANCELLED, WakeStatus.EXPIRED}),
    WakeStatus.RUNNING: frozenset({WakeStatus.COMPLETED, WakeStatus.FAILED, WakeStatus.UNSENT, WakeStatus.SCHEDULED,
                                   WakeStatus.CANCELLED, WakeStatus.EXPIRED}),
}


class WakeReason(StrEnum):
    JUDGE = "judge"
    PROACTIVE = "proactive"
    WAIT_REPLY = "wait_reply"
    WAIT_MESSAGES = "wait_messages"
    TIMEOUT = "timeout"


RANK = {WakeReason.JUDGE: 4, WakeReason.WAIT_REPLY: 1, WakeReason.WAIT_MESSAGES: 1,
        WakeReason.TIMEOUT: 0, WakeReason.PROACTIVE: -1}


class ScopeMode(StrEnum):
    OBSERVING = "observing"
    ACTIVE = "active"
    WAITING = "waiting"
    SLEEPING = "sleeping"
    PAUSED = "paused"


class Wakes:
    def __init__(self, db: sqlite3.Connection, now: Callable[[], float], scopes: Scopes,
                 messages: Messages, views: Views) -> None:
        self.db, self.now = db, now
        self.scopes, self.messages, self.views = scopes, messages, views

    def _move(self, to: WakeStatus, where: str, params: Sequence[object], **columns: object) -> int:
        """Move only states with a declared transition to `to`; return the updated row count."""
        sources = [source for source, targets in TRANSITIONS.items() if to in targets]
        assignments = ",".join(["status=?", *(f"{column}=?" for column in columns)])
        marks = ",".join("?" for _ in sources)
        return self.db.execute(f"UPDATE wakes SET {assignments} WHERE ({where}) AND status IN ({marks})", (to, *columns.values(), *params, *sources)).rowcount

    def _insert_scheduled(self, columns: str, values: str, params: Sequence[object]) -> None:
        self.db.execute(f"INSERT INTO wakes({columns}) " + values.format(status=WakeStatus.SCHEDULED), params)

    def claim(self, key: str, job: str, version: str, turn: str) -> None:
        with transaction(self.db):
            self._check_trigger_source(key, job)
            context = self.dialogue_job(job)
            if context is not None and context["source_seq"] > 0 and not self.messages.retains_seq(key, context["source_seq"]):
                raise PermissionError("dialogue source is no longer retained")
            changed = self._move(WakeStatus.RUNNING, "id=? AND scope=? AND version=? AND status='queued' AND expires>?", (job,key,version,self.now(),), turn_id=turn)
            if changed != 1:
                raise PermissionError("wake is stale, expired or already claimed")
            self.db.execute("UPDATE scopes SET mode=?,wake_at=NULL,wait_infinite=0 WHERE key=?", (ScopeMode.ACTIVE, key,))
            self.db.execute("DELETE FROM dialogue_waits WHERE scope=?", (key,))

    def active_turn(self, key: str, version: str, turn: str) -> bool:
        return self.db.execute( "SELECT 1 FROM wakes WHERE scope=? AND version=? AND turn_id=? AND status='running' AND expires>?", (key, version, turn, self.now()), ).fetchone() is not None

    def finish(self, turn: str, status: WakeStatus) -> None:
        with transaction(self.db):
            self._move(status, "turn_id=? AND status='running'", (turn,))
            self.db.execute("UPDATE scopes SET mode=? WHERE mode=? AND key IN (SELECT scope FROM wakes WHERE turn_id=?)", (ScopeMode.OBSERVING, ScopeMode.ACTIVE, turn,))

    def dialogue_job(self, job: str) -> DialogueContext | None:
        row = self.db.execute("SELECT * FROM dialogue_jobs WHERE job=?", (job,)).fetchone()
        if row is None:
            return None
        return {"purpose": str(row["purpose"]), "quiet": self.now() >= float(row["quiet_due"]),
                "quiet_due": float(row["quiet_due"]), "total_due": float(row["total_due"]), "source_seq": int(row["source_seq"]), "after_seq": int(row["after_seq"])}

    def sleep_observation_due(self, key: str, window: float) -> float | None:
        """Receiving content alone does not establish model observation."""
        row = self.db.execute("SELECT * FROM dialogue_state WHERE scope=?", (key,)).fetchone()
        now = self.now()
        if row is None:
            return now + window
        last = float(row["last_at"]) if row["last_at"] is not None else now
        if row["last_sent"] is not None:
            last = max(last, float(row["last_sent"]))
        if 0 <= now - last and now - last >= window:
            return None  # The conversation has already been continuously quiet.
        observed = float(row["observed_at"]) if row["observed_at"] is not None else now
        if observed <= now and now >= observed + window and int(row["observed_seq"]) >= self.scopes.state(key)["event_seq"]:
            return None
        fully_observed = int(row["observed_seq"]) >= self.scopes.state(key)["event_seq"]
        observation_due = observed + window if fully_observed and observed <= now < observed + window else last + window
        return min(observation_due, last + window)

    def consume_dialogue(self) -> None:
        now = self.now()
        with transaction(self.db):
            # Backwards clock jumps invalidate transient attention, not history.
            self._move(WakeStatus.CANCELLED, "status='scheduled' AND created>? AND id IN (SELECT job FROM dialogue_jobs)", (now,))
            self.db.execute("UPDATE dialogue_state SET last_at=MIN(last_at,?),last_sent=MIN(last_sent,?),observed_at=CASE WHEN observed_at>? THEN NULL ELSE observed_at END", (now, now, now))
            for row in self.db.execute("SELECT * FROM dialogue_waits").fetchall():
                key = str(row["scope"])
                quiet_due = float(row["quiet_due"]) if row["quiet_due"] is not None else None
                limits = [float(row[name]) for name in ("quiet_due", "total_due") if row[name] is not None]
                due = min(limits) if limits else None
                expired_condition = row["condition_expires"] is not None and float(row["condition_expires"]) <= now
                if now >= float(row["started"]) and (due is None or now < due) and not expired_condition:
                    continue
                self.db.execute("DELETE FROM dialogue_waits WHERE scope=?", (key,))
                self.db.execute("UPDATE scopes SET mode=?,wait_infinite=0 WHERE key=? AND mode=?", (ScopeMode.OBSERVING, key, ScopeMode.WAITING,))
                if expired_condition or due is None or now < float(row["started"]) or now >= due + 120:
                    continue
                if row["purpose"] == "observe":
                    # A pending/new event gets a model opportunity before sleeping.
                    pending = self.db.execute("SELECT 1 FROM wakes WHERE scope=? AND status IN ('scheduled','queued','running') AND (turn_id IS NULL OR status<>'running')", (key,)).fetchone()
                    if pending is None and self.scopes.state(key)["event_seq"] <= int(row["after_seq"]):
                        wake_at = (float(row["sleep_until"]) if row["sleep_until"] is not None else
                                   now + float(row["sleep_seconds"]) if row["sleep_seconds"] is not None else None)
                        self.db.execute("UPDATE scopes SET mode=?,wake_at=?,wake_on_timeout=?,wait_infinite=? WHERE key=?", (ScopeMode.SLEEPING, wake_at, row["on_timeout"], int(wake_at is None), key,))
                    continue
                new = int(row["source_seq"]) > int(row["after_seq"])
                if row["purpose"] != "reply" and not new and not row["on_timeout"]:
                    continue
                if row["purpose"] == "messages" and not new:
                    state = self.scopes.state(key)
                    if state["event_seq"] <= state["timeout_through"]:
                        continue
                    self.db.execute("UPDATE scopes SET timeout_through=event_seq WHERE key=?", (key,))
                reason = WakeReason.WAIT_REPLY if row["purpose"] == "reply" else WakeReason.WAIT_MESSAGES
                seq = int(row["source_seq"])
                # Consume and enqueue in the same transaction, even if the source was marked read.
                self.schedule(key, seq, reason, 0)
                job = self.db.execute("SELECT id FROM wakes WHERE scope=? AND status='scheduled'", (key,)).fetchone()
                if job:
                    self.db.execute("INSERT OR REPLACE INTO dialogue_jobs VALUES(?,?,?,?,?,?,?)",
                                    (job[0], row["purpose"], row["quiet_seconds"], quiet_due if quiet_due is not None else now + 1,
                                     row["total_due"], row["after_seq"], seq))
                    if new:
                        self.db.execute("UPDATE wakes SET condition_config=? WHERE id=? AND reason='wait_messages'", (self.scopes.state(key)["wake_config"], job[0]))
                    if row["condition_expires"] is not None:
                        self.db.execute("UPDATE wakes SET expires=MIN(expires,?) WHERE id=? AND reason='wait_messages'", (row["condition_expires"], job[0]))

    def dialogue_status(self, key: str) -> dict[str, object]:
        wait = self.wait(key)
        return {"wait": dict(wait) if wait else None}

    def defer(self, key: str, turn: str, *, sleeping: bool, seconds: float | None, on_timeout: bool) -> None:
        shown = self.views.shown_sequences(key, turn)
        with transaction(self.db):
            self.db.execute("DELETE FROM dialogue_waits WHERE scope=?", (key,))
            self.db.execute(
                "UPDATE scopes SET mode=?,wake_at=?,wake_on_timeout=?,wait_after_seq=?,wait_infinite=? WHERE key=?",
                (ScopeMode.SLEEPING if sleeping else ScopeMode.WAITING, self.now() + seconds if seconds is not None else None,
                 int(on_timeout), max(shown, default=0), int(seconds is None), key),
            )
            # A newly arrived direct address must survive a simultaneous wait.
            self._move(WakeStatus.CANCELLED, "scope=? AND status='scheduled' AND reason <> 'judge' AND (trigger_seq<=? OR ?)", (key,max(shown, default=0),int(sleeping),))

    def set_wake_config(self, key: str, body: str) -> None:
        with transaction(self.db):
            for candidate in self.db.execute("SELECT * FROM wakes WHERE scope=? AND condition_config<>'{}' AND condition_config<>? AND status IN ('scheduled','queued')", (key, body)).fetchall():
                self._refund(candidate)
                self._move(WakeStatus.CANCELLED, "id=?", (candidate["id"],))
            self.db.execute("UPDATE scopes SET wake_config=? WHERE key=?", (body, key))
            if body == "{}":
                expired = self.db.execute("DELETE FROM dialogue_waits WHERE scope=? AND condition_expires<=?", (key, self.now())).rowcount
                if expired:
                    self.db.execute("UPDATE scopes SET mode=?,wait_infinite=0 WHERE key=? AND mode=?", (ScopeMode.OBSERVING, key, ScopeMode.WAITING,))
                self.db.execute("UPDATE scopes SET mode=?,wait_infinite=0,wake_at=NULL WHERE key=? AND wait_infinite=1 AND mode IN (?,?)", (ScopeMode.OBSERVING, key, ScopeMode.WAITING, ScopeMode.SLEEPING,))

    def schedule(self, key: str, seq: int, reason: WakeReason, delay: float) -> None:
        """Caller owns the transaction, including observation/dedup when applicable."""
        now = self.now()
        state = self.scopes.state(key)
        if state["mode"] == "paused":
            return
        # A queued opportunity has not reached a model yet. Let an actual
        # conversation replace it; its old bus item will fail ordinary claim.
        for opportunity in self.db.execute("SELECT id FROM wakes WHERE scope=? AND reason='proactive' AND status='queued'", (key,)).fetchall():
            self.cancel_opportunity(str(opportunity[0]))
        row = self.db.execute("SELECT * FROM wakes WHERE scope=? AND version=? AND status='scheduled'", (key, state["version"])).fetchone()
        condition_config = "{}"
        if row is not None:
            chosen = reason if RANK[reason] >= RANK[row["reason"]] else str(row["reason"])
            source = seq if chosen == reason else int(row["source_seq"] or row["trigger_seq"])
            if chosen != reason:
                condition_config = str(row["condition_config"])
            self.db.execute("UPDATE wakes SET trigger_seq=?,source_seq=?,condition_config=?,reason=?,due=MIN(due,?),expires=?,proactive_source=NULL WHERE id=?",
                            (max(seq, int(row["trigger_seq"])), source, condition_config, chosen, now + delay,
                             now + 120, row["id"]))
        else:
            self._insert_scheduled("id,scope,version,trigger_seq,source_seq,condition_config,status,created,expires,due,reason",
                                   "VALUES(?,?,?,?,?,?,'{status}',?,?,?,?)",
                                   (uuid4().hex, key, state["version"], seq, seq, condition_config, now, now + 120, now + delay, reason))

    def consume_timers(self) -> None:
        now = self.now()
        self.consume_dialogue()
        for scope in self.scopes.all():
            state = self.scopes.state(scope.session_key)
            if state["wake_at"] is None or state["wake_at"] > now:
                continue
            with transaction(self.db):
                self.db.execute("UPDATE scopes SET mode=?,wake_at=NULL,wait_infinite=0 WHERE key=?", (ScopeMode.OBSERVING, scope.session_key,))
                if (state["wake_on_timeout"] and now < state["wake_at"] + 120
                        and state["event_seq"] > state["timeout_through"]):
                    # Insert in the same transaction as consuming the timer.
                    self._insert_scheduled("id,scope,version,trigger_seq,status,created,expires,due,reason",
                                           "SELECT ?,?,?,?,'{status}',?,?,?,'timeout' WHERE NOT EXISTS "
                                           "(SELECT 1 FROM wakes WHERE scope=? AND status='{status}')",
                                           (uuid4().hex, scope.session_key, state["version"], state["next_seq"] - 1,
                                            now, now + 120, now, scope.session_key))
                    self.db.execute("UPDATE scopes SET timeout_through=event_seq WHERE key=?", (scope.session_key,))

    def ready(self, limit: int, cooldown: float) -> list[WakeJob]:
        now = self.now()
        ready: list[WakeJob] = []
        with transaction(self.db):
            self.db.execute("DELETE FROM wakes WHERE created<? AND status NOT IN ('scheduled','queued','running')", (now - 86400,))
            self._move(WakeStatus.EXPIRED, "expires<=? AND status IN ('scheduled','queued','running')", (now,))
            for row in self.db.execute("SELECT * FROM wakes WHERE status='scheduled' AND due<=? ORDER BY due", (now,)).fetchall():
                key = str(row["scope"])
                state = self.scopes.state(key)
                if state["version"] != row["version"] or state["mode"] == "paused":
                    self._move(WakeStatus.CANCELLED, "id=?", (row["id"],))
                    continue
                try:
                    self._check_trigger_source(key, str(row["id"]))
                except PermissionError:
                    self._move(WakeStatus.CANCELLED, "id=?", (row["id"],))
                    continue
                context = self.dialogue_job(str(row["id"]))
                if (context is not None and context["source_seq"] > 0 and not self.messages.retains_seq(key, context["source_seq"])):
                    self._move(WakeStatus.CANCELLED, "id=?", (row["id"],))
                    continue
                busy = self.db.execute("SELECT MAX(expires) FROM wakes WHERE scope=? AND status IN ('queued','running')", (key,)).fetchone()[0]
                if busy is not None:
                    self.db.execute("UPDATE wakes SET due=? WHERE id=?", (min(float(busy), now + 1), row["id"]))
                    continue
                requests = self.db.execute("SELECT at FROM requests WHERE scope=? AND at>? ORDER BY at", (key, now - 3600)).fetchall()
                due = max(now, float(requests[-1][0]) + cooldown if requests else now)
                if len(requests) >= limit:
                    due = max(due, float(requests[-limit][0]) + 3600)
                if due > now:
                    self.db.execute("UPDATE wakes SET due=? WHERE id=?", (due, row["id"]))
                    continue
                changed = self._move(WakeStatus.QUEUED, "id=? AND status='scheduled'", (row["id"],), requested=now)
                if changed:
                    self.db.execute("INSERT INTO requests VALUES(?,?)", (key, now))
                    ready.append(WakeJob(
                        id=str(row["id"]), scope=key, version=str(row["version"]),
                        trigger_seq=int(row["trigger_seq"]), status="queued", created=float(row["created"]),
                        expires=float(row["expires"]), turn_id=None, due=now, reason=str(row["reason"]),
                    ))
            self.db.execute("DELETE FROM requests WHERE at<=?", (now - 3600,))
        return ready

    def _check_trigger_source(self, key: str, job: str) -> None:
        row = self.db.execute("SELECT * FROM wakes WHERE id=? AND scope=?", (job, key)).fetchone()
        if row is not None and row["condition_config"] != "{}" and row["condition_config"] != self.scopes.state(key)["wake_config"]:
            raise PermissionError("temporary wake conditions changed")
        if row is None:
            return
        for batch in self.db.execute("SELECT input_meta,decision FROM judge_batches WHERE handoff_job=?", (job,)):
            if batch[0] is None:
                continue
            metadata = json.loads(batch[0])
            sources = json.loads(batch[1])["source_seqs"] if batch[1] else metadata["new_seqs"]
            revisions = {item["seq"]:item["revision"] for item in metadata["excerpts"]}
            for seq in sources:
                message = self.messages.judge_source(key, seq)
                if (message is None or revisions.get(seq) != message[0]
                        or (seq in metadata["new_seqs"] and not json.loads(message[1]).get("eligible"))):
                    raise PermissionError("judged source is no longer retained or changed")

    def release_pending(self, key: str) -> None:
        with transaction(self.db):
            self.db.execute("UPDATE wakes SET due=MIN(due,?) WHERE scope=? AND status='scheduled' AND id NOT IN (SELECT job FROM dialogue_jobs)", (self.now(), key))

    def next_due(self) -> float | None:
        row = self.db.execute("SELECT MIN(at) FROM (SELECT MIN(MIN(due,expires)) AS at FROM wakes "
                              "WHERE status='scheduled' UNION ALL SELECT MIN(wake_at) FROM scopes "
                              "UNION ALL SELECT MIN(MIN(due,expires)) FROM actions WHERE status='queued' "
                              "UNION ALL SELECT MIN(next_check) FROM proactive_state UNION ALL SELECT MIN(next_activity_check) FROM proactive_state "
                              "UNION ALL SELECT MIN(quiet_due) FROM dialogue_waits UNION ALL SELECT MIN(total_due) FROM dialogue_waits "
                              "UNION ALL SELECT MIN(condition_expires) FROM dialogue_waits UNION ALL SELECT MIN(CAST(json_extract(wake_config,'$.expires_at') AS REAL)) FROM scopes WHERE wake_config<>'{}' "
                              "UNION ALL SELECT MIN(pending_due) FROM judge_state WHERE inflight=0)").fetchone()
        return float(row[0]) if row[0] is not None else None

    def _refund(self, row: sqlite3.Row) -> None:
        if row["status"] == "queued" and row["turn_id"] is None and row["requested"] is not None:
            self.db.execute("DELETE FROM requests WHERE rowid=(SELECT rowid FROM requests WHERE scope=? AND at=? LIMIT 1)", (row["scope"], row["requested"]))
            self.db.execute("UPDATE wakes SET requested=NULL WHERE id=?", (row["id"],))

    def recover(self) -> None:
        with transaction(self.db):
            self._move(WakeStatus.CANCELLED, "status IN ('scheduled','queued','running') AND EXISTS (SELECT 1 FROM message_deliveries d WHERE d.scope=wakes.scope AND d.turn_id=wakes.turn_id AND d.status='unknown')", ())
            # Refund even expired queued opportunities before status recovery;
            # no model claimed these reservations before the restart.
            for opportunity in self.db.execute("SELECT * FROM wakes WHERE reason='proactive' AND status='queued'").fetchall():
                self._refund(opportunity)
            # An idle opportunity already claimed by the main model is consumed.
            # Recover only unclaimed events, never ask again after a silent/interrupted run.
            self._move(WakeStatus.CANCELLED, "reason='proactive' AND status='running' AND json_extract(proactive_source,'$.kind') IN ('idle','activity')", ())
            # A durable wait decision is already the end of that run, even if
            # shutdown happened before the runner's completion hook.
            self._move(WakeStatus.COMPLETED, "status='running' AND scope IN (SELECT key FROM scopes WHERE mode IN (?,?))", (ScopeMode.WAITING, ScopeMode.SLEEPING))
            # A model re-awakening on the same old question could otherwise
            # recreate a send whose platform result was lost during shutdown.
            self._move(WakeStatus.CANCELLED, "status IN ('scheduled','queued','running') "
                       "AND EXISTS (SELECT 1 FROM actions WHERE actions.scope=wakes.scope "
                       "AND actions.version=wakes.version AND actions.status='unknown' AND actions.created>=wakes.created)", ())
            self._move(WakeStatus.EXPIRED, "expires<=? AND status IN ('scheduled','queued','running')", (self.now(),))
            rows = self.db.execute( "SELECT * FROM wakes WHERE status IN ('scheduled','queued','running') ORDER BY created DESC", ).fetchall()
            seen: set[str] = set()
            for row in rows:
                status = WakeStatus.CANCELLED if row["scope"] in seen else WakeStatus.SCHEDULED
                seen.add(row["scope"])
                self._move(status, "id=?", (row["id"],), turn_id=None)
                # A restart must preserve dynamic quiet deadlines and total caps.
                dialogue = self.db.execute("SELECT quiet_due,total_due FROM dialogue_jobs WHERE job=?", (row["id"],)).fetchone()
                if dialogue is None:
                    self.db.execute("UPDATE wakes SET due=? WHERE id=?", (self.now(), row["id"]))
                else:
                    self.db.execute("UPDATE wakes SET due=? WHERE id=?", (min(dialogue[0],dialogue[1]),row["id"]))
            self.db.execute("UPDATE scopes SET mode=? WHERE mode=?", (ScopeMode.OBSERVING, ScopeMode.ACTIVE,))

    def cancel_opportunity(self, job: str) -> sqlite3.Row | None:
        """Caller owns the transaction; an unclaimed slot was not a model call."""
        row = self.db.execute("SELECT * FROM wakes WHERE id=? AND reason='proactive'", (job,)).fetchone()
        if row is not None:
            self._refund(row)
            self._move(WakeStatus.CANCELLED, "id=?", (job,))
        return row

    def wait_for_quiet(self, key: str, turn: str, *, purpose: str, quiet: float,
                       seconds: float | None, on_timeout: bool, sleep_seconds: float | None = None,
                       quiet_due: float | None = None, condition_expires: float | None = None,
                       selective: bool = False, sleep_until: float | None = None,
                       max_delay: float = 12) -> dict[str, object]:
        """Persist a continuation, never a long-running tool or an agent lock."""
        now = self.now()
        shown = self.views.shown_sequences(key, turn)
        after = max(shown, default=0)
        original_source = self.db.execute("SELECT MAX(trigger_seq) FROM wakes WHERE scope=? AND turn_id=? AND status='running'", (key, turn)).fetchone()[0]
        source = int(original_source) if original_source in shown else max(shown, default=0)
        activity = self.db.execute("SELECT * FROM dialogue_state WHERE scope=?", (key,)).fetchone()
        last = float(activity["last_at"]) if activity and activity["last_at"] is not None else now
        total = now + seconds if seconds is not None else None
        if purpose == "reply":
            if total is None:
                raise ValueError("reply waiting must have a finite total limit")
            if not source:
                raise ValueError("reply waiting requires messages actually displayed in this turn")
            original = self.db.execute("SELECT MIN(d.total_due) FROM dialogue_jobs d JOIN wakes w ON w.id=d.job WHERE w.scope=? AND w.turn_id=?", (key, turn)).fetchone()[0]
            if original is not None:
                total = min(total, float(original))
            quiet_due = last + quiet
            if now >= min(quiet_due, total):
                return {"deferred": False, "purpose": purpose, "quiet": now >= quiet_due}
        elif purpose == "messages":
            pending = self.db.execute("SELECT MAX(trigger_seq) FROM wakes WHERE scope=? AND status='scheduled' AND reason='judge' AND trigger_seq>?", (key, after)).fetchone()[0]
            candidate = int(pending) if pending is not None else self.scopes.state(key)["event_seq"] if not selective else 0
            if candidate > after:
                source, quiet_due = candidate, last + quiet
                if total is None:
                    total = now + max_delay
            else:
                quiet_due = None
        with transaction(self.db):
            self.db.execute("INSERT OR REPLACE INTO dialogue_waits VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                            (key, purpose, now, quiet, quiet_due, total, after, source, int(on_timeout), sleep_seconds, condition_expires, sleep_until))
            self.db.execute("UPDATE scopes SET mode=?,wake_at=NULL,wait_after_seq=?,wait_infinite=? WHERE key=?", (ScopeMode.WAITING, after, int(seconds is None), key,))
            # A newer direct mention always survives the decision to wait.
            self._move(WakeStatus.CANCELLED, "scope=? AND status='scheduled' AND reason <> 'judge' AND trigger_seq<=?", (key,after,))
        return {"deferred": True, "purpose": purpose, "mode": "waiting", "total_due": total, "quiet_due": quiet_due}

    def reset_binding(self, key: str, version: str) -> None:
        self._move(WakeStatus.CANCELLED, "scope=? AND version<>? AND status IN ('scheduled','queued','running')", (key, version))
        self.db.execute("UPDATE scopes SET mode=?,wake_at=NULL,wake_config='{}',wait_infinite=0 WHERE key=?", (ScopeMode.OBSERVING, key))
        for table in ("dialogue_state", "dialogue_waits"):
            self.db.execute(f"DELETE FROM {table} WHERE scope=?", (key,))

    def pause(self, key: str) -> None:
        for table in ("dialogue_state", "dialogue_waits"):
            self.db.execute(f"DELETE FROM {table} WHERE scope=?", (key,))
        self.db.execute("UPDATE scopes SET mode=?,wake_at=NULL,wait_infinite=0 WHERE key=?", (ScopeMode.PAUSED, key))
        self._move(WakeStatus.CANCELLED, "scope=? AND status IN ('scheduled','queued','running')", (key,))

    def note_member_activity(self, key: str, seq: int) -> None:
        self.db.execute("INSERT INTO dialogue_state(scope,last_at) VALUES(?,?) ON CONFLICT(scope) DO UPDATE SET last_at=excluded.last_at", (key, self.now()))
        wait = self.wait(key)
        if wait is not None and wait["purpose"] == "reply" and seq > wait["after_seq"]:
            # Continue an already requested answer after quiet, with its original total cap.
            self.db.execute("UPDATE dialogue_waits SET quiet_due=? WHERE scope=?", (self.now()+float(wait["quiet_seconds"]),key))

    def get(self, job: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM wakes WHERE id=?", (job,)).fetchone()

    def admit_view_job(self, key: str, version: str, job: str, turn: str) -> bool:
        return bool(self._move(WakeStatus.RUNNING, "id=? AND scope=? AND status='queued' AND version=? AND expires>?",
                               (job, key, version, self.now()), turn_id=turn))

    def admit_shown(self, key: str, version: str, seqs: list[int], turn: str) -> list[str]:
        admitted: list[str] = []
        for seq in seqs:
            candidates = self.db.execute("SELECT id FROM wakes WHERE scope=? AND version=? AND status='scheduled' AND trigger_seq=? AND expires>?", (key, version, seq, self.now())).fetchall()
            for candidate in candidates:
                try:
                    self._check_trigger_source(key, str(candidate[0]))
                except PermissionError:
                    self._move(WakeStatus.CANCELLED, "id=?", (candidate[0],))
                else:
                    admitted.append(str(candidate[0]))
            self._move(WakeStatus.RUNNING, "scope=? AND version=? AND status='scheduled' AND trigger_seq=? AND expires>?",
                       (key, version, seq, self.now()), turn_id=turn)
        return admitted

    def note_observed(self, key: str, seq: int) -> None:
        self.db.execute("INSERT INTO dialogue_state(scope,observed_at,observed_seq) VALUES(?,?,?) "
                        "ON CONFLICT(scope) DO UPDATE SET observed_at=COALESCE(observed_at,excluded.observed_at),"
                        "observed_seq=MAX(observed_seq,excluded.observed_seq)", (key, self.now(), seq))

    def recent_turns(self, key: str) -> list[sqlite3.Row]:
        return self.db.execute("SELECT id,turn_id FROM wakes WHERE scope=? AND (status='completed' OR (status='running' AND expires>?)) ORDER BY created DESC LIMIT 32", (key, self.now())).fetchall()

    def scheduled(self, key: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT id,reason FROM wakes WHERE scope=? AND status='scheduled'", (key,)).fetchone()

    def attach_judgement(self, job: str, expires: float, condition_config: str, proactive_source: str | None) -> None:
        self.db.execute("UPDATE wakes SET expires=MIN(expires,?),condition_config=?,proactive_source=? WHERE id=?", (expires, condition_config, proactive_source, job))

    def add_dialogue_job(self, job: str, purpose: str, quiet_seconds: float, quiet_due: float,
                         total_due: float, after_seq: int, source_seq: int) -> None:
        self.db.execute("INSERT OR REPLACE INTO dialogue_jobs(job,purpose,quiet_seconds,quiet_due,total_due,after_seq,source_seq) "
                        "VALUES(?,?,?,?,?,?,?)", (job, purpose, quiet_seconds, quiet_due, total_due, after_seq, source_seq))

    def clear_wake_config(self, key: str) -> None:
        self.db.execute("UPDATE scopes SET wake_config='{}' WHERE key=?", (key,))

    def turn_expires(self, turn: str) -> float | None:
        return self.db.execute("SELECT MAX(expires) FROM wakes WHERE turn_id=? AND status='running'", (turn,)).fetchone()[0]

    def schedule_opportunity(self, key: str, version: str, candidate: ProactiveCandidate, now: float) -> None:
        self._insert_scheduled("id,scope,version,trigger_seq,source_seq,status,created,expires,due,reason,proactive_source",
                               "VALUES(?,?,?,0,0,'{status}',?,?,?,'proactive',?)",
                               (uuid4().hex, key, version, now, candidate.expires_at, now, candidate.model_dump_json()))

    def requeue(self, job: str) -> None:
        with transaction(self.db):
            row = self.get(job)
            if row is not None:
                self._refund(row)
            self._move(WakeStatus.SCHEDULED, "id=? AND status='queued'", (job,), due=self.now()+1)

    def note_sent(self, key: str) -> None:
        with transaction(self.db):
            self.db.execute("INSERT INTO dialogue_state(scope,last_sent) VALUES(?,?) ON CONFLICT(scope) DO UPDATE SET observed_at=NULL,observed_seq=0,last_sent=excluded.last_sent", (key,self.now()))

    def wait(self, key: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM dialogue_waits WHERE scope=?", (key,)).fetchone()

    def latest_opportunity(self, key: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT id,status,turn_id,proactive_source FROM wakes WHERE scope=? AND reason='proactive' ORDER BY created DESC LIMIT 1", (key,)).fetchone()

    def has_non_sleep_wait(self, key: str) -> bool:
        return self.db.execute("SELECT 1 FROM dialogue_waits WHERE scope=? AND purpose<>'sleep'", (key,)).fetchone() is not None

    def active_wake(self, key: str, now: float) -> sqlite3.Row | None:
        return self.db.execute("SELECT 1 FROM wakes WHERE scope=? AND status IN ('scheduled','queued','running') AND expires>?", (key, now)).fetchone()

    def request_count(self, key: str, since: float) -> int:
        return self.db.execute("SELECT COUNT(*) FROM requests WHERE scope=? AND at>?", (key, since)).fetchone()[0]

    def active_opportunities(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT id,scope,proactive_source,status FROM wakes WHERE reason='proactive' AND status IN ('scheduled','queued','running')").fetchall()

    def opportunity(self, key: str, job: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT reason,proactive_source,status FROM wakes WHERE id=? AND scope=?", (job, key)).fetchone()

    def turn_opportunities(self, key: str, turn: str) -> list[sqlite3.Row]:
        return self.db.execute("SELECT id FROM wakes WHERE scope=? AND turn_id=? AND reason='proactive'", (key, turn)).fetchall()

    def is_activity_turn(self, key: str | None, turn: str | None) -> bool:
        return self.db.execute("SELECT 1 FROM wakes WHERE scope=? AND turn_id=? AND reason='proactive' AND json_extract(proactive_source,'$.kind')='activity'", (key, turn)).fetchone() is not None

    def running_conditions(self, key: str, job: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT condition_config FROM wakes WHERE id=? AND scope=? AND status='running'", (job, key)).fetchone()

    def request_usage(self, key: str, since: float) -> sqlite3.Row:
        return self.db.execute("SELECT COUNT(*),MAX(at) FROM requests WHERE scope=? AND at>?", (key, since)).fetchone()

    def running_activity(self, key: str | None, turn: str | None) -> sqlite3.Row | None:
        return self.db.execute("SELECT 1 FROM wakes WHERE scope=? AND turn_id=? AND status='running' AND reason='proactive' AND json_extract(proactive_source,'$.kind')='activity'", (key, turn)).fetchone()

    def latest_turn_job(self, scope: str, turn: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT id FROM wakes WHERE scope=? AND turn_id=? ORDER BY created DESC LIMIT 1", (scope, turn)).fetchone()

    def turn_source(self, scope: str, turn_id: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT proactive_source FROM wakes WHERE scope=? AND turn_id=? AND status='running'", (scope, turn_id)).fetchone()
