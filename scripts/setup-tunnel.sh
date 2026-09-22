#!/usr/bin/env bash
# Cloudflare Tunnel setup helper.
#
# Assumes cloudflared is installed (deploy-vps.sh does this) and the user is logged in
# (`cloudflared tunnel login` already done — cert.pem in /etc/cloudflared).
#
# Idempotent: re-running won't recreate what already exists.

set -euo pipefail

CFDIR=/etc/cloudflared

prompt() {
  local var="$1" desc="$2" default="${3:-}"
  local current="${!var:-}"
  local prompt_text="$desc"
  [[ -n "$default" ]] && prompt_text="$desc [$default]"
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

if ! command -v cloudflared >/dev/null 2>&1; then
  echo "cloudflared not installed. Run deploy-vps.sh first." >&2
  exit 1
fi

if [[ ! -f "$CFDIR/cert.pem" ]]; then
  echo "Not logged in to Cloudflare. Running 'cloudflared tunnel login' now."
  cloudflared tunnel login
fi

prompt TUNNEL_NAME "Tunnel name" "openbot"
prompt TUNNEL_HOSTNAME "Public hostname" "bot.example.com"

if [[ ! -f "$CFDIR/${TUNNEL_NAME}.json" ]]; then
  cloudflared tunnel create "$TUNNEL_NAME"
fi

TUNNEL_ID=$(cloudflared tunnel list 2>/dev/null | awk -v t="$TUNNEL_NAME" '$2==t {print $1}')
[[ -z "$TUNNEL_ID" ]] && echo "could not find tunnel id for $TUNNEL_NAME" >&2 && exit 2

cloudflared tunnel route dns "$TUNNEL_NAME" "$TUNNEL_HOSTNAME" || echo "DNS route failed (maybe already exists)" >&2

CREDS_FILE="$CFDIR/${TUNNEL_ID}.json"
cat > "$CFDIR/config.yml" <<EOF
tunnel: $TUNNEL_ID
credentials-file: $CREDS_FILE
ingress:
  - hostname: $TUNNEL_HOSTNAME
    service: http://localhost:3000
  - service: http_status:404
EOF

if [[ ! -f /etc/systemd/system/cloudflared-openbot.service ]]; then
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
fi

systemctl daemon-reload
systemctl enable --now cloudflared-openbot

echo "Tunnel $TUNNEL_NAME running. Public URL: https://${TUNNEL_HOSTNAME}"
echo "Wait ~30s for DNS propagation, then test with: curl -I https://${TUNNEL_HOSTNAME}"