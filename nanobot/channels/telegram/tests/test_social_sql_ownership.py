import ast
import re
from pathlib import Path


def test_social_sql_ownership():
    root = Path(__file__).resolve().parents[1]
    allowed = {
        "social/db/connection.py", "social/db/schema.py", "social/db/scopes.py",
        "social/db/messages.py", "social/db/views.py", "social/db/stickers.py",
        "social/db/wakes.py", "social/db/judge_state.py", "social/db/deliveries.py",
        "social/db/proactive_state.py",
        # Owner modules of knowledge/learning tables; move under db/ when Phase 6/7 regroups them.
        "social/memory.py", "social/jargon.py", "social/followups.py", "social/activity.py",
        "social/learning.py", "social/expressions.py",
    }
    paths = [*root.joinpath("social").rglob("*.py"), root / "runtime.py",
             root.parents[1] / "agent/tools/telegram_social.py"]
    executors = {path.relative_to(root).as_posix() if path.is_relative_to(root)
                 else "agent/tools/telegram_social.py" for path in paths
                 if any(token in path.read_text() for token in
                        ("db.execute(", "db.executemany(", "db.executescript("))}
    assert executors <= allowed
    learning = ast.parse(root.joinpath("social/learning.py").read_text())
    assert not any(re.search(r"\b(?:FROM|JOIN|UPDATE|INTO)\s+(?:expressions|expression_evidence|jargon|jargon_evidence)\b", node.value, re.I)
                   for call in ast.walk(learning) if isinstance(call, ast.Call)
                   and isinstance(call.func, ast.Attribute) and call.func.attr in {"execute", "executemany", "executescript"}
                   for node in ast.walk(call.args[0]) if isinstance(node, ast.Constant) and isinstance(node.value, str))
