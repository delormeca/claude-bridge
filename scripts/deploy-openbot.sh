#!/usr/bin/env bash
# Non-destructive OpenBot + claude-bridge deploy on an existing VPS.
#
# Use this when the VPS already has other services (Docker, Traefik, brain, etc.) and
# you want to add OpenBot + bridge without clobbering existing config.
#
# Differs from deploy-vps.sh:
#   - Skips apt update / install (Docker + Bun + Claude Code already installed)
#   - Skips UFW changes (already configured)
#   - Skips cloudflared install (use Traefik or existing tunnel)
#   - Uses a dedicated Docker network + non-conflicting ports
#   - Adds Traefik dynamic config for the chosen subdomain
#
# Run on the VPS:
#   sudo bash deploy-openbot.sh
#
# Required env vars:
#   SUBDOMAIN       e.g. "bot.delorme.ca" — public hostname for OpenBot
#   KEY_ENCRYPTION_KEY   openssl rand -base64 32
#   BETTER_AUTH_SECRET   openssl rand -base64 32
#   INITIAL_ADMIN_EMAILS your-email@example.com
#   INTELLIGENCE_API_KEY  cpk-... (from `npx copilotkit@latest project select`)
#
# Optional env vars (with defaults):
#   OPENBOT_PORT     13001  (server, was 3001)
#   BRIDGE_PORT      14203  (claude-bridge, was 4203)
#   CREDENTIAL_POOL_DIR  /etc/claude-pool  (subscriptions get authed here)
#   BOTS_CONFIG_PATH    /etc/claude-bridge/bots.yaml
#   REGISTRY_DB_PATH    /var/lib/claude-bridge/sessions.db

set -euo pipefail

# ---------- defaults ----------
OPENBOT_PORT="${OPENBOT_PORT:-13001}"
BRIDGE_PORT="${BRIDGE_PORT:-14203}"
CREDENTIAL_POOL_DIR="${CREDENTIAL_POOL_DIR:-/etc/claude-pool}"
BOTS_CONFIG_PATH="${BOTS_CONFIG_PATH:-/etc/claude-bridge/bots.yaml}"
REGISTRY_DB_PATH="${REGISTRY_DB_PATH:-/var/lib/claude-bridge/sessions.db}"

OPENBOT_REPO="${OPENBOT_REPO:-https://github.com/CopilotKit/openbot.git}"
BRIDGE_REPO="${BRIDGE_REPO:-https://github.com/delormeca/claude-bridge.git}"
OPENBOT_DIR="${OPENBOT_DIR:-/opt/openbot}"
BRIDGE_DIR="${BRIDGE_DIR:-/opt/claude-bridge}"
TRAEFIK_DYNAMIC_DIR="${TRAEFIK_DYNAMIC_DIR:-/root/traefik-dynamic}"

log()  { printf '\033[1;36m[%s]\033[0m %s\n' "$(date -u +%H:%M:%S)" "$*"; }
warn() { printf '\033[1;33m[%s] WARN: %s\033[0m\n' "$(date -u +%H:%M:%S)" "$*" >&2; }
die()  { printf '\033[1;31m[%s] FATAL: %s\033[0m\n' "$(date -u +%H:%M:%S)" "$*" >&2; exit 1; }

prompt() {
  local var="$1" desc="$2"
  local current="${!var:-}"
  if [[ -n "$current" ]]; then
    # Value is already set (via env or prior). Use it without prompting if running non-interactively.
    if [[ -t 0 ]] && [[ -z "${DEPLOY_NONINTERACTIVE:-}" ]]; then
      printf '%s = (set, press Enter to keep): ' "$desc" >&2
      read -r value < /dev/tty
      [[ -z "$value" ]] && value="$current"
    else
      printf '%s = (preset)\n' "$desc" >&2
      value="$current"
    fi
  else
    printf '%s: ' "$desc" >&2
    read -r value < /dev/tty
    [[ -z "$value" ]] && die "$desc is required"
  fi
  eval "$var=\$value"
}

# ---------- preflight ----------
log "preflight"
require_root() {
  if [[ $EUID -ne 0 ]]; then
    die "run as root: sudo bash $0"
  fi
}
require_root

for cmd in docker curl git bun; do
  command -v "$cmd" >/dev/null 2>&1 || die "$cmd not found; install it first"
done

if ! docker info >/dev/null 2>&1; then
  die "docker daemon not running"
fi

# ---------- prompts ----------
log "prompts"
prompt SUBDOMAIN "Public subdomain (e.g. bot.delorme.ca)"
prompt KEY_ENCRYPTION_KEY "KEY_ENCRYPTION_KEY (openssl rand -base64 32)"
prompt BETTER_AUTH_SECRET "BETTER_AUTH_SECRET (openssl rand -base64 32)"
prompt INITIAL_ADMIN_EMAILS "INITIAL_ADMIN_EMAILS (your admin email)"
prompt INTELLIGENCE_API_KEY "INTELLIGENCE_API_KEY (cpk-...) from npx copilotkit@latest project select"

# ---------- clone repos ----------
log "cloning openbot"
if [[ ! -d "$OPENBOT_DIR/.git" ]]; then
  git clone "$OPENBOT_REPO" "$OPENBOT_DIR"
fi
log "cloning claude-bridge"
if [[ ! -d "$BRIDGE_DIR/.git" ]]; then
  git clone "$BRIDGE_REPO" "$BRIDGE_DIR"
fi

# ---------- bot config + credential pool ----------
log "writing /etc/claude-bridge/bots.yaml"
mkdir -p /etc/claude-bridge
if [[ ! -f "$BOTS_CONFIG_PATH" ]]; then
  cat > "$BOTS_CONFIG_PATH" <<EOF
bots:
  - id: example-bot
    persona_file: personas/example.md
    allowed_tools: [Read, Bash, WebFetch]
    mcp_config: mcps/example.json
    default_model: claude-sonnet-4-5
EOF
  cp -r "$BRIDGE_DIR/config/personas" /etc/claude-bridge/personas 2>/dev/null || true
  cp -r "$BRIDGE_DIR/config/mcps" /etc/claude-bridge/mcps 2>/dev/null || true
fi
mkdir -p "$CREDENTIAL_POOL_DIR"
mkdir -p "$(dirname "$REGISTRY_DB_PATH")"

# ---------- bridge env ----------
log "writing bridge .env"
if [[ ! -f "$BRIDGE_DIR/.env" ]]; then
  cp "$BRIDGE_DIR/.env.example" "$BRIDGE_DIR/.env"
fi
sed -i "s|^CREDENTIAL_POOL_DIR=.*|CREDENTIAL_POOL_DIR=$CREDENTIAL_POOL_DIR|" "$BRIDGE_DIR/.env"
sed -i "s|^REGISTRY_DB_PATH=.*|REGISTRY_DB_PATH=$REGISTRY_DB_PATH|" "$BRIDGE_DIR/.env"
sed -i "s|^BOTS_CONFIG_PATH=.*|BOTS_CONFIG_PATH=$BOTS_CONFIG_PATH|" "$BRIDGE_DIR/.env"
sed -i "s|^WORKSPACE_BASE=.*|WORKSPACE_BASE=/workspace|" "$BRIDGE_DIR/.env"
sed -i "s|^BRIDGE_PORT=.*|BRIDGE_PORT=$BRIDGE_PORT|" "$BRIDGE_DIR/.env" || echo "BRIDGE_PORT=$BRIDGE_PORT" >> "$BRIDGE_DIR/.env"

# ---------- openbot .env ----------
log "writing openbot .env"
if [[ ! -f "$OPENBOT_DIR/.env" ]]; then
  cp "$OPENBOT_DIR/.env.example" "$OPENBOT_DIR/.env"
fi
# Replace the values the user gave us
sed -i "s|^KEY_ENCRYPTION_KEY=.*|KEY_ENCRYPTION_KEY=$KEY_ENCRYPTION_KEY|" "$OPENBOT_DIR/.env"
sed -i "s|^BETTER_AUTH_SECRET=.*|BETTER_AUTH_SECRET=$BETTER_AUTH_SECRET|" "$OPENBOT_DIR/.env"
sed -i "s|^INITIAL_ADMIN_EMAILS=.*|INITIAL_ADMIN_EMAILS=$INITIAL_ADMIN_EMAILS|" "$OPENBOT_DIR/.env"
sed -i "s|^INTELLIGENCE_API_KEY=.*|INTELLIGENCE_API_KEY=$INTELLIGENCE_API_KEY|" "$OPENBOT_DIR/.env"
# Wire bridge into openbot
sed -i "s|^ANTHROPIC_BASE_URL=.*|ANTHROPIC_BASE_URL=http://host.docker.internal:$BRIDGE_PORT|" "$OPENBOT_DIR/.env" || \
  echo "ANTHROPIC_BASE_URL=http://host.docker.internal:$BRIDGE_PORT" >> "$OPENBOT_DIR/.env"
sed -i "s|^ANTHROPIC_API_KEY=.*|ANTHROPIC_API_KEY=not-used|" "$OPENBOT_DIR/.env" || \
  echo "ANTHROPIC_API_KEY=not-used" >> "$OPENBOT_DIR/.env"
sed -i "s|^BOT_PROVIDER=.*|BOT_PROVIDER=anthropic|" "$OPENBOT_DIR/.env" || \
  echo "BOT_PROVIDER=anthropic" >> "$OPENBOT_DIR/.env"
sed -i "s|^BOT_MODEL=.*|BOT_MODEL=claude-sonnet-4-5|" "$OPENBOT_DIR/.env" || \
  echo "BOT_MODEL=claude-sonnet-4-5" >> "$OPENBOT_DIR/.env"
# Server on non-conflicting port
sed -i "s|^PORT=.*|PORT=$OPENBOT_PORT|" "$OPENBOT_DIR/.env"
sed -i "s|^SERVER_PORT=.*|SERVER_PORT=$OPENBOT_PORT|" "$OPENBOT_DIR/.env"

# ---------- append bridge to openbot compose ----------
log "appending bridge service to openbot docker-compose.yml"
COMPOSE="$OPENBOT_DIR/docker-compose.yml"
if ! grep -q "claude-bridge:" "$COMPOSE"; then
  cat >> "$COMPOSE" <<EOF

  # claude-bridge (added by deploy-openbot.sh)
  claude-bridge:
    build:
      context: $BRIDGE_DIR
      dockerfile: Dockerfile
    image: claude-bridge:latest
    container_name: openbot-claude-bridge
    restart: unless-stopped
    network_mode: host
    env_file:
      - $BRIDGE_DIR/.env
    volumes:
      - $CREDENTIAL_POOL_DIR:$CREDENTIAL_POOL_DIR:ro
      - /workspace:/workspace
      - openbot-bridge-registry:$REGISTRY_DB_PATH
      - /etc/claude-bridge:/etc/claude-bridge:ro
    healthcheck:
      test: ["CMD-SHELL", "curl -fsS http://127.0.0.1:$BRIDGE_PORT/healthz || exit 1"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 10s

volumes:
  openbot-bridge-registry:
EOF
fi

# ---------- Traefik dynamic config ----------
log "writing Traefik dynamic config for $SUBDOMAIN"
cat > "$TRAEFIK_DYNAMIC_DIR/openbot.yml" <<EOF
http:
  routers:
    openbot:
      rule: 'Host(\`$SUBDOMAIN\`)'
      service: openbot
      entryPoints:
        - websecure
      tls:
        certResolver: mytlschallenge

  services:
    openbot:
      loadBalancer:
        servers:
          - url: http://127.0.0.1:$OPENBOT_PORT
EOF
# Allow Traefik to reach OpenBot
ufw allow from 172.18.0.0/16 to any port $OPENBOT_PORT proto tcp 2>&1 || true

# ---------- start the stack ----------
log "starting openbot + bridge (this can take a few minutes for the first build)"
cd "$OPENBOT_DIR"
docker compose up -d --build

# ---------- smoke test ----------
log "smoke test (waiting for services to start)"
sleep 10
echo ""
echo "============================================================"
echo "Service endpoints (loopback):"
echo "============================================================"
for url in "http://127.0.0.1:$OPENBOT_PORT/api/health" "http://127.0.0.1:3010" "http://127.0.0.1:$BRIDGE_PORT/healthz" "http://127.0.0.1:$BRIDGE_PORT/pool/health" "http://127.0.0.1:$BRIDGE_PORT/bots"; do
  printf '  %-50s ' "$url"
  if curl -fsS --max-time 5 "$url" >/dev/null 2>&1; then
    printf '\033[1;32mOK\033[0m\n'
  else
    printf '\033[1;31mDOWN\033[0m\n'
  fi
done

# Restart Traefik to pick up the new dynamic config
log "restarting Traefik to load $SUBDOMAIN route"
docker restart root-traefik-1 2>&1 || true
sleep 5

echo ""
echo "============================================================"
echo "Public URL (after DNS propagation + ACME):"
echo "  https://$SUBDOMAIN"
echo "============================================================"
echo ""
echo "============================================================"
echo "NEXT STEPS:"
echo "============================================================"
cat <<EOF
1. Authenticate Claude Code subscriptions:
     sudo bash $BRIDGE_DIR/scripts/setup-subscription.sh sub-1
   Repeat for sub-2, sub-3, etc.

2. Edit your Bots:
     sudo vim $BOTS_CONFIG_PATH
   One entry per OpenBot coworker. Restart the bridge after editing:
     sudo docker restart openbot-claude-bridge

3. Configure DNS for $SUBDOMAIN:
   - Point A record at 72.61.64.93
   - Or set up Cloudflare Tunnel

4. Watch logs:
   sudo docker compose -f $OPENBOT_DIR/docker-compose.yml logs -f
EOF
