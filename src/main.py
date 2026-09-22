"""FastAPI app — routes for /v1/messages, /pool/health, /sessions, /bots, /healthz.

Startup:
  - Load settings from env
  - Connect SQLite registry (run schema)
  - Discover + sync credential pool
  - Load bots.yaml
  - Build SessionManager (does NOT pre-spawn any sessions; lazy)

Shutdown:
  - Close all live REPLs gracefully
  - Close the registry

Run with:
  python -m uvicorn src.main:app --host 127.0.0.1 --port 4203
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .anthropic_adapter import (
    MessagesRequest,
    MessagesResponse,
    build_response,
    estimate_tokens,
    extract_user_input,
)
from .bots import BotsConfig, load_bots_config, reload_bots_config
from .config import Settings, get_settings
from .pool import CredentialPool
from .pty_runner import ClaudeReplError
from .registry import Registry
from .session import SessionManager, UnknownBotError


# ---- logging ----

def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


log = logging.getLogger("claude-bridge")


# ---- lifespan ----

@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = get_settings()
    configure_logging(settings.log_level)
    log.info("claude-bridge starting on %s:%d", settings.bridge_host, settings.bridge_port)

    registry = Registry(settings.registry_db_path)
    await registry.connect()
    pool = CredentialPool(
        pool_dir=settings.credential_pool_dir,
        registry=registry,
        cooldown_seconds=settings.credential_cooldown_seconds,
    )
    n_subs = await pool.discover_and_sync()
    log.info("credential pool: %d subscription(s) discovered", n_subs)
    if n_subs == 0:
        log.warning(
            "no subscriptions found under %s — run scripts/setup-subscription.sh to add one",
            settings.credential_pool_dir,
        )

    try:
        bots: BotsConfig = load_bots_config(settings.bots_config_path)
    except FileNotFoundError as e:
        log.error("bots config missing: %s", e)
        bots = BotsConfig(base_dir=settings.bots_config_path.parent, bots={})

    manager = SessionManager(
        registry=registry,
        pool=pool,
        bots=bots,
        turn_timeout_seconds=settings.turn_timeout_seconds,
    )

    # Stash on app state for the route handlers
    app.state.settings = settings
    app.state.registry = registry
    app.state.pool = pool
    app.state.bots = bots
    app.state.manager = manager

    log.info("claude-bridge ready: %d bot(s), %d subscription(s)", len(bots.bots), n_subs)
    try:
        yield
    finally:
        log.info("claude-bridge shutting down: closing live sessions")
        # Close all live sessions
        for sid in list(manager._active.keys()):
            try:
                await manager.close_session(sid)
            except Exception as e:
                log.warning("error closing session %s: %s", sid, e)
        log.info("claude-bridge shutdown complete")


# ---- app ----

app = FastAPI(title="claude-bridge", version="0.1.0", lifespan=lifespan)


@app.exception_handler(UnknownBotError)
async def _unknown_bot_handler(request: Request, exc: UnknownBotError) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={"type": "error", "error": {"type": "invalid_request", "message": str(exc)}},
    )


@app.exception_handler(ClaudeReplError)
async def _repl_error_handler(request: Request, exc: ClaudeReplError) -> JSONResponse:
    return JSONResponse(
        status_code=502,
        content={"type": "error", "error": {"type": "upstream_error", "message": str(exc)}},
    )


# ---- routes ----

@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {"status": "ok"}


@app.get("/pool/health")
async def pool_health(request: Request) -> dict[str, Any]:
    pool: CredentialPool = request.app.state.pool
    snaps = await pool.health()
    return {
        "subscriptions": [
            {
                "id": s.id,
                "path": s.path,
                "available": s.available,
                "cooldown_remaining_ms": s.cooldown_remaining_ms,
                "sessions_active": s.sessions_active,
                "last_error": s.last_error,
                "last_used_at": s.last_used_at,
            }
            for s in snaps
        ]
    }


@app.get("/bots")
async def list_bots(request: Request) -> dict[str, Any]:
    bots: BotsConfig = request.app.state.bots
    return {
        "bots": [
            {
                "id": bot.id,
                "default_model": bot.default_model,
                "allowed_tools": bot.allowed_tools,
                "workspace": str(bot.workspace),
                "mcp_config_path": str(bot.mcp_config_path) if bot.mcp_config_path else None,
                "agents_config_path": (
                    str(bot.agents_config_path) if bot.agents_config_path else None
                ),
                "persona_chars": len(bot.persona_text),
            }
            for bot in bots.bots.values()
        ]
    }


@app.get("/sessions")
async def list_sessions(request: Request) -> dict[str, Any]:
    manager: SessionManager = request.app.state.manager
    rows = await manager.list_sessions()
    return {
        "sessions": [
            {
                "conversation_id": r.conversation_id,
                "bot_id": r.bot_id,
                "claude_session_id": r.claude_session_id,
                "config_dir": r.config_dir,
                "status": r.status,
                "spawned_at": r.spawned_at,
                "last_active_at": r.last_active_at,
            }
            for r in rows
        ]
    }


@app.get("/sessions/{conversation_id}")
async def get_session(conversation_id: str, request: Request) -> dict[str, Any]:
    manager: SessionManager = request.app.state.manager
    row = await manager.get_session(conversation_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown conversation_id {conversation_id}")
    return {
        "conversation_id": row.conversation_id,
        "bot_id": row.bot_id,
        "claude_session_id": row.claude_session_id,
        "config_dir": row.config_dir,
        "status": row.status,
        "spawned_at": row.spawned_at,
        "last_active_at": row.last_active_at,
    }


@app.post("/sessions/{conversation_id}/close")
async def close_session(conversation_id: str, request: Request) -> dict[str, Any]:
    manager: SessionManager = request.app.state.manager
    closed = await manager.close_session(conversation_id)
    return {"closed": closed, "conversation_id": conversation_id}


@app.post("/v1/messages")
async def messages(req: MessagesRequest, request: Request) -> MessagesResponse:
    """Anthropic-format chat completion.

    Forward the last user message to the persistent Claude Code REPL for this conversation_id.
    """
    manager: SessionManager = request.app.state.manager
    bots: BotsConfig = request.app.state.bots

    # Determine bot_id: prefer metadata.bot_id, otherwise error if no conversation_id either.
    conversation_id = req.metadata.conversation_id if req.metadata else None
    bot_id = req.metadata.bot_id if req.metadata else None

    if conversation_id is None:
        # No conversation_id means we can't pick a credential or REPL. Treat as a 400.
        raise HTTPException(
            status_code=400,
            detail="metadata.conversation_id is required (the bridge is session-scoped)",
        )

    if bot_id is None:
        # Try to look up the existing session
        existing = await manager.get_session(conversation_id)
        if existing is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    "metadata.bot_id is required for new conversations (existing session not found)"
                ),
            )
        bot_id = existing.bot_id

    # Look up bot config (raises UnknownBotError → 400)
    bot = bots.get(bot_id)
    if bot is None:
        raise UnknownBotError(
            f"unknown bot_id '{bot_id}'. Available: {sorted(bots.bots)}"
        )

    # Create session row if missing
    existing = await manager.get_session(conversation_id)
    if existing is None:
        await manager.create_session(conversation_id=conversation_id, bot_id=bot_id)

    # Extract user input
    try:
        user_input = extract_user_input(req)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not user_input.strip():
        raise HTTPException(status_code=400, detail="user input is empty")

    # Forward to REPL
    result = await manager.send(conversation_id=conversation_id, text=user_input)

    return build_response(
        model=bot.default_model,
        text=result.text,
        input_tokens_estimate=estimate_tokens(user_input),
        output_tokens_estimate=estimate_tokens(result.text),
    )


@app.post("/admin/reload-bots")
async def reload_bots(request: Request) -> dict[str, Any]:
    """Re-read bots.yaml from disk. Live sessions keep their existing config."""
    settings: Settings = request.app.state.settings
    new_bots = reload_bots_config(settings.bots_config_path)
    request.app.state.bots = new_bots
    # Update the SessionManager's reference too
    request.app.state.manager.bots = new_bots
    return {"reloaded": True, "bot_count": len(new_bots.bots)}


@app.post("/admin/credential-cooldown-clear")
async def clear_cooldown(request: Request) -> dict[str, Any]:
    """Force-clear a credential's cooldown. Admin endpoint — wire auth as needed."""
    body = await request.json()
    credential_id = body.get("id")
    if not credential_id:
        raise HTTPException(status_code=400, detail="'id' required")
    pool: CredentialPool = request.app.state.pool
    await pool.clear_cooldown(credential_id)
    return {"cleared": True, "id": credential_id}