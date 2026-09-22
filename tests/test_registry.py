"""Unit tests for the registry."""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from src.registry import Registry


@pytest.fixture
async def registry():
    with tempfile.TemporaryDirectory() as tmp:
        r = Registry(Path(tmp) / "sessions.db")
        await r.connect()
        yield r


@pytest.mark.asyncio
async def test_upsert_and_get_session(registry: Registry):
    await registry.upsert_session(
        conversation_id="conv-1",
        bot_id="bot-1",
        config_dir="/etc/claude-pool/sub-1",
        project_dir_name="bot-1",
        cwd="/workspace/bot-1",
    )
    row = await registry.get_session("conv-1")
    assert row is not None
    assert row.conversation_id == "conv-1"
    assert row.bot_id == "bot-1"
    assert row.config_dir == "/etc/claude-pool/sub-1"
    assert row.claude_session_id is None
    assert row.status == "active"


@pytest.mark.asyncio
async def test_update_claude_session_id(registry: Registry):
    await registry.upsert_session(
        conversation_id="conv-2",
        bot_id="bot-2",
        config_dir="/etc/claude-pool/sub-1",
        project_dir_name="bot-2",
        cwd="/workspace/bot-2",
    )
    await registry.update_claude_session_id("conv-2", "abc12345-6789")
    row = await registry.get_session("conv-2")
    assert row.claude_session_id == "abc12345-6789"


@pytest.mark.asyncio
async def test_upsert_preserves_claude_session_id(registry: Registry):
    """Re-upserting a session should NOT wipe out the claude_session_id we already stored."""
    await registry.upsert_session(
        conversation_id="conv-3",
        bot_id="bot-3",
        config_dir="/etc/claude-pool/sub-1",
        project_dir_name="bot-3",
        cwd="/workspace/bot-3",
        claude_session_id="orig-sid",
    )
    # Re-upsert without claude_session_id (e.g. resume path)
    await registry.upsert_session(
        conversation_id="conv-3",
        bot_id="bot-3",
        config_dir="/etc/claude-pool/sub-1",
        project_dir_name="bot-3",
        cwd="/workspace/bot-3",
    )
    row = await registry.get_session("conv-3")
    assert row.claude_session_id == "orig-sid"


@pytest.mark.asyncio
async def test_delete_session(registry: Registry):
    await registry.upsert_session(
        conversation_id="conv-4",
        bot_id="bot-4",
        config_dir="/etc/claude-pool/sub-1",
        project_dir_name="bot-4",
        cwd="/workspace/bot-4",
    )
    await registry.delete_session("conv-4")
    row = await registry.get_session("conv-4")
    assert row is None


@pytest.mark.asyncio
async def test_credential_pool_round_trip(registry: Registry):
    await registry.upsert_credential(
        id_="sub-1", path="/etc/claude-pool/sub-1", sessions_active=2
    )
    creds = await registry.list_credentials()
    assert len(creds) == 1
    assert creds[0].id == "sub-1"
    assert creds[0].sessions_active == 2

    await registry.incr_active_sessions("sub-1", -1)
    creds = await registry.list_credentials()
    assert creds[0].sessions_active == 1


@pytest.mark.asyncio
async def test_credential_cooldown(registry: Registry):
    await registry.upsert_credential(id_="sub-2", path="/etc/claude-pool/sub-2")
    await registry.set_credential_cooldown("sub-2", 9999999999999, "rate_limit")
    cred = await registry.get_credential("sub-2")
    assert cred.cooldown_until == 9999999999999
    assert cred.last_error == "rate_limit"