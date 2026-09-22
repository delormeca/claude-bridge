# Deployment runbook for the OpenBot × Claude Code CLI stack on a Hostinger (or any) VPS.

This document covers the end-to-end deployment of:
- OpenBot (CopilotKit AI coworker stack)
- claude-bridge (our HTTP service wrapping persistent Claude Code REPLs)
- Cloudflare Tunnel (no public ports opened on the VPS)

## Prerequisites

- Hostinger VPS (KVM 2 or larger) running Ubuntu 24.04 LTS
- Root or sudo access
- A domain you control, with DNS pointed to Cloudflare (free plan is fine)
- One or more Claude Code subscriptions (Claude Pro / Max / Team / Enterprise)
- The `claude-bridge` repo available (this repo) on the VPS

## TL;DR (10 minutes on the VPS)

```bash
# 1. SSH in
ssh root@your-vps.example.com

# 2. Get the bridge code on the VPS (pick one)
# Option A: git clone (after pushing to your own git remote)
git clone https://github.com/YOUR-ORG/claude-bridge.git /opt/claude-bridge

# Option B: scp from your laptop
# scp -r ./claude-bridge root@your-vps.example.com:/opt/claude-bridge

# 3. Run the deploy script
cd /opt/claude-bridge
sudo bash scripts/deploy-vps.sh
```

The script will:
1. Harden the VPS (fail2ban, UFW, unattended-upgrades, swap)
2. Install Docker + Bun
3. Install Claude Code CLI (pinned version)
4. Clone OpenBot into `/opt/openbot`
5. Configure OpenBot's `.env` (prompts for secrets)
6. Build the claude-bridge image
7. Wire the bridge into OpenBot's `docker-compose.yml`
8. Install `cloudflared` and prompt for tunnel name + hostname
9. Start the full stack
10. Smoke-test the endpoints

After the script finishes, follow the on-screen NEXT STEPS for the interactive parts:
- Authenticate Claude Code subscriptions
- Edit `/etc/claude-bridge/bots.yaml`
- Sign in to OpenBot from your phone

## Detailed walkthrough

### 1. SSH into the VPS

```bash
ssh root@your-vps.hostinger.com
```

If you used `ssh-keygen -t ed25519` on your Mac and pasted the public key:
```bash
# On your Mac
ssh-copy-id -i ~/.ssh/id_ed25519.pub root@your-vps.hostinger.com
```

### 2. Place the bridge code on the VPS

**Option A — git clone (after you push):**
```bash
git clone https://github.com/YOUR-ORG/claude-bridge.git /opt/claude-bridge
```

**Option B — scp from your Mac:**
```bash
# On your Mac (from the pipeline monorepo root)
scp -r ./claude-bridge root@your-vps.hostinger.com:/opt/
```

**Option C — inline via stdin:**
```bash
# On your Mac
tar czf - -C ~/Documents/pipeline claude-bridge | ssh root@your-vps.hostinger.com "tar xzf - -C /opt"
```

### 3. Run the deploy script

```bash
ssh root@your-vps.hostinger.com
cd /opt/claude-bridge
sudo bash scripts/deploy-vps.sh
```

The script is **interactive at three points**:

1. **OpenBot secrets** — `KEY_ENCRYPTION_KEY`, `BETTER_AUTH_SECRET`, `INTELLIGENCE_API_KEY`, `INITIAL_ADMIN_EMAILS`, `BOT_PROVIDER`, `BOT_MODEL`
2. **Cloudflare Tunnel login** — opens a URL in your browser, paste back the cert
3. **Tunnel name + hostname** — e.g. `openbot` for the tunnel and `bot.yourdomain.com` for the hostname

If you need to generate fresh secrets:
```bash
openssl rand -base64 32   # for KEY_ENCRYPTION_KEY and BETTER_AUTH_SECRET
# For INTELLIGENCE_API_KEY, run on any machine:
npx --yes copilotkit@latest login
npx --yes copilotkit@latest project select
```

### 4. Authenticate Claude Code subscriptions

For each subscription you want to use:
```bash
sudo bash /opt/claude-bridge/scripts/setup-subscription.sh sub-1
```

The script opens an OAuth flow in your terminal (it tries to launch a browser; if you're SSHed in, you may need to copy the URL and paste it into a browser on your laptop).

Repeat for `sub-2`, `sub-3`, etc. as needed.

### 5. Configure your Bots

Edit `/etc/claude-bridge/bots.yaml`:
```bash
sudo vim /etc/claude-bridge/bots.yaml
```

One entry per OpenBot coworker that should use Claude Code:
```yaml
bots:
  - id: sales-researcher
    persona_file: personas/sales-researcher.md
    allowed_tools: [Read, Bash, WebFetch, mcp__playwright__*]
    mcp_config: mcps/sales-researcher.json
    default_model: claude-sonnet-4-5
    workspace: /workspace/sales-researcher

  - id: code-reviewer
    persona_file: personas/code-reviewer.md
    allowed_tools: [Read, Grep, Glob, "Bash(git *)"]
    default_model: claude-opus-4-1
```

Persona files and MCP configs live under `/etc/claude-bridge/personas/` and `/etc/claude-bridge/mcps/`. The script created the example at `/etc/claude-bridge/personas/example.md` — copy or replace it.

After editing, restart the bridge:
```bash
sudo docker compose -f /opt/openbot/docker-compose.yml restart claude-bridge
```

### 6. Sign in to OpenBot from your phone

Open `https://bot.yourdomain.com` in your phone browser.

You'll need to configure an OAuth identity provider (Google / Microsoft / Okta) before anyone else can sign in. See OpenBot's [Sign in docs](https://github.com/CopilotKit/openbot/blob/main/README.md#sign-in) for the variables (`GOOGLE_OAUTH_CLIENT_ID` etc.).

For initial testing, you can keep `OPENBOT_SINGLE_USER=true` to skip sign-in.

### 7. Smoke test from your phone

In the chat panel:
- Type `/help` — should display Claude Code's slash command help
- Type `/model opus` — should switch model
- Type `/clear` — should clear the conversation
- Type `/cost` — should show usage
- Type a free-text prompt — should get a real Claude response

### 8. Audit + observability

- `/admin/audit` — every Bot action, with the rule that permitted or refused it
- `curl http://localhost:4203/pool/health | jq` — credential pool state
- `curl http://localhost:4203/sessions | jq` — active session count
- `sudo docker compose -f /opt/openbot/docker-compose.yml logs -f claude-bridge` — bridge logs

## Re-running / updating

`deploy-vps.sh` is **idempotent** — running it again skips completed steps and re-applies the ones you want to update.

To pull a new version of the bridge:
```bash
ssh root@your-vps.hostinger.com
cd /opt/claude-bridge
git pull   # or rsync from your laptop
sudo bash scripts/deploy-vps.sh
```

It will re-build the bridge image and restart services, without touching the credential pool or OpenBot's data.

## Adding a new subscription later

```bash
sudo bash /opt/claude-bridge/scripts/setup-subscription.sh sub-N+1
```

The bridge picks it up on the next session spawn (or you can `docker compose restart claude-bridge` to force re-discovery).

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `https://bot.yourdomain.com` is unreachable | Cloudflare Tunnel not running | `sudo systemctl status cloudflared-openbot` |
| OpenBot loads but `/bot` errors | OpenBot env not configured | Re-run `deploy-vps.sh` and re-fill prompts |
| `/v1/messages` returns "no credential available" | All subs in cooldown or none set up | Wait for cooldown or run `setup-subscription.sh` |
| First turn takes 10s | Claude Code cold-start | Expected; subsequent turns ~2-5s |
| `permission_denied` errors in audit | Bot tried to use a tool not in `--allowedTools` | Tighten the bot's `allowed_tools` list |
| `claude auth login` doesn't open a browser on VPS | Headless | Copy the OAuth URL from the terminal into a browser on your laptop |

## Deleting everything

```bash
sudo docker compose -f /opt/openbot/docker-compose.yml down -v
sudo rm -rf /opt/openbot /opt/claude-bridge /etc/claude-pool /etc/claude-bridge /var/lib/claude-bridge /workspace
sudo systemctl disable --now cloudflared-openbot
sudo rm /etc/systemd/system/cloudflared-openbot.service
sudo cloudflared tunnel delete openbot
```