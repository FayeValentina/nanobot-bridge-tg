"""A durable notebook for self-directed interests and ongoing activities."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from nanobot.channels.telegram.social.db.connection import transaction

if TYPE_CHECKING:
    from nanobot.channels.telegram.social.store import SocialStore


class ActivityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["list", "start", "progress", "pause", "resume", "complete", "block", "abandon"]
    activity_id: str | None = Field(default=None, min_length=1, max_length=64)
    expected_revision: int | None = Field(default=None, ge=1)
    title: str = Field(default="", max_length=120)
    goal: str = Field(default="", max_length=800)
    motivation: str = Field(default="", max_length=500)
    checkpoint: str = Field(default="", max_length=2000)
    next_step: str = Field(default="", max_length=800)
    artifacts: list[str] = Field(default_factory=list, max_length=8)


class SocialActivity:
    def __init__(self, store: SocialStore) -> None:
        self.store = store
        self.db = store.db

    def context(self, scope: str) -> list[dict[str, object]]:
        rows = self.db.execute("SELECT * FROM activities WHERE scope=? ORDER BY status='active' DESC,updated DESC LIMIT 5",
                               (scope,)).fetchall()
        return [{**dict(row), "artifacts": json.loads(row["artifacts"])} for row in rows]

    def index(self, scope: str) -> list[dict[str, object]]:
        rows = self.db.execute("SELECT id,status,title,next_step FROM activities WHERE scope=? "
                               "ORDER BY status='active' DESC,updated DESC LIMIT 5", (scope,)).fetchall()
        return [dict(row) for row in rows]

    def current(self, scope: str) -> dict[str, object] | None:
        row = self.db.execute("SELECT * FROM activities WHERE scope=? AND status='active'",
                              (scope,)).fetchone()
        return {**dict(row), "artifacts": json.loads(row["artifacts"])} if row is not None else None

    def apply(self, scope: str, turn: str, request: ActivityRequest) -> dict[str, object]:
        if request.action == "list":
            return {"activities": self.context(scope)}
        now = self.store.now()
        with transaction(self.db):
            if request.action == "start":
                if not request.title.strip():
                    raise ValueError("start requires a short title describing what caught your interest")
                self.db.execute("UPDATE activities SET status='paused',revision=revision+1,updated=? "
                                "WHERE scope=? AND status='active'", (now, scope))
                identity = uuid4().hex
                self.db.execute("INSERT INTO activities(id,scope,status,title,goal,motivation,next_step,created,updated) "
                                "VALUES(?,?,'active',?,?,?,?,?,?)", (identity, scope, request.title.strip(), request.goal.strip(),
                                    request.motivation.strip(), request.next_step.strip(), now, now))
            else:
                identity = request.activity_id
                row = self.db.execute("SELECT * FROM activities WHERE id=? AND scope=?", (identity, scope)).fetchone()
                if row is None:
                    raise ValueError("activity is missing in this conversation")
                if request.expected_revision != row["revision"]:
                    raise ValueError("activity changed; list and use its current expected_revision")
                if not request.checkpoint.strip() and request.action not in {"pause", "resume", "abandon", "complete"}:
                    raise ValueError("record a short note in checkpoint so you can remember where you left off")
                if any(not value.strip() or len(value) > 1000 for value in request.artifacts):
                    raise ValueError("artifacts must be nonempty paths or source references of at most 1000 characters")
                status = {"progress": "active", "pause": "paused", "resume": "active", "complete": "completed",
                          "block": "blocked", "abandon": "abandoned"}[request.action]
                if status == "active":
                    self.db.execute("UPDATE activities SET status='paused',revision=revision+1,updated=? "
                                    "WHERE scope=? AND status='active' AND id<>?", (now, scope, identity))
                artifacts = json.dumps(request.artifacts, ensure_ascii=False)
                checkpoint = request.checkpoint.strip() or str(row["checkpoint"])
                next_step = request.next_step.strip() or (str(row["next_step"]) if status in {"active", "paused"} else "")
                if not request.artifacts:
                    artifacts = str(row["artifacts"])
                self.db.execute("UPDATE activities SET revision=revision+1,status=?,checkpoint=?,next_step=?,artifacts=?,"
                                "updated=?,last_turn=? WHERE id=?", (status, checkpoint,
                                    next_step, artifacts, now, turn, identity))
                self.db.execute("INSERT INTO activity_steps VALUES(?,?,?,?,?,?,?,?)", (identity, int(row["revision"]) + 1,
                                turn, status, checkpoint, next_step, artifacts, now))
        saved = self.db.execute("SELECT * FROM activities WHERE id=? AND scope=?", (identity, scope)).fetchone()
        assert saved is not None
        return {"activity": {**dict(saved), "artifacts": json.loads(saved["artifacts"])}}
