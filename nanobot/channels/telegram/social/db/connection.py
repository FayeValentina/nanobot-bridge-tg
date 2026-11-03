import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path


def open_social_db(root: Path) -> sqlite3.Connection:
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root / "social.sqlite3"
    if path.is_symlink():
        raise ValueError("social database must not be a symlink")
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=FULL")
    return db


@contextmanager
def transaction(db: sqlite3.Connection) -> Generator[None, None, None]:
    """Join the caller's transaction, or open one and commit/rollback it."""
    if db.in_transaction:
        yield
        return
    db.execute("BEGIN")
    try:
        yield
    except BaseException:
        db.rollback()
        raise
    db.commit()
