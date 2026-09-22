# Integrating claude-bridge with OpenBot

This document describes how to wire OpenBot's existing agents to the claude-bridge on a Hostinger
(or any) VPS.

## Prerequisites

- OpenBot deployed and reachable at `https://bot.yourdomain.com`
- claude-bridge running on the same VPS (see `docs/architecture.md`)
- One or more Claude Code subscriptions set up via `scripts/setup-subscription.sh`

## Wire OpenBot's LangGraph harness to the bridge

In OpenBot's `.env`:

```env
BOT_PROVIDER=anthropic
ANTHROPIC_BASE_URL=http://claude-bridge:4203
ANTHROPIC_API_KEY=not-used
BOT_MODEL=claude-sonnet-4-5
```

That's it — three lines. OpenBot's `agent-langgraph` (or any harness with `BOT_PROVIDER=anthropic`)
will now route every LLM call to the bridge.

## Add the bridge to OpenBot's docker-compose.yml

Append to `docker-compose.yml`:

```yaml
  claude-bridge:
    build:
      context: ../claude-bridge
      dockerfile: Dockerfile
    image: claude-bridge:latest
    container_name: claude-bridge
    restart: unless-stopped
    network_mode: host
    env_file:
      - ../claude-bridge/.env
    volumes:
      - /etc/claude-pool:/etc/claude-pool:ro
      - /workspace:/workspace
      - bridge-registry:/var/lib/claude-bridge
      - ./claude-bridge-config:/etc/claude-bridge/config:ro
    healthcheck:
      test: ["CMD-SHELL", "curl -fsS http://127.0.0.1:4203/healthz || exit 1"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 10s

volumes:
  bridge-registry:
```

(The exact mount paths depend on your deployment layout.)

## Configure Bot personas

Edit `claude-bridge/config/bots.yaml` (mounted into the container). One entry per OpenBot coworker
that should use Claude Code.

## Smoke test from your phone

1. On the VPS: `docker compose restart` (or restart just `claude-bridge`).
2. From your phone: open `https://bot.yourdomain.com/bot`.
3. Send `Hello`.
4. Verify the response renders — first turn spawns the REPL, ~3-5s startup.
5. Try `/help`, `/clear`, `/model opus`, `/cost` — all should work natively.
6. Close the phone, wait 10 minutes, send another message — should resume via `--resume`.
7. Check `https://bot.yourdomain.com/admin/audit` — actions recorded with bot.id.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| "no credential available" | All subscriptions in cooldown | Wait or add a sub via `setup-subscription.sh` |
| REPL hangs on permission prompt | Tool not in `--allowedTools` | Tighten `allowed_tools` per bot |
| First turn takes 10+s | Claude Code cold-start + MCP connection | Expected; subsequent turns ~2-5s |
| Slash command renders weirdly on phone | TUI picker (`/resume`, `/permissions`) | Text capture only; no arrow keys in v1 |
| Response truncated | `TURN_TIMEOUT_SECONDS` too low | Raise it; defaults to 120s |