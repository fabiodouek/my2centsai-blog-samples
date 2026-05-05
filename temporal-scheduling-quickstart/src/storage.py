import aiosqlite

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_name TEXT NOT NULL,
    target_url TEXT NOT NULL,
    status_code INTEGER,
    error TEXT,
    elapsed_ms INTEGER NOT NULL,
    detected_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_incidents_target ON incidents(target_name);
"""


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()


async def insert_incident(
    target_name: str,
    target_url: str,
    status_code: int | None,
    error: str | None,
    elapsed_ms: int,
) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO incidents (target_name, target_url, status_code, error, elapsed_ms) "
            "VALUES (?, ?, ?, ?, ?)",
            (target_name, target_url, status_code, error, elapsed_ms),
        )
        await db.commit()
