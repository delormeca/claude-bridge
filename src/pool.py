"""Credential pool manager — discovers sub-N dirs, rotates with cooldowns, picks one per session spawn.

Each subscription is a directory under CREDENTIAL_POOL_DIR containing a .claude/.credentials.json
from a one-time `CLAUDE_CONFIG_DIR=/etc/claude-pool/sub-N claude auth login`.

A session is PINNED to its chosen credential for its entire lifetime — no mid-session switching.
Rotation only happens when spawning a NEW session.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

from .registry import CredentialRow, Registry


log = logging.getLogger(__name__)


SUB_DIR_PATTERN = re.compile(r"^sub-[a-zA-Z0-9_-]+$")


@dataclass
class CredentialSnapshot:
    """Snapshot of one credential's pool state at pick time."""

    id: str
    path: str
    available: bool
    cooldown_remaining_ms: int
    sessions_active: int
    last_error: str | None
    last_used_at: int | None


def discover_subscriptions(pool_dir: Path) -> list[tuple[str, Path]]:
    """Return [(id, path)] for each sub-N directory under pool_dir.

    A sub is valid if it contains a `.claude` directory (Claude Code's config root).
    """
    if not pool_dir.exists():
        return []
    out: list[tuple[str, Path]] = []
    for entry in sorted(pool_dir.iterdir()):
        if not entry.is_dir():
            continue
        if not SUB_DIR_PATTERN.match(entry.name):
            continue
        if (entry / ".claude").is_dir():
            out.append((entry.name, entry))
    return out


class CredentialPool:
    """Manages a pool of Claude Code subscriptions with round-robin + cooldown rotation."""

    def __init__(self, pool_dir: Path, registry: Registry, cooldown_seconds: int) -> None:
        self.pool_dir = pool_dir
        self.registry = registry
        self.cooldown_seconds = cooldown_seconds
        self._known_subs: list[tuple[str, Path]] = []

    async def discover_and_sync(self) -> int:
        """Discover sub-N dirs and upsert them into the registry. Returns count."""
        subs = discover_subscriptions(self.pool_dir)
        self._known_subs = subs
        # Ensure rows exist for each
        for sub_id, sub_path in subs:
            existing = await self.registry.get_credential(sub_id)
            if existing is None:
                await self.registry.upsert_credential(
                    id_=sub_id, path=str(sub_path), sessions_active=0
                )
            elif existing.path != str(sub_path):
                # Path moved — update but keep cooldowns/active counts.
                await self.registry.upsert_credential(
                    id_=sub_id,
                    path=str(sub_path),
                    cooldown_until=existing.cooldown_until,
                    last_error=existing.last_error,
                    sessions_active=existing.sessions_active,
                    last_used_at=existing.last_used_at,
                )
        return len(subs)

    async def pick(self) -> CredentialRow | None:
        """Pick the next credential for a new session.

        Strategy: among credentials whose cooldown_until is None or in the past, pick the one
        with the lowest sessions_active (then oldest last_used_at). Ties broken by id alphabetically.

        Returns None if no credential is currently available (all in cooldown).
        """
        await self.discover_and_sync()
        creds = await self.registry.list_credentials()
        now_ms = int(time.time() * 1000)
        available: list[CredentialRow] = []
        for c in creds:
            if c.cooldown_until is None or c.cooldown_until <= now_ms:
                available.append(c)
        if not available:
            return None
        # Lowest active count, then oldest last_used, then id
        available.sort(
            key=lambda c: (
                c.sessions_active,
                c.last_used_at if c.last_used_at is not None else 0,
                c.id,
            )
        )
        return available[0]

    async def mark_used(self, credential_id: str) -> None:
        """Increment active count + record last_used."""
        await self.registry.incr_active_sessions(credential_id, 1)
        await self.registry.touch_credential_used(credential_id)

    async def mark_released(self, credential_id: str) -> None:
        """Decrement active count when a session ends."""
        await self.registry.incr_active_sessions(credential_id, -1)

    async def report_error(self, credential_id: str, error: str) -> None:
        """Cooldown a credential after a rate_limit / overloaded error."""
        cooldown_until = int(time.time() * 1000) + (self.cooldown_seconds * 1000)
        await self.registry.set_credential_cooldown(credential_id, cooldown_until, error)
        log.warning(
            "credential_pool: cooldown %s for %ss (%s)",
            credential_id,
            self.cooldown_seconds,
            error,
        )

    async def clear_cooldown(self, credential_id: str) -> None:
        """Force-clear a cooldown (admin endpoint)."""
        await self.registry.set_credential_cooldown(credential_id, None, None)

    async def health(self) -> list[CredentialSnapshot]:
        await self.discover_and_sync()
        creds = await self.registry.list_credentials()
        now_ms = int(time.time() * 1000)
        out: list[CredentialSnapshot] = []
        for c in creds:
            cooldown_remaining = 0
            available = True
            if c.cooldown_until is not None and c.cooldown_until > now_ms:
                cooldown_remaining = c.cooldown_until - now_ms
                available = False
            out.append(
                CredentialSnapshot(
                    id=c.id,
                    path=c.path,
                    available=available,
                    cooldown_remaining_ms=cooldown_remaining,
                    sessions_active=c.sessions_active,
                    last_error=c.last_error,
                    last_used_at=c.last_used_at,
                )
            )
        return out