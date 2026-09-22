"""SQLite-backed session + credential registry (async via aiosqlite).

Two tables:
  - sessions: conversation_id → bot_id, claude_session_id, credential dir, cwd, timestamps, status
  - credential_pool: subscription id → path, cooldown_until, last_error, sessions_active

Schema is created on first connect. No external migrations; we add columns by ALTER on startup if needed.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

import aiosqlite


SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    conversation_id    TEXT PRIMARY KEY,
    bot_id              TEXT NOT NULL,
    claude_session_id   TEXT,
    config_dir          TEXT NOT NULL,
    project_dir_name    TEXT NOT NULL,
    cwd                 TEXT NOT NULL,
    spawned_at          INTEGER NOT NULL,
    last_active_at      INTEGER NOT NULL,
    status              TEXT NOT NULL DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS credential_pool (
    id                  TEXT PRIMARY KEY,
    path                TEXT NOT NULL,
    cooldown_until      INTEGER,
    last_error          TEXT,
    sessions_active     INTEGER DEFAULT 0,
    last_used_at        INTEGER
);

CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);
CREATE INDEX IF NOT EXISTS idx_sessions_bot ON sessions(bot_id);
"""


@dataclass
class SessionRow:
    conversation_id: str
    bot_id: str
    claude_session_id: str | None
    config_dir: str
    project_dir_name: str
    cwd: str
    spawned_at: int
    last_active_at: int
    status: str = "active"


@dataclass
class CredentialRow:
    id: str
    path: str
    cooldown_until: int | None
    last_error: str | None
    sessions_active: int
    last_used_at: int | None


class Registry:
    """Async SQLite registry for sessions and the credential pool.

    Single shared connection per process is fine for our scale (low-write, read-heavy).
    We hold aiosqlite's per-call transactions; SQLite handles concurrent reads cleanly.
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    async def connect(self) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(SCHEMA)
            await db.commit()

    async def close(self) -> None:
        """Close any held resources. No persistent connection today, but kept for symmetry."""
        return None

    # ---------------- sessions ----------------

    async def upsert_session(
        self,
        *,
        conversation_id: str,
        bot_id: str,
        config_dir: str,
        project_dir_name: str,
        cwd: str,
        claude_session_id: str | None = None,
        status: str = "active",
    ) -> None:
        now = int(time.time() * 1000)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO sessions (
                    conversation_id, bot_id, claude_session_id, config_dir,
                    project_dir_name, cwd, spawned_at, last_active_at, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    bot_id = excluded.bot_id,
                    claude_session_id = COALESCE(excluded.claude_session_id, sessions.claude_session_id),
                    config_dir = excluded.config_dir,
                    project_dir_name = excluded.project_dir_name,
                    cwd = excluded.cwd,
                    last_active_at = excluded.last_active_at,
                    status = excluded.status
                """,
                (
                    conversation_id,
                    bot_id,
                    claude_session_id,
                    config_dir,
                    project_dir_name,
                    cwd,
                    now,
                    now,
                    status,
                ),
            )
            await db.commit()

    async def get_session(self, conversation_id: str) -> SessionRow | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM sessions WHERE conversation_id = ?", (conversation_id,)
            )
            row = await cur.fetchone()
            await cur.close()
        if not row:
            return None
        return SessionRow(
            conversation_id=row["conversation_id"],
            bot_id=row["bot_id"],
            claude_session_id=row["claude_session_id"],
            config_dir=row["config_dir"],
            project_dir_name=row["project_dir_name"],
            cwd=row["cwd"],
            spawned_at=row["spawned_at"],
            last_active_at=row["last_active_at"],
            status=row["status"],
        )

    async def update_claude_session_id(self, conversation_id: str, claude_session_id: str) -> None:
        now = int(time.time() * 1000)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE sessions SET claude_session_id = ?, last_active_at = ? WHERE conversation_id = ?",
                (claude_session_id, now, conversation_id),
            )
            await db.commit()

    async def touch_session(self, conversation_id: str) -> None:
        now = int(time.time() * 1000)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE sessions SET last_active_at = ? WHERE conversation_id = ?",
                (now, conversation_id),
            )
            await db.commit()

    async def set_session_status(self, conversation_id: str, status: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE sessions SET status = ?, last_active_at = ? WHERE conversation_id = ?",
                (status, int(time.time() * 1000), conversation_id),
            )
            await db.commit()

    async def delete_session(self, conversation_id: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM sessions WHERE conversation_id = ?", (conversation_id,))
            await db.commit()

    async def list_sessions(self, *, status: str | None = None) -> list[SessionRow]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            if status is None:
                cur = await db.execute("SELECT * FROM sessions")
            else:
                cur = await db.execute("SELECT * FROM sessions WHERE status = ?", (status,))
            rows = await cur.fetchall()
            await cur.close()
        return [
            SessionRow(
                conversation_id=r["conversation_id"],
                bot_id=r["bot_id"],
                claude_session_id=r["claude_session_id"],
                config_dir=r["config_dir"],
                project_dir_name=r["project_dir_name"],
                cwd=r["cwd"],
                spawned_at=r["spawned_at"],
                last_active_at=r["last_active_at"],
                status=r["status"],
            )
            for r in rows
        ]

    # ---------------- credential pool ----------------

    async def upsert_credential(
        self, *, id_: str, path: str, cooldown_until: int | None = None,
        last_error: str | None = None, sessions_active: int = 0, last_used_at: int | None = None,
    ) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO credential_pool (id, path, cooldown_until, last_error, sessions_active, last_used_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    path = excluded.path,
                    cooldown_until = excluded.cooldown_until,
                    last_error = excluded.last_error,
                    sessions_active = excluded.sessions_active,
                    last_used_at = excluded.last_used_at
                """,
                (id_, path, cooldown_until, last_error, sessions_active, last_used_at),
            )
            await db.commit()

    async def list_credentials(self) -> list[CredentialRow]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM credential_pool")
            rows = await cur.fetchall()
            await cur.close()
        return [
            CredentialRow(
                id=r["id"],
                path=r["path"],
                cooldown_until=r["cooldown_until"],
                last_error=r["last_error"],
                sessions_active=r["sessions_active"],
                last_used_at=r["last_used_at"],
            )
            for r in rows
        ]

    async def get_credential(self, id_: str) -> CredentialRow | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM credential_pool WHERE id = ?", (id_,))
            row = await cur.fetchone()
            await cur.close()
        if not row:
            return None
        return CredentialRow(
            id=row["id"],
            path=row["path"],
            cooldown_until=row["cooldown_until"],
            last_error=row["last_error"],
            sessions_active=row["sessions_active"],
            last_used_at=row["last_used_at"],
        )

    async def set_credential_cooldown(
        self, id_: str, cooldown_until_ms: int | None, last_error: str | None = None
    ) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE credential_pool SET cooldown_until = ?, last_error = ? WHERE id = ?",
                (cooldown_until_ms, last_error, id_),
            )
            await db.commit()

    async def incr_active_sessions(self, id_: str, delta: int) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE credential_pool SET sessions_active = MAX(0, sessions_active + ?) WHERE id = ?",
                (delta, id_),
            )
            await db.commit()

    async def touch_credential_used(self, id_: str) -> None:
        now = int(time.time() * 1000)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE credential_pool SET last_used_at = ? WHERE id = ?",
                (now, id_),
            )
            await db.commit()