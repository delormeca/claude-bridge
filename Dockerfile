FROM python:3.11-slim

WORKDIR /app

# System deps: ca-certificates for HTTPS to api.anthropic.com, git for some MCP tools, curl for the claude CLI installer.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI (official installer). Pinned version is overridden at build time via --build-arg.
ARG CLAUDE_VERSION=stable
RUN if [ "$CLAUDE_VERSION" = "stable" ]; then \
      curl -fsSL https://claude.ai/install.sh | bash; \
    else \
      curl -fsSL https://claude.ai/install.sh | bash -s -- "$CLAUDE_VERSION"; \
    fi

# Ensure claude is on PATH for any user
ENV PATH="/root/.local/bin:${PATH}"

# Python deps first (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App
COPY src/ ./src/
COPY config/ ./config/

# Directories for credential pool (read-only at runtime ideally), registry, and workspaces
RUN mkdir -p /etc/claude-pool /var/lib/claude-bridge /workspace

# Bridge runs as root inside the container because Claude Code CLI requires HOME-writable config dirs
# (CLAUDE_CONFIG_DIR must be inside the home tree or be writable). We'll switch to a user but keep
# HOME set appropriately below.
RUN useradd -m -u 1000 -s /bin/bash bridge
USER bridge
ENV HOME=/home/bridge

EXPOSE 4203

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -fsS http://127.0.0.1:4203/healthz || exit 1

CMD ["python", "-m", "uvicorn", "src.main:app", "--host", "127.0.0.1", "--port", "4203"]