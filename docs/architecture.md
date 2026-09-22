# Architecture

The claude-bridge is a thin HTTP service that wraps a pool of persistent Claude Code REPL processes
and exposes an Anthropic-format HTTP API. It is the integration layer between OpenBot's
LLM-agnostic agents (which speak `/v1/messages` with `ANTHROPIC_BASE_URL`) and one or more
Claude Code OAuth subscriptions on the host.

## Why a bridge

OpenBot's `agent-langgraph` (and the harness system) already supports an Anthropic-base-URL
provider. We don't fork OpenBot — we point its Anthropic traffic at the bridge, and the bridge
forwards each Anthropic request to a real persistent Claude Code REPL.

The bridge is the only thing in the deployment that knows how to invoke Claude Code. It is a
thin translation layer, not a model gateway.

## Core components

```
┌────────────┐      ┌────────────┐      ┌──────────────────┐
│ OpenBot    │ ───► │ claude-brdg│ ───► │ claude REPL #1   │ (CLAUDE_CONFIG_DIR=sub-1)
│  :3001     │      │  :4203     │      │ (own persona)    │
│  agent-lgr │      │            │      └──────────────────┘
│  ph :4201  │      │  registry  │      ┌──────────────────┐
│            │      │  (SQLite)  │ ───► │ claude REPL #2   │ (CLAUDE_CONFIG_DIR=sub-2)
│  ANTHROPIC │      │            │      │ (own persona)    │
│  _BASE_URL │      │  pool      │      └──────────────────┘
│  =bridge   │      │ (rotation) │
└────────────┘      └────────────┘      ┌──────────────────┐
                                       │ claude REPL #3   │ (resumed via --resume)
                                       │ (resumed)        │
                                       └──────────────────┘
```

### 1. Session registry (`src/registry.py`)

SQLite-backed. One row per conversation_id with:
- bot_id
- claude_session_id (populated after first response)
- config_dir (the credential this session is pinned to)
- cwd (Bot's workspace)
- spawned_at, last_active_at, status

Survives bridge restarts. Lazy `--resume` after any restart.

### 2. Credential pool (`src/pool.py`)

Discovers `sub-N` dirs under `CREDENTIAL_POOL_DIR`. Round-robins on session spawn, with
per-subscription cooldowns on rate_limit / overloaded errors. Sessions are pinned to one
credential for their entire lifetime — no mid-session switching.

### 3. PTY runner (`src/pty_runner.py`)

Wraps a single `claude` interactive REPL via `pexpect`. Forwards user input verbatim (text or
slash commands). Waits for the `❯` prompt sentinel to know a turn is complete. Strips TUI
decoration (escape sequences, spinner glyphs, status lines) before returning clean text.

### 4. Session manager (`src/session.py`)

Orchestrates the three above. Public API:
- `create_session(conversation_id, bot_id)` — picks credential, spawns REPL
- `send(conversation_id, text)` — forwards input, returns REPL response, handles REPL crashes
  by respawning with `--resume`
- `close_session(conversation_id)` — graceful shutdown, releases credential

### 5. Anthropic adapter (`src/anthropic_adapter.py`)

Translates `/v1/messages` requests into REPL turns. Last user message → REPL stdin. REPL output →
Anthropic-format response. No streaming in v1.

### 6. Bots config (`src/bots.py`)

`bots.yaml` declares one entry per OpenBot coworker: id, persona file, allowed tools, MCP config,
default model, workspace. The bridge spawns one REPL per conversation_id, configured per its
bot's config.

## Persistence matrix

| Event | REPL survives? | Registry survives? | Resume cost |
|---|---|---|---|
| User closes phone | yes | yes | 0 |
| User offline hours/days | yes | yes | 0 |
| Bridge restart | no | yes | 2-5s |
| VPS reboot | no | yes | 2-5s |
| Network blip / rate-limit | yes (REPL retries) | yes | handled by Claude Code |

## Lifecycle of one turn

```
1. OpenBot harness sends POST /v1/messages
   {metadata: {conversation_id: "abc", bot_id: "sales-researcher"},
    messages: [{role: "user", content: "open news.ycombinator.com"}]}

2. Bridge:
   - Look up session abc in registry (row exists? claude_session_id?)
   - If not, create: pick credential from pool, spawn REPL with persona/tools
   - Get or resume REPL from in-memory cache
   - Forward the user input via PTY (rexpect.sendline)
   - Read until prompt sentinel `❯`
   - Strip TUI decoration
   - Update claude_session_id if we just discovered it
   - Touch last_active_at

3. Bridge returns Anthropic-format response:
   {id: "msg_...", role: "assistant", content: [{type: "text", text: "..."}]}

4. OpenBot renders the text in the chat UI.
```

## Multi-subscription rotation

- `pick()` finds the credential with the lowest active session count, skipping cooldown ones
- Ties broken by oldest `last_used_at`, then alphabetical id
- On rate_limit / overloaded: `report_error(cred_id, error_msg)` → 60-90s cooldown
- Adding a new subscription: drop a `sub-N` dir under POOL_DIR, run `claude auth login` once
  inside it. The pool picks it up on the next `discover_and_sync()`.