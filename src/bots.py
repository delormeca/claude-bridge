"""Bot configuration loader — bots.yaml schema and helpers.

A bots.yaml file declares one entry per OpenBot coworker:
    bots:
      - id: sales-researcher
        persona_file: personas/sales-researcher.md
        allowed_tools: [Read, Bash, WebFetch, mcp__playwright__*]
        mcp_config: mcps/sales-researcher.json
        agents: agents/sales-researcher.json
        default_model: claude-sonnet-4-5
        workspace: /workspace/sales-researcher

Paths in bots.yaml are resolved relative to the directory containing bots.yaml.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


log = logging.getLogger(__name__)


@dataclass
class BotConfig:
    id: str
    persona_text: str
    allowed_tools: list[str]
    mcp_config_path: Path | None
    agents_config_path: Path | None
    default_model: str
    workspace: Path
    extra_args: list[str] = field(default_factory=list)

    def claude_args(self, *, claude_session_id: str | None = None) -> list[str]:
        """Build the claude CLI argv (everything after `claude`).

        Includes --resume <sid> if we're continuing a previous session.
        """
        args: list[str] = ["--append-system-prompt", self.persona_text]
        if self.allowed_tools:
            args.extend(["--allowedTools", ",".join(self.allowed_tools)])
        if self.mcp_config_path is not None:
            args.extend(["--mcp-config", str(self.mcp_config_path)])
        if self.agents_config_path is not None:
            # Read the agents JSON and pass it via --agents
            args.extend(["--agents", self.agents_config_path.read_text()])
        args.extend(["--model", self.default_model])
        # bypassPermissions avoids interactive permission prompts that would deadlock the PTY.
        args.extend(["--permission-mode", "bypassPermissions"])
        args.extend(self.extra_args)
        if claude_session_id:
            args.extend(["--resume", claude_session_id])
        return args


@dataclass
class BotsConfig:
    """Loaded bots.yaml with metadata about the source."""

    base_dir: Path
    bots: dict[str, BotConfig]  # keyed by bot id

    def get(self, bot_id: str) -> BotConfig | None:
        return self.bots.get(bot_id)


def load_bots_config(path: Path) -> BotsConfig:
    """Load and validate bots.yaml from `path`.

    Required fields per bot: id. All others are optional with sensible defaults.
    Files referenced by `persona_file` / `mcp_config` / `agents` are resolved relative
    to the directory containing bots.yaml.
    """
    if not path.exists():
        raise FileNotFoundError(f"bots.yaml not found at {path}")
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict) or "bots" not in raw:
        raise ValueError(f"bots.yaml must contain a top-level 'bots' key, got: {raw}")
    base_dir = path.parent.resolve()
    bots: dict[str, BotConfig] = {}
    for entry in raw["bots"]:
        if not isinstance(entry, dict):
            raise ValueError(f"each bot entry must be a mapping, got: {entry}")
        bot_id = entry.get("id")
        if not bot_id or not isinstance(bot_id, str):
            raise ValueError(f"bot entry missing 'id': {entry}")
        if bot_id in bots:
            raise ValueError(f"duplicate bot id '{bot_id}' in {path}")

        persona_text = ""
        persona_file = entry.get("persona_file")
        if persona_file:
            persona_path = (base_dir / persona_file).resolve()
            if not persona_path.exists():
                raise FileNotFoundError(
                    f"bot '{bot_id}' persona_file not found: {persona_path}"
                )
            persona_text = persona_path.read_text()

        mcp_config_path: Path | None = None
        mcp_config = entry.get("mcp_config")
        if mcp_config:
            mcp_path = (base_dir / mcp_config).resolve()
            if not mcp_path.exists():
                raise FileNotFoundError(
                    f"bot '{bot_id}' mcp_config not found: {mcp_path}"
                )
            # Validate it's valid JSON
            import json
            json.loads(mcp_path.read_text())
            mcp_config_path = mcp_path

        agents_config_path: Path | None = None
        agents_file = entry.get("agents")
        if agents_file:
            agents_path = (base_dir / agents_file).resolve()
            if not agents_path.exists():
                raise FileNotFoundError(
                    f"bot '{bot_id}' agents file not found: {agents_path}"
                )
            agents_config_path = agents_path

        default_model = entry.get("default_model", "claude-sonnet-4-5")
        workspace_str = entry.get("workspace")
        if workspace_str:
            workspace = Path(workspace_str)
        else:
            # Default convention: <workspace_base>/<bot_id>
            from .config import get_settings
            workspace = get_settings().workspace_base / bot_id

        allowed_tools = entry.get("allowed_tools", [])
        if not isinstance(allowed_tools, list):
            raise ValueError(
                f"bot '{bot_id}' allowed_tools must be a list, got: {allowed_tools}"
            )

        extra_args = entry.get("extra_args", [])
        if not isinstance(extra_args, list):
            raise ValueError(
                f"bot '{bot_id}' extra_args must be a list, got: {extra_args}"
            )

        bot = BotConfig(
            id=bot_id,
            persona_text=persona_text,
            allowed_tools=allowed_tools,
            mcp_config_path=mcp_config_path,
            agents_config_path=agents_config_path,
            default_model=default_model,
            workspace=workspace,
            extra_args=extra_args,
        )
        bots[bot_id] = bot
        log.info("loaded bot config: %s (model=%s, workspace=%s)", bot_id, default_model, workspace)
    return BotsConfig(base_dir=base_dir, bots=bots)


def reload_bots_config(path: Path) -> BotsConfig:
    """Re-read bots.yaml from disk. Use after editing the file."""
    return load_bots_config(path)