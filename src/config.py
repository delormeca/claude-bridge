"""Configuration loaded from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    """All runtime configuration. Frozen so we treat it as immutable after startup."""

    bridge_host: str
    bridge_port: int

    credential_pool_dir: Path
    registry_db_path: Path
    credential_cooldown_seconds: int

    bots_config_path: Path
    workspace_base: Path

    turn_timeout_seconds: int
    log_level: str

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            bridge_host=os.getenv("BRIDGE_HOST", "127.0.0.1"),
            bridge_port=int(os.getenv("BRIDGE_PORT", "4203")),
            credential_pool_dir=Path(os.getenv("CREDENTIAL_POOL_DIR", "/etc/claude-pool")),
            registry_db_path=Path(os.getenv("REGISTRY_DB_PATH", "/var/lib/claude-bridge/sessions.db")),
            credential_cooldown_seconds=int(os.getenv("CREDENTIAL_COOLDOWN_SECONDS", "75")),
            bots_config_path=Path(os.getenv("BOTS_CONFIG_PATH", "/etc/claude-bridge/config/bots.yaml")),
            workspace_base=Path(os.getenv("WORKSPACE_BASE", "/workspace")),
            turn_timeout_seconds=int(os.getenv("TURN_TIMEOUT_SECONDS", "120")),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
        )


def get_settings() -> Settings:
    """Cached settings accessor. Call once at startup, then reuse."""
    global _settings
    if _settings is None:
        _settings = Settings.from_env()
    return _settings


_settings: Settings | None = None


def reset_settings_cache() -> None:
    """Test helper: clear the cached settings so from_env re-reads."""
    global _settings
    _settings = None