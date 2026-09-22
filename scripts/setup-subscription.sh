#!/usr/bin/env bash
# Setup a single Claude Code subscription on the host.
#
# Usage: sudo ./setup-subscription.sh <sub-id>
# Example: sudo ./setup-subscription.sh sub-1
#
# The subscription is a directory under POOL_DIR (default /etc/claude-pool) containing
# Claude Code's per-subscription config dir (CLAUDE_CONFIG_DIR). After this runs once,
# the bridge uses that directory to authenticate REPLs.

set -euo pipefail

SUB_ID="${1:-}"
POOL_DIR="${POOL_DIR:-/etc/claude-pool}"

if [ -z "$SUB_ID" ]; then
  echo "Usage: $0 <sub-id>" >&2
  echo "  POOL_DIR override: POOL_DIR=/some/path $0 sub-1" >&2
  exit 1
fi

if ! command -v claude >/dev/null 2>&1; then
  echo "Error: 'claude' CLI is not installed. Install with:" >&2
  echo "  curl -fsSL https://claude.ai/install.sh | bash" >&2
  exit 1
fi

CONFIG_DIR="$POOL_DIR/$SUB_ID"

if [ -d "$CONFIG_DIR/.claude" ] && [ -f "$CONFIG_DIR/.claude/.credentials.json" ]; then
  echo "Subscription '$SUB_ID' is already set up at $CONFIG_DIR" >&2
  echo "  Re-running 'claude auth login' will replace the credentials." >&2
fi

mkdir -p "$CONFIG_DIR"

echo "Setting up Claude Code subscription '$SUB_ID' at $CONFIG_DIR"
echo "Follow the OAuth prompt. When done, Claude Code will save credentials there."
echo ""

CLAUDE_CONFIG_DIR="$CONFIG_DIR" claude auth login

echo ""
echo "Verifying..."
CLAUDE_CONFIG_DIR="$CONFIG_DIR" claude auth status || true
echo ""
echo "OK. claude-bridge can now use this subscription as $SUB_ID."