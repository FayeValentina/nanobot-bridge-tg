from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable

from nanobot.channels.telegram.social.models import MediaReference, ObservedMessage, QuotedMessage


class Messages:
    def __init__(self, db: sqlite3.Connection, now: Callable[[], float]) -> None:
        self.db, self.now = db, now

    def has_message(self, key: str, message_id: int) -> bool:
        return self.db.execute("SELECT 1 FROM messages WHERE scope=? AND (message_id=? OR (reply_to=? AND reply_context IS NOT NULL))", (key, message_id, message_id)).fetchone() is not None

    def media_for_message(self, key: str, message_id: int) -> list[MediaReference]:
        row = self.db.execute("SELECT media_refs FROM messages WHERE scope=? AND message_id=?",
                              (key, message_id)).fetchone()
        if row is not None:
            return [MediaReference.model_validate(ref) for ref in json.loads(row[0])]
        row = self.db.execute("SELECT reply_context FROM messages WHERE scope=? AND reply_to=? "
                              "AND reply_context IS NOT NULL ORDER BY seq DESC LIMIT 1", (key, message_id)).fetchone()
        if row is not None:
            return QuotedMessage.model_validate_json(row[0]).media_refs
        raise PermissionError("media message was not observed in this group/topic")

    def displayed_revisions(self, key: str, seqs: list[int]) -> list[sqlite3.Row]:
        return self.db.execute("SELECT seq,message_id,sender_id,text,is_self,revision FROM messages WHERE scope=? AND seq IN "
                               "(SELECT value FROM json_each(?))", (key, json.dumps(seqs))).fetchall()

    def sequences_between(self, key: str, after: int, through: int) -> list[sqlite3.Row]:
        return self.db.execute("SELECT seq FROM messages WHERE scope=? AND seq>? AND seq<=?", (key, after, through)).fetchall()

    def observation(self, key: str, message_id: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT seq,revision FROM messages WHERE scope=? AND message_id=?", (key, message_id)).fetchone()

    def update_seen(self, channel: str, update_id: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT 1 FROM updates WHERE channel=? AND id=?", (channel, update_id)).fetchone()

    def record_update(self, channel: str, update_id: int) -> None:
        self.db.execute("INSERT INTO updates VALUES(?,?)", (channel, update_id))

    def edit(self, key: str, msg: ObservedMessage, revision: int) -> None:
        self.db.execute("UPDATE messages SET text=?,media=?,media_refs=?,reply_context=?,album_id=?,judge_facts='{}',revision=? "
                        "WHERE scope=? AND message_id=?",
                        (msg.text[:4000], json.dumps(msg.media),
                         json.dumps([ref.model_dump() for ref in msg.media_refs]),
                         msg.reply.model_dump_json() if msg.reply else None, msg.album_id, revision, key, msg.message_id))

    def insert(self, key: str, seq: int, msg: ObservedMessage) -> None:
        self.db.execute("INSERT INTO messages(scope,seq,update_id,message_id,sender_id,display_name,at,text,"
                        "reply_to,media,is_self,media_refs,reply_context,album_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (key, seq, msg.update_id, msg.message_id, msg.sender_id,
                         msg.display_name[:120], msg.timestamp, msg.text[:4000], msg.reply_to,
                         json.dumps(msg.media), int(msg.is_self),
                         json.dumps([ref.model_dump() for ref in msg.media_refs]),
                         msg.reply.model_dump_json() if msg.reply else None, msg.album_id))

    def set_judge_facts(self, key: str, seq: int, msg: ObservedMessage, judge_event: bool, directed: bool) -> None:
        self.db.execute("UPDATE messages SET judge_facts=? WHERE scope=? AND seq=?",
                        (json.dumps({"eligible": judge_event, "directed": directed, "bot_username": msg.bot_username,
                                     "received_at": self.now()}), key, seq))

    def set_revision(self, key: str, message_id: int, revision: int) -> None:
        self.db.execute("UPDATE messages SET revision=? WHERE scope=? AND message_id=?", (revision, key, message_id))

    def prune(self, key: str, through: int) -> None:
        self.db.execute("DELETE FROM messages WHERE scope=? AND seq<=?", (key, through))

    def prune_updates(self, channel: str, before: int) -> None:
        self.db.execute("DELETE FROM updates WHERE channel=? AND id < ?", (channel, before))

    def after(self, key: str, after: int, limit: int) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM messages WHERE scope=? AND seq>? ORDER BY seq LIMIT ?", (key, after, limit)).fetchall()

    def recent(self, key: str, limit: int) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM messages WHERE scope=? ORDER BY seq DESC LIMIT ?", (key, limit)).fetchall()

    def by_seq(self, key: str, seq: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM messages WHERE scope=? AND seq=?", (key, seq)).fetchone()

    def first_seq(self, key: str) -> sqlite3.Row:
        return self.db.execute("SELECT MIN(seq) FROM messages WHERE scope=?", (key,)).fetchone()

    def retains_seq(self, key: str, seq: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT 1 FROM messages WHERE scope=? AND seq=?", (key, seq)).fetchone()

    def judge_source(self, key: str, seq: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT revision,judge_facts FROM messages WHERE scope=? AND seq=?", (key,seq)).fetchone()

    def set_learning_source(self, key: str, message_id: int, source: str, text: str) -> None:
        self.db.execute("UPDATE messages SET learning_source=?,learning_text=? WHERE scope=? AND message_id=?", (source, text, key, message_id))

    def learning_neighbors(self, key: str, seq: int, limit: int) -> list[sqlite3.Row]:
        return self.db.execute("SELECT message_id,revision,sender_id,learning_source,text FROM messages "
                               "WHERE scope=? AND seq<? ORDER BY seq DESC LIMIT ?", (key, seq, limit)).fetchall()

    def learning_text(self, key: str, message_id: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT revision,learning_source,learning_text FROM messages WHERE scope=? AND message_id=?", (key, message_id)).fetchone()

    def by_message_id(self, key: str, message_id: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM messages WHERE scope=? AND message_id=?", (key, message_id)).fetchone()

    def range(self, key: str, after: int, through: int, limit: int) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM messages WHERE scope=? AND seq>? AND seq<=? ORDER BY seq LIMIT ?", (key, after, through, limit)).fetchall()

    def history_through(self, key: str, through: int, limit: int) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM messages WHERE scope=? AND seq<=? ORDER BY seq DESC LIMIT ?", (key, through, limit)).fetchall()

    def self_after(self, key: str, through: int) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM messages WHERE scope=? AND seq>? AND is_self=1 ORDER BY seq LIMIT 3", (key,through)).fetchall()

    def eligible_after(self, key: str, after: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT 1 FROM messages WHERE scope=? AND seq>? AND json_extract(judge_facts,'$.eligible')=1", (key,after)).fetchone()

    def participation(self, key: str) -> list[sqlite3.Row]:
        return self.db.execute("SELECT m.message_id,MAX(m.at) AS at,COALESCE(a.group_id,d.id,CAST(m.message_id AS TEXT)) AS send_group "
                               "FROM messages m LEFT JOIN actions a ON a.scope=m.scope AND a.message_id=m.message_id AND a.status='sent' "
                               "LEFT JOIN message_deliveries d ON d.scope=m.scope AND d.status='sent' "
                               "AND m.message_id IN (SELECT value FROM json_each(d.message_ids)) "
                               "WHERE m.scope=? AND m.is_self=1 GROUP BY m.message_id", (key,)).fetchall()

    def first(self, key: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT seq,at FROM messages WHERE scope=? ORDER BY seq LIMIT 1", (key,)).fetchone()

    def timestamp(self, key: str, seq: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT at FROM messages WHERE scope=? AND seq=?", (key, seq)).fetchone()

    def recent_member_texts(self, key: str) -> list[sqlite3.Row]:
        return self.db.execute("SELECT sender_id,text FROM messages WHERE scope=? AND is_self=0 ORDER BY seq DESC LIMIT 4", (key,)).fetchall()

    def sticker_notes(self, key: str, message_id: int, revision: int) -> list[sqlite3.Row]:
        return self.db.execute("SELECT s.note FROM messages m JOIN json_each(m.media_refs) r "
                               "JOIN stickers s ON s.scope=m.scope AND s.id=json_extract(r.value,'$.unique_id') "
                               "WHERE m.scope=? AND m.message_id=? AND m.revision=? "
                               "AND json_extract(r.value,'$.kind')='sticker' AND trim(s.note)<>'' LIMIT 2", (key, message_id, revision)).fetchall()

    def current_revision(self, key: str, message_id: int, revision: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT 1 FROM messages WHERE scope=? AND message_id=? AND revision=?", (key, message_id, revision)).fetchone()
