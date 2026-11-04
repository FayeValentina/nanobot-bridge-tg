from __future__ import annotations

import sqlite3
from collections.abc import Callable

from nanobot.channels.telegram.social.models import MediaReference


class StickerLibrary:
    def __init__(self, db: sqlite3.Connection, now: Callable[[], float]) -> None:
        self.db, self.now = db, now

    def candidates(self, scope: str, needles: list[str], limit: int) -> list[sqlite3.Row]:
        score = "+".join("(instr(lower(note),?)>0)" for _ in needles) or "0"
        return self.db.execute(f"SELECT * FROM (SELECT id,0 AS revision,note,({score}) AS relevance,"
            "ROW_NUMBER() OVER (PARTITION BY substr(note,1,6) ORDER BY updated DESC,id) AS variety "
            "FROM stickers WHERE scope=? AND trim(note)<>'') ORDER BY relevance DESC,variety,id LIMIT ?", [*needles, scope, limit]).fetchall()

    def row(self, scope: str, entry_id: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM stickers WHERE scope=? AND id=?", (scope, entry_id)).fetchone()

    def collect(self, key: str, ref: MediaReference, note: str | None, limit: int) -> str:
        if ref.kind != "sticker" or ref.animated or ref.video:
            raise ValueError("only static stickers can be collected in this release")
        note = " ".join(note.split()) if note is not None else None
        if note is not None and len(note) > 400:
            raise ValueError("sticker note exceeds 400 characters")
        with self.db:
            exists = self.exists(key, ref.unique_id)
            count = self.db.execute("SELECT COUNT(*) FROM stickers WHERE scope=?", (key,)).fetchone()[0]
            if not exists and count >= limit:
                raise PermissionError("sticker collection limit reached")
            self.db.execute("INSERT INTO stickers(scope,id,media_ref,note,updated) VALUES(?,?,?,?,?) "
                            "ON CONFLICT(scope,id) DO UPDATE SET media_ref=excluded.media_ref,"
                            "note=COALESCE(?,stickers.note),updated=excluded.updated",
                            (key, ref.unique_id, ref.model_dump_json(), note or "", self.now(), note))
        return ref.unique_id

    def get(self, key: str, sticker_id: str) -> MediaReference:
        row = self.db.execute("SELECT media_ref FROM stickers WHERE scope=? AND id=?", (key, sticker_id)).fetchone()
        if row is None:
            raise PermissionError("sticker is not collected in this group/topic")
        return MediaReference.model_validate_json(row[0])

    def import_set(self, key: str, refs: tuple[MediaReference, ...], limit: int) -> int:
        """Import an entire supported set atomically, preserving learned notes and use counts."""
        existing = {row[0] for row in self.db.execute("SELECT id FROM stickers WHERE scope=?", (key,))}
        incoming = {ref.unique_id for ref in refs}
        if len(existing | incoming) > limit:
            raise PermissionError("sticker collection limit reached; no stickers imported")
        with self.db:
            for ref in refs:
                if ref.kind != "sticker" or ref.animated or ref.video:
                    raise ValueError("only static stickers can be imported in this release")
                self.db.execute("INSERT INTO stickers(scope,id,media_ref,note,updated) VALUES(?,?,?,?,?) "
                                "ON CONFLICT(scope,id) DO UPDATE SET media_ref=excluded.media_ref",
                                (key, ref.unique_id, ref.model_dump_json(), "", self.now()))
        return len(incoming - existing)

    def catalog(self, key: str, set_name: str = "") -> list[dict[str, object]]:
        rows = self.db.execute("SELECT id,media_ref,note,uses FROM stickers WHERE scope=? ORDER BY rowid", (key,))
        result: list[dict[str, object]] = []
        for row in rows:
            ref = MediaReference.model_validate_json(row["media_ref"])
            if not set_name or ref.sticker_set.casefold() == set_name.casefold():
                result.append({"sticker_id": row["id"], "note": row["note"], "uses": row["uses"],
                               "emoji": ref.emoji, "set_name": ref.sticker_set})
        return result

    def annotate(self, key: str, notes: dict[str, str]) -> None:
        with self.db:
            for sticker_id, note in notes.items():
                self.db.execute("UPDATE stickers SET note=? WHERE scope=? AND id=?", (note, key, sticker_id))

    def search(self, key: str, query: str) -> list[dict[str, object]]:
        if len(query) > 100:
            raise ValueError("sticker query exceeds 100 characters")
        rows = self.db.execute("SELECT * FROM stickers WHERE scope=? AND "
                               "(instr(lower(note),lower(?))>0 OR instr(media_ref,?)>0) "
                               "ORDER BY updated DESC LIMIT 30", (key, query, query)).fetchall()
        return [{"sticker_id": row["id"], "note": row["note"], "uses": row["uses"],
                 "emoji": MediaReference.model_validate_json(row["media_ref"]).emoji} for row in rows]

    def count_use(self, scope: str, sticker_id: str) -> None:
        self.db.execute("UPDATE stickers SET uses=uses+1,updated=? WHERE scope=? AND id=?", (self.now(), scope, sticker_id))

    def exists(self, scope: str, sticker_id: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT 1 FROM stickers WHERE scope=? AND id=?", (scope, sticker_id)).fetchone()
