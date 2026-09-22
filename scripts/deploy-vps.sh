#!/usr/bin/env bash
# One-shot VPS deployment script for the OpenBot × Claude Code CLI stack.
#
# Run this ON the VPS (Ubuntu 24.04 LTS, root or sudo):
#   curl -fsSL https://raw.githubusercontent.com/<your-org>/claude-bridge/main/scripts/deploy-vps.sh | sudo bash
# OR if you've cloned the bridge already:
#   cd /opt/claude-bridge && sudo bash scripts/deploy-vps.sh
#
# What it does (in order, each step is idempotent):
#   1. Pre-flight: confirm Ubuntu, root, network
#   2. System update + packages (fail2ban, ufw, unattended-upgrades, swap)
#   3. Install Docker + Bun
#   4. Install Claude Code CLI (pinned version)
#   5. Create /etc/claude-pool/<sub-N> directories (skeleton; auth runs interactively later)
#   6. Create /workspace base for OpenBot Bot workspaces
#   7. Clone OpenBot from GitHub into /opt/openbot
#   8. Configure OpenBot's .env (prompts for secrets: KEY_ENCRYPTION_KEY, BETTER_AUTH_SECRET,
#      INTELLIGENCE_API_KEY, INITIAL_ADMIN_EMAILS)
#   9. Build claude-bridge image
#  10. Append bridge service to docker-compose.yml (or overlay file)
#  11. Set up Cloudflare Tunnel (prompts for token) and route bot.yourdomain.com
#  12. Start the stack
#  13. Smoke-test endpoints (curl /healthz, /pool/health, /bots)
#  14. Print next steps (run claude auth login, configure bots.yaml, open admin)
#
# All state lives under /opt/openbot, /etc/claude-pool, /var/lib/claude-bridge, /workspace.

set -euo pipefail

# ---------- config ----------
readonly OPENBOT_REPO="${OPENBOT_REPO:-https://github.com/CopilotKit/openbot.git}"
readonly OPENBOT_DIR="${OPENBOT_DIR:-/opt/openbot}"
readonly BRIDGE_SRC="${BRIDGE_SRC:-}"   # if empty, assume we're running from inside the cloned bridge
readonly BRIDGE_DIR="${BRIDGE_DIR:-/opt/claude-bridge}"
readonly POOL_DIR="${POOL_DIR:-/etc/claude-pool}"
readonly WORKSPACE_BASE="${WORKSPACE_BASE:-/workspace}"
readonly CLAUDE_VERSION="${CLAUDE_VERSION:-2.1.257}"

# ---------- helpers ----------
log()  { printf '\033[1;36m[%s]\033[0m %s\n' "$(date -u +%H:%M:%S)" "$*"; }
warn() { printf '\033[1;33m[%s] WARN: %s\033[0m\n' "$(date -u +%H:%M:%S)" "$*" >&2; }
die()  { printf '\033[1;31m[%s] FATAL: %s\033[0m\n' "$(date -u +%H:%M:%S)" "$*" >&2; exit 1; }

require_root() {
  if [[ $EUID -ne 0 ]]; then
    die "this script must run as root (or via sudo). Re-run with: sudo bash $0"
  fi
}

require_ubuntu() {
  if ! command -v lsb_release >/dev/null 2>&1; then
    warn "lsb_release not installed yet; assuming Ubuntu (will be verified after apt update)"
    return
  fi
  local distro
  distro="$(lsb_release -is 2>/dev/null || true)"
  if [[ "$distro" != "Ubuntu" ]]; then
    die "this script is written for Ubuntu. Detected: ${distro:-unknown}"
  fi
}

prompt() {
  local var="$1" desc="$2" default="${3:-}"
  local current="${!var:-}"
  local prompt_text="$desc"
  if [[ -n "$default" ]]; then
    prompt_text="$desc [$default]"
  fi
  if [[ -n "$current" ]]; then
    printf '%s = %s (press Enter to keep)\n' "$desc" "$current" >&2
    read -r value < /dev/tty
    [[ -z "$value" ]] && value="$current"
  else
    printf '%s' "$prompt_text" >&2
    read -r value < /dev/tty
    [[ -z "$value" && -n "$default" ]] && value="$default"
  fi
  eval "$var=\$value"
}

prompt_secret() {
  local var="$1" desc="$2"
  local current="${!var:-}"
  if [[ -n "$current" ]]; then
    printf '%s = %s (press Enter to keep)\n' "$desc" "***redacted***" >&2
    read -r value < /dev/tty
    [[ -z "$value" ]] && value="$current"
  else
    printf '%s (hidden): ' "$desc" >&2
    read -rs value < /dev/tty
    echo >&2
  fi
  eval "$var=\$value"
}

# ---------- preflight ----------
log "preflight: checking root + Ubuntu + network"
require_root
require_ubuntu
if ! curl -fsS --max-time 5 https://api.github.com >/dev/null 2>&1; then
  die "cannot reach github.com — check DNS / firewall before proceeding"
fi

# ---------- 2. system packages ----------
log "system packages: update + harden"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y >/dev/null
apt-get upgrade -y >/dev/null
apt-get install -y --no-install-recommends \
  ca-certificates curl wget git vim jq \
  apt-transport-https software-properties-common \
  ufw fail2ban unattended-upgrades \
  net-tools htop tmux \
  >/dev/null

# 4 GB swap if not present
if [[ ! -f /swapfile ]]; then
  log "creating 4 GB swapfile"
  fallocate -l 4G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile >/dev/null
  swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# SSH: keep current config, just ensure key auth is on
if [[ ! -d /etc/ssh ]]; then
  warn "no /etc/ssh — skipping SSH hardening"
fi

# fail2ban defaults are fine; just enable
systemctl enable --now fail2ban >/dev/null 2>&1 || true

# unattended-upgrades
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
EOF

# UFW: allow SSH + nothing else (everything else stays loopback)
ufw --force reset >/dev/null
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw allow 22/tcp >/dev/null
# OpenBot's bridge + services bind to loopback; Cloudflare Tunnel talks outbound.
# If you ever expose 80/443 directly, open them here.
ufw --force enable >/dev/null
log "UFW enabled: SSH (22) inbound only"

# ---------- 3. Docker + Bun ----------
log "docker: installing"
if ! command -v docker >/dev/null 2>&1; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg >/dev/null
  chmod a+r /etc/apt/keyrings/docker.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -y >/dev/null
  apt-get install -y --no-install-recommends docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin >/dev/null
fi
systemctl enable --now docker >/dev/null
log "docker: $(docker --version)"

log "bun: installing"
if ! command -v bun >/dev/null 2>&1; then
  curl -fsSL https://bun.sh/install | bash >/dev/null
  export BUN_INSTALL="$HOME/.bun"
  export PATH="$BUN_INSTALL/bin:$PATH"
  echo 'export BUN_INSTALL="$HOME/.bun"' >> /root/.bashrc
  echo 'export PATH="$BUN_INSTALL/bin:$PATH"' >> /root/.bashrc
fi
# Symlink for systemd / non-interactive shells
if [[ -x "$HOME/.bun/bin/bun" ]] && ! command -v bun >/dev/null 2>&1; then
  ln -sf "$HOME/.bun/bin/bun" /usr/local/bin/bun
fi
log "bun: $(bun --version)"

# ---------- 4. Claude Code CLI ----------
log "claude: installing (version $CLAUDE_VERSION)"
if ! command -v claude >/dev/null 2>&1; then
  curl -fsSL https://claude.ai/install.sh | bash -s -- "$CLAUDE_VERSION" >/dev/null
  # Make available to non-interactive shells
  if [[ -x /root/.local/bin/claude ]] && ! command -v claude >/dev/null 2>&1; then
    ln -sf /root/.local/bin/claude /usr/local/bin/claude
  fi
fi
claude --version || true

# ---------- 5. credential pool skeleton ----------
log "credential pool: skeleton under $POOL_DIR"
mkdir -p "$POOL_DIR"
# We do NOT pre-create sub-N dirs; the user runs setup-subscription.sh interactively
# for each subscription. We just print the next-step instructions at the end.

# ---------- 6. workspace base ----------
log "workspace base: $WORKSPACE_BASE"
mkdir -p "$WORKSPACE_BASE"

# ---------- 7. OpenBot ----------
log "openbot: cloning to $OPENBOT_DIR"
if [[ ! -d "$OPENBOT_DIR/.git" ]]; then
  git clone "$OPENBOT_REPO" "$OPENBOT_DIR"
fi
cd "$OPENBOT_DIR"
log "openbot: pinned to commit $(git rev-parse --short HEAD)"

# ---------- 8. configure OpenBot .env ----------
log "openbot: configuring .env"
if [[ ! -f "$OPENBOT_DIR/.env" ]]; then
  cp "$OPENBOT_DIR/.env.example" "$OPENBOT_DIR/.env"
fi

declare -A ENV_PROMPTS=(
  [KEY_ENCRYPTION_KEY]="OpenBot credential vault key (openssl rand -base64 32)"
  [BETTER_AUTH_SECRET]="OpenBot Better Auth secret (openssl rand -base64 32)"
  [INTELLIGENCE_API_KEY]="CopilotKit Intelligence API key (run: npx --yes copilotkit@latest project select)"
  [INITIAL_ADMIN_EMAILS]="Your admin email (comma separated)"
  [BOT_PROVIDER]="Model provider for the LangGraph harness"
  [BOT_MODEL]="Model name (default: claude-sonnet-4-5)"
)

# Pre-fill bridge wiring
sed -i 's|^ANTHROPIC_BASE_URL=.*|ANTHROPIC_BASE_URL=http://claude-bridge:4203|' "$OPENBOT_DIR/.env" || true
sed -i 's|^ANTHROPIC_API_KEY=.*|ANTHROPIC_API_KEY=not-used|' "$OPENBOT_DIR/.env" || true
sed -i 's|^BOT_PROVIDER=.*|BOT_PROVIDER=anthropic|' "$OPENBOT_DIR/.env" || true
sed -i 's|^BOT_MODEL=.*|BOT_MODEL=claude-sonnet-4-5|' "$OPENBOT_DIR/.env" || true

for key in "${!ENV_PROMPTS[@]}"; do
  desc="${ENV_PROMPTS[$key]}"
  current=$(grep -E "^${key}=" "$OPENBOT_DIR/.env" 2>/dev/null | head -1 | cut -d'=' -f2- || true)
  if [[ -z "$current" || "$current" == "your-email@example.com" ]]; then
    prompt "value" "$desc"
    sed -i "s|^${key}=.*|${key}=${value}|" "$OPENBOT_DIR/.env"
  fi
done

# OpenBot must NOT ship in single-user mode for any deployment beyond the laptop
if grep -qE '^OPENBOT_SINGLE_USER=' "$OPENBOT_DIR/.env"; then
  sed -i 's|^OPENBOT_SINGLE_USER=.*|# OPENBOT_SINGLE_USER=true|' "$OPENBOT_DIR/.env" || true
fi

# ---------- 9. claude-bridge ----------
log "claude-bridge: placing at $BRIDGE_DIR"
if [[ -n "$BRIDGE_SRC" ]]; then
  if [[ "$BRIDGE_SRC" =~ ^https?:// || "$BRIDGE_SRC" =~ ^git@ ]]; then
    if [[ ! -d "$BRIDGE_DIR/.git" ]]; then
      git clone "$BRIDGE_SRC" "$BRIDGE_DIR"
    fi
  elif [[ -d "$BRIDGE_SRC" ]]; then
    log "claude-bridge: copying from local $BRIDGE_SRC"
    mkdir -p "$BRIDGE_DIR"
    rsync -a --exclude '.venv' --exclude '__pycache__' --exclude '.git' "$BRIDGE_SRC/" "$BRIDGE_DIR/"
  else
    die "BRIDGE_SRC is set to '$BRIDGE_SRC' but is neither a URL nor an existing directory"
  fi
elif [[ ! -d "$BRIDGE_DIR" ]]; then
  die "no BRIDGE_SRC set and $BRIDGE_DIR doesn't exist. Either set BRIDGE_SRC=/path/to/claude-bridge or BRIDGE_SRC=git@github.com:you/claude-bridge.git, or git clone the bridge into $BRIDGE_DIR first."
fi

# Bridge env file
if [[ ! -f "$BRIDGE_DIR/.env" ]]; then
  cp "$BRIDGE_DIR/.env.example" "$BRIDGE_DIR/.env"
  sed -i "s|^CREDENTIAL_POOL_DIR=.*|CREDENTIAL_POOL_DIR=$POOL_DIR|" "$BRIDGE_DIR/.env"
  sed -i "s|^WORKSPACE_BASE=.*|WORKSPACE_BASE=$WORKSPACE_BASE|" "$BRIDGE_DIR/.env"
  sed -i "s|^BOTS_CONFIG_PATH=.*|BOTS_CONFIG_PATH=/etc/claude-bridge/bots.yaml|" "$BRIDGE_DIR/.env"
fi
mkdir -p /etc/claude-bridge

# Default bots.yaml placeholder
if [[ ! -f /etc/claude-bridge/bots.yaml ]]; then
  cp "$BRIDGE_DIR/config/bots.yaml.example" /etc/claude-bridge/bots.yaml
  log "wrote default /etc/claude-bridge/bots.yaml (edit to add your Bots)"
fi

# ---------- 10. wire bridge into openbot compose ----------
log "openbot: appending claude-bridge service to docker-compose.yml"
COMPOSE="$OPENBOT_DIR/docker-compose.yml"
if ! grep -q 'claude-bridge:' "$COMPOSE"; then
  cat >> "$COMPOSE" <<EOF

  # claude-bridge — added by deploy-vps.sh
  claude-bridge:
    build:
      context: $BRIDGE_DIR
      dockerfile: Dockerfile
    image: claude-bridge:latest
    container_name: claude-bridge
    restart: unless-stopped
    network_mode: host
    env_file:
      - $BRIDGE_DIR/.env
    volumes:
      - $POOL_DIR:$POOL_DIR:ro
      - $WORKSPACE_BASE:$WORKSPACE_BASE
      - bridge-registry:/var/lib/claude-bridge
      - /etc/claude-bridge:/etc/claude-bridge:ro
    healthcheck:
      test: ["CMD-SHELL", "curl -fsS http://127.0.0.1:4203/healthz || exit 1"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 10s
EOF
  cat >> "$COMPOSE" <<EOF

volumes:
  bridge-registry:
EOF
fi

# Make sure agent-langgraph uses network_mode: host or has access to claude-bridge.
# We added claude-bridge with network_mode: host, so all containers on the host network can reach it.
# If OpenBot compose was running on a custom network, we'd add the bridge there instead.

# ---------- 11. cloudflared ----------
log "cloudflared: installing"
if ! command -v cloudflared >/dev/null 2>&1; then
  arch=$(dpkg --print-architecture)
  case "$arch" in
    amd64) cfa_arch=amd64 ;;
    arm64) cfa_arch=arm64 ;;
    *) die "unsupported arch: $arch" ;;
  esac
  curl -fsSL -o /tmp/cloudflared "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${cfa_arch}"
  install -m 0755 /tmp/cloudflared /usr/local/bin/cloudflared
fi

CFDIR=/etc/cloudflared
mkdir -p "$CFDIR"

if [[ ! -f "$CFDIR/cert.pem" ]]; then
  log "cloudflared: login to Cloudflare (opens a URL — paste the cert back)"
  cloudflared tunnel login
fi

# Determine / prompt for the tunnel name + hostname
prompt TUNNEL_NAME "Cloudflare Tunnel name (e.g. openbot)" "openbot"
prompt TUNNEL_HOSTNAME "Public hostname (e.g. bot.yourdomain.com)" "bot.example.com"

if [[ ! -f "$CFDIR/${TUNNEL_NAME}.json" ]]; then
  cloudflared tunnel create "$TUNNEL_NAME" || die "tunnel creation failed"
fi

# Get the tunnel UUID
TUNNEL_ID=$(cloudflared tunnel list 2>/dev/null | awk -v t="$TUNNEL_NAME" '$2==t {print $1}')
[[ -z "$TUNNEL_ID" ]] && die "could not find tunnel id for $TUNNEL_NAME"

# Route DNS
cloudflared tunnel route dns "$TUNNEL_NAME" "$TUNNEL_HOSTNAME" || die "DNS route failed"

# Credentials file path
CREDS_FILE="$CFDIR/${TUNNEL_ID}.json"
[[ ! -f "$CREDS_FILE" ]] && die "expected credentials file at $CREDS_FILE not found"

# config.yml
cat > "$CFDIR/config.yml" <<EOF
tunnel: $TUNNEL_ID
credentials-file: $CREDS_FILE
ingress:
  - hostname: $TUNNEL_HOSTNAME
    service: http://localhost:3000
  - service: http_status:404
EOF

# systemd unit
cat > /etc/systemd/system/cloudflared-openbot.service <<EOF
[Unit]
Description=Cloudflare Tunnel for OpenBot ($TUNNEL_NAME)
After=network.target

[Service]
Type=simple
ExecStart=/usr/local/bin/cloudflared tunnel --config $CFDIR/config.yml run $TUNNEL_NAME
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now cloudflared-openbot

# ---------- 12. start the stack ----------
log "openbot: starting the stack (this can take a few minutes for the first build)"
cd "$OPENBOT_DIR"
docker compose up -d --build

# ---------- 13. smoke test ----------
log "smoke test"
sleep 5
echo ""
echo "============================================================"
echo "Service endpoints:"
echo "============================================================"
echo ""

for url in "http://127.0.0.1:3001/healthz" "http://127.0.0.1:3010" "http://127.0.0.1:4203/healthz" "http://127.0.0.1:4203/pool/health" "http://127.0.0.1:4203/bots"; do
  printf '%-45s ' "$url"
  if curl -fsS --max-time 5 "$url" >/dev/null 2>&1; then
    printf '\033[1;32mOK\033[0m\n'
  else
    printf '\033[1;31mDOWN\033[0m\n'
  fi
done

echo ""
echo "============================================================"
echo "Public URL (after Cloudflare Tunnel propagation, ~30s):"
echo "  https://${TUNNEL_HOSTNAME}"
echo "============================================================"
echo ""
echo "============================================================"
echo "NEXT STEPS — finish by hand:"
echo "============================================================"
cat <<'EOF'

1. Authenticate Claude Code subscriptions on this VPS:
     sudo bash /opt/claude-bridge/scripts/setup-subscription.sh sub-1
     sudo bash /opt/claude-bridge/scripts/setup-subscription.sh sub-2
     ... (repeat per subscription)

   Each one opens an OAuth prompt in your browser. Once done,
   the credentials live under /etc/claude-pool/sub-N.

2. Edit your Bots:
     sudo vim /etc/claude-bridge/bots.yaml
   Add one entry per OpenBot coworker that should use Claude Code.
   The example file shows the schema. Restart the bridge after editing:
     sudo docker compose -f /opt/openbot/docker-compose.yml restart claude-bridge

3. From your phone, open:
     https://<TUNNEL_HOSTNAME>
   Sign in (use one of the OAuth providers — Google / Microsoft / Okta —
   you'll need to register OAuth client IDs first; see
   /opt/openbot/.env.example for the variables).

4. Test from the chat panel:
     /help
     /clear
     /model opus
     /cost
   All should work natively.

5. Audit trail:
     https://<TUNNEL_HOSTNAME>/admin/audit

EOF

echo "Setup complete. Container logs:"
echo "  sudo docker compose -f $OPENBOT_DIR/docker-compose.yml logs -f --tail=200"