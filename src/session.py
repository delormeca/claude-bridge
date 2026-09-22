"""Session manager — orchestrates registry + credential pool + REPL for each conversation.

Per conversation_id, we keep:
  - one row in the SQLite registry (claude_session_id, config_dir, cwd, timestamps, status)
  - one in-memory ClaudeReplSession (live PTY)

Lifecycle:
  - First message for a conversation_id:
    - pick a credential from the pool (round-robin + cooldowns)
    - upsert session row
    - spawn REPL with bot persona/tools
    - capture claude_session_id from REPL output and update registry
  - Subsequent messages:
    - look up session in registry
    - if REPL alive in memory, use it
    - if REPL not in memory but registry has claude_session_id: spawn with --resume
    - if REPL not in memory and no claude_session_id: spawn fresh (shouldn't happen)
  - On REPL crash mid-turn:
    - respawn with --resume using registry's claude_session_id
    - retry the user input
  - On conversation close:
    - send /exit (graceful) or kill, decrement credential active count, delete registry row
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .bots import BotConfig, BotsConfig
from .pool import CredentialPool
from .pty_runner import ClaudeReplError, ClaudeReplSession, TurnResult
from .registry import Registry, SessionRow


log = logging.getLogger(__name__)


class UnknownBotError(Exception):
    """Raised when an OpenBot request references a bot id not in bots.yaml."""


@dataclass
class ManagedSession:
    """In-memory handle for a live session, paired with its registry row."""

    row: SessionRow
    repl: ClaudeReplSession
    credential_id: str


class SessionManager:
    """Manages active REPL sessions in memory, backed by the SQLite registry."""

    def __init__(
        self,
        *,
        registry: Registry,
        pool: CredentialPool,
        bots: BotsConfig,
        turn_timeout_seconds: int = 120,
    ) -> None:
        self.registry = registry
        self.pool = pool
        self.bots = bots
        self.turn_timeout_seconds = turn_timeout_seconds
        # conversation_id → ManagedSession
        self._active: dict[str, ManagedSession] = {}
        self._lock = asyncio.Lock()

    # ---------------- helpers ----------------

    def _bot_or_raise(self, bot_id: str) -> BotConfig:
        bot = self.bots.get(bot_id)
        if bot is None:
            raise UnknownBotError(
                f"unknown bot_id '{bot_id}'. Available: {sorted(self.bots.bots)}"
            )
        return bot

    def _resolve_workspace(self, bot: BotConfig) -> Path:
        ws = Path(bot.workspace)
        ws.mkdir(parents=True, exist_ok=True)
        return ws

    async def _spawn_for_session(
        self,
        *,
        conversation_id: str,
        bot: BotConfig,
        credential_id: str,
        credential_path: str,
        claude_session_id: Optional[str] = None,
    ) -> ClaudeReplSession:
        workspace = self._resolve_workspace(bot)
        repl = ClaudeReplSession(
            bot=bot,
            config_dir=credential_path,
            project_dir_name=bot.id,
            cwd=workspace,
            claude_session_id=claude_session_id,
        )
        await repl.start()
        # Pull claude_session_id from the initial output if we don't have one
        # (this only matters on first spawn; --resume sessions already have one).
        if not claude_session_id:
            # The initial output is in the REPL's buffer but we can't read it from outside;
            # we rely on the first turn's raw_output to surface it.
            pass
        await self.registry.upsert_session(
            conversation_id=conversation_id,
            bot_id=bot.id,
            config_dir=credential_path,
            project_dir_name=bot.id,
            cwd=str(workspace),
            claude_session_id=claude_session_id,
        )
        await self.pool.mark_used(credential_id)
        return repl

    async def _ensure_active(self, conversation_id: str) -> ManagedSession:
        """Get or create the active managed session for a conversation."""
        if conversation_id in self._active and self._active[conversation_id].repl.is_alive:
            return self._active[conversation_id]

        row = await self.registry.get_session(conversation_id)
        if row is not None and self._active.get(conversation_id) is None:
            # We have a registry row but no in-memory REPL — try to resume.
            bot = self._bot_or_raise(row.bot_id)
            credential_id = Path(row.config_dir).name
            log.info(
                "resuming session %s (bot=%s, credential=%s, claude_session_id=%s)",
                conversation_id, row.bot_id, credential_id, row.claude_session_id,
            )
            repl = await self._spawn_for_session(
                conversation_id=conversation_id,
                bot=bot,
                credential_id=credential_id,
                credential_path=row.config_dir,
                claude_session_id=row.claude_session_id,
            )
            self._active[conversation_id] = ManagedSession(
                row=row, repl=repl, credential_id=credential_id,
            )
            return self._active[conversation_id]

        if row is not None:
            return self._active[conversation_id]

        # No registry row — caller must call create_session first.
        raise KeyError(f"no session row for conversation_id={conversation_id}")

    # ---------------- public API ----------------

    async def create_session(self, *, conversation_id: str, bot_id: str) -> SessionRow:
        """Create a new session row, pick a credential, spawn the REPL. Idempotent on existing."""
        async with self._lock:
            if conversation_id in self._active and self._active[conversation_id].repl.is_alive:
                return self._active[conversation_id].row
            existing = await self.registry.get_session(conversation_id)
            if existing is not None:
                # Resume existing
                return await self._ensure_active(conversation_id)

            bot = self._bot_or_raise(bot_id)
            cred = await self.pool.pick()
            if cred is None:
                raise RuntimeError(
                    "no credential available — all subscriptions are in cooldown. "
                    "Add another subscription or wait for the cooldown to expire."
                )
            workspace = self._resolve_workspace(bot)
            # Initial registry row (claude_session_id is None until first response)
            await self.registry.upsert_session(
                conversation_id=conversation_id,
                bot_id=bot.id,
                config_dir=cred.path,
                project_dir_name=bot.id,
                cwd=str(workspace),
                claude_session_id=None,
            )
            repl = await self._spawn_for_session(
                conversation_id=conversation_id,
                bot=bot,
                credential_id=cred.id,
                credential_path=cred.path,
                claude_session_id=None,
            )
            self._active[conversation_id] = ManagedSession(
                row=SessionRow(
                    conversation_id=conversation_id,
                    bot_id=bot.id,
                    claude_session_id=None,
                    config_dir=cred.path,
                    project_dir_name=bot.id,
                    cwd=str(workspace),
                    spawned_at=int(time.time() * 1000),
                    last_active_at=int(time.time() * 1000),
                    status="active",
                ),
                repl=repl,
                credential_id=cred.id,
            )
            return self._active[conversation_id].row

    async def send(
        self, *, conversation_id: str, text: str
    ) -> TurnResult:
        """Forward one user input to the session, return the REPL's response."""
        async with self._lock:
            managed = await self._ensure_active(conversation_id)
            repl = managed.repl
            credential_id = managed.credential_id
        try:
            result = await repl.send_and_await(
                text, timeout_seconds=self.turn_timeout_seconds,
            )
        except ClaudeReplError as e:
            log.warning(
                "REPL error on conv=%s (%s); attempting respawn",
                conversation_id, e,
            )
            # Respawn with --resume using the registry's claude_session_id
            row = await self.registry.get_session(conversation_id)
            sid = row.claude_session_id if row else None
            async with self._lock:
                # Drop the dead REPL and re-spawn.
                await self._active[conversation_id].repl.close()
                bot = self._bot_or_raise(managed.row.bot_id)
                repl = await self._spawn_for_session(
                    conversation_id=conversation_id,
                    bot=bot,
                    credential_id=credential_id,
                    credential_path=managed.row.config_dir,
                    claude_session_id=sid,
                )
                self._active[conversation_id] = ManagedSession(
                    row=managed.row, repl=repl, credential_id=credential_id,
                )
            # Retry once
            result = await repl.send_and_await(
                text, timeout_seconds=self.turn_timeout_seconds,
            )

        # After a successful turn, update claude_session_id if we discovered one.
        await self.registry.touch_session(conversation_id)
        discovered = repl.find_claude_session_id_in_output(result.raw_output)
        if discovered and discovered != managed.row.claude_session_id:
            await self.registry.update_claude_session_id(conversation_id, discovered)
            managed.row.claude_session_id = discovered
            repl.note_claude_session_id(discovered)
        return result

    async def close_session(self, conversation_id: str) -> bool:
        """Close a session: send /exit (graceful), release credential, drop registry row."""
        async with self._lock:
            managed = self._active.pop(conversation_id, None)
            if managed is not None:
                try:
                    await managed.repl.close()
                except Exception:
                    pass
                await self.pool.mark_released(managed.credential_id)
            await self.registry.delete_session(conversation_id)
            return managed is not None

    async def list_sessions(self) -> list[SessionRow]:
        return await self.registry.list_sessions()

    async def get_session(self, conversation_id: str) -> SessionRow | None:
        return await self.registry.get_session(conversation_id)