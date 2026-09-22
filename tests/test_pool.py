"""Unit tests for the credential pool."""
from __future__ import annotations

import asyncio
import tempfile
import time
from pathlib import Path

import pytest

from src.pool import CredentialPool, discover_subscriptions
from src.registry import Registry


@pytest.fixture
async def pool_with_subs():
    """Create a pool directory with 3 valid subscriptions."""
    with tempfile.TemporaryDirectory() as tmp:
        for sub_id in ("sub-1", "sub-2", "sub-3"):
            (Path(tmp) / sub_id / ".claude").mkdir(parents=True)
        # Also create an invalid dir that should be ignored
        (Path(tmp) / "not-a-sub").mkdir()
        (Path(tmp) / "sub-empty").mkdir()  # no .claude inside
        db_path = Path(tmp) / "sessions.db"
        r = Registry(db_path)
        await r.connect()
        pool = CredentialPool(pool_dir=Path(tmp), registry=r, cooldown_seconds=60)
        yield pool, r
        # Cleanup: close any open connections; tempdir is removed automatically.
        await r.close()


@pytest.fixture
async def empty_pool():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "sessions.db"
        r = Registry(db_path)
        await r.connect()
        pool = CredentialPool(pool_dir=Path(tmp), registry=r, cooldown_seconds=60)
        yield pool
        await r.close()


@pytest.mark.asyncio
async def test_discover_filters_invalid_dirs(pool_with_subs):
    pool, _ = pool_with_subs
    subs = discover_subscriptions(pool.pool_dir)
    ids = sorted(s[0] for s in subs)
    assert ids == ["sub-1", "sub-2", "sub-3"]


@pytest.mark.asyncio
async def test_pick_returns_lowest_active(pool_with_subs):
    pool, r = pool_with_subs
    await pool.discover_and_sync()
    # Set sub-1 active=2, sub-2 active=0, sub-3 active=1
    await r.incr_active_sessions("sub-1", 2)
    await r.incr_active_sessions("sub-3", 1)
    cred = await pool.pick()
    assert cred is not None
    assert cred.id == "sub-2"  # lowest active count


@pytest.mark.asyncio
async def test_pick_skips_cooldown(pool_with_subs):
    pool, r = pool_with_subs
    await pool.discover_and_sync()
    # Put sub-2 in cooldown
    future = int(time.time() * 1000) + 60000
    await r.set_credential_cooldown("sub-2", future, "rate_limit")
    cred = await pool.pick()
    assert cred is not None
    assert cred.id in ("sub-1", "sub-3")  # sub-2 excluded


@pytest.mark.asyncio
async def test_pick_returns_none_when_all_in_cooldown(pool_with_subs):
    pool, r = pool_with_subs
    await pool.discover_and_sync()
    future = int(time.time() * 1000) + 60000
    for sub_id in ("sub-1", "sub-2", "sub-3"):
        await r.set_credential_cooldown(sub_id, future, "rate_limit")
    cred = await pool.pick()
    assert cred is None


@pytest.mark.asyncio
async def test_report_error_sets_cooldown(pool_with_subs):
    pool, r = pool_with_subs
    await pool.discover_and_sync()
    await pool.report_error("sub-1", "rate_limit_error")
    cred = await r.get_credential("sub-1")
    assert cred.cooldown_until is not None
    assert cred.cooldown_until > int(time.time() * 1000)
    assert cred.last_error == "rate_limit_error"


@pytest.mark.asyncio
async def test_health_snapshots(pool_with_subs):
    pool, r = pool_with_subs
    await pool.discover_and_sync()
    snaps = await pool.health()
    assert len(snaps) == 3
    for s in snaps:
        assert s.available is True
        assert s.cooldown_remaining_ms == 0
    await pool.report_error("sub-1", "overloaded")
    snaps = await pool.health()
    by_id = {s.id: s for s in snaps}
    assert by_id["sub-1"].available is False
    assert by_id["sub-1"].cooldown_remaining_ms > 0


@pytest.mark.asyncio
async def test_empty_pool_returns_no_subs(empty_pool: CredentialPool):
    """A missing or empty pool dir is valid (no subs discovered)."""
    count = await empty_pool.discover_and_sync()
    assert count == 0
    cred = await empty_pool.pick()
    assert cred is None