# claude-bridge

A thin HTTP service that wraps a pool of persistent Claude Code REPL processes and exposes an Anthropic-API-compatible HTTP endpoint.

Built for the OpenBot × Claude Code integration on the Hostinger VPS deployment.

## What it does

- Spawns persistent `claude` REPL processes via PTY (one per OpenBot conversation)
- Forwards user input verbatim — text or slash commands (`/help`, `/clear`, `/model`, `/compact`, `/resume`, `/agents`, `/cost`, etc.)
- Translates Anthropic-format HTTP requests into PTY I/O on the right session
- Maintains a SQLite-backed session registry that survives bridge restarts
- Lazy `--resume` after any restart — sessions keep their conversation context
- Rotates across multiple Claude Code subscriptions (one credential per session lifetime)
- Per-Bot customization via `bots.yaml` — persona, tools, MCP servers, sub-agents, model
- Mobile-friendly — REPLs idle cheaply across hours/days between user messages

## Architecture in one diagram

```
OpenBot  ──HTTP──►  claude-bridge  ──PTY──►  persistent claude REPL #1 (sub-1)
   │                    │                       (own persona, tools, MCP, /workspace)
   │                    │                  ──►  persistent claude REPL #2 (sub-2)
   │                    │                       (own persona, tools, MCP, /workspace)
   │                    │                  ──►  ...
   │                    │
   │                    └──SQLite──►  sessions.db (conversation_id → claude_session_id)
   │
   └──Each conversation = one REPL. Persistent across phone-off, bridge restart, VPS reboot.
```

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/v1/messages` | Anthropic-format chat completion (the OpenBot-facing endpoint) |
| `GET` | `/pool/health` | credential pool state per subscription |
| `GET` | `/sessions/{conversation_id}` | session metadata |
| `POST` | `/sessions/{conversation_id}/close` | close session cleanly |
| `GET` | `/bots` | list loaded Bot configs |
| `GET` | `/healthz` | liveness probe |

## Quick start

```bash
# 1. Install Claude Code CLI on the host (one-time)
curl -fsSL https://claude.ai/install.sh | bash

# 2. Set up one or more subscriptions (one-time per sub)
mkdir -p /etc/claude-pool/sub-1
CLAUDE_CONFIG_DIR=/etc/claude-pool/sub-1 claude auth login

# 3. Install Python deps
pip install -r requirements.txt

# 4. Configure Bots
cp config/bots.yaml.example config/bots.yaml
# edit config/bots.yaml

# 5. Run
python -m uvicorn src.main:app --host 127.0.0.1 --port 4203
```

See [docs/architecture.md](docs/architecture.md) for the full design and [docs/integration-openbot.md](docs/integration-openbot.md) for the OpenBot wiring.

## Tests

```bash
pytest tests/
```

## License

MIT