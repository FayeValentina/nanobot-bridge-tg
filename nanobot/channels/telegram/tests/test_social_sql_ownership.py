from pathlib import Path


def test_social_sql_ownership():
    root = Path(__file__).resolve().parents[1]
    allowed = {
        "social/db/connection.py", "social/db/schema.py", "social/db/scopes.py",
        "social/db/messages.py", "social/db/views.py", "social/db/stickers.py",
        "social/db/wakes.py", "social/db/judge.py", "social/db/deliveries.py",
        "social/db/proactive_state.py",
        # Owner modules of knowledge/learning tables; move under db/ when Phase 6/7 regroups them.
        "social/memory.py", "social/jargon.py", "social/followups.py", "social/activity.py",
        "social/learning.py", "social/expressions.py",
        # Dynamic candidate queries across expressions/jargon/stickers; Phase 7.
        "social/selection.py",
    }
    paths = [*root.joinpath("social").rglob("*.py"), root / "runtime.py",
             root.parents[1] / "agent/tools/telegram_social.py"]
    executors = {path.relative_to(root).as_posix() if path.is_relative_to(root)
                 else "agent/tools/telegram_social.py" for path in paths
                 if any(token in path.read_text() for token in
                        ("db.execute(", "db.executemany(", "db.executescript("))}
    assert executors <= allowed
