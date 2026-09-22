"""Unit tests for the bots.yaml loader."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from src.bots import BotConfig, load_bots_config


def test_load_minimal_bot(tmp_path: Path):
    """A bot with only an id should load with sensible defaults."""
    (tmp_path / "bots.yaml").write_text(
        """
bots:
  - id: minimal
"""
    )
    cfg = load_bots_config(tmp_path / "bots.yaml")
    bot = cfg.get("minimal")
    assert bot is not None
    assert bot.id == "minimal"
    assert bot.persona_text == ""
    assert bot.allowed_tools == []
    assert bot.mcp_config_path is None
    assert bot.default_model == "claude-sonnet-4-5"


def test_load_with_persona_and_tools(tmp_path: Path):
    (tmp_path / "bots.yaml").write_text(
        """
bots:
  - id: full
    persona_file: persona.md
    allowed_tools:
      - Read
      - Bash
      - "Bash(git *)"
    default_model: claude-opus-4-1
"""
    )
    (tmp_path / "persona.md").write_text("You are a full bot.\n")
    cfg = load_bots_config(tmp_path / "bots.yaml")
    bot = cfg.get("full")
    assert bot is not None
    assert bot.persona_text == "You are a full bot.\n"
    assert bot.allowed_tools == ["Read", "Bash", "Bash(git *)"]
    assert bot.default_model == "claude-opus-4-1"


def test_load_with_mcp_config(tmp_path: Path):
    (tmp_path / "bots.yaml").write_text(
        """
bots:
  - id: mcpbot
    mcp_config: mcp.json
"""
    )
    mcp = {"mcpServers": {"playwright": {"command": "npx", "args": ["-y", "@playwright/mcp@latest"]}}}
    (tmp_path / "mcp.json").write_text(json.dumps(mcp))
    cfg = load_bots_config(tmp_path / "bots.yaml")
    bot = cfg.get("mcpbot")
    assert bot is not None
    assert bot.mcp_config_path is not None
    assert bot.mcp_config_path.exists()
    # Round-trip the JSON to confirm validity
    json.loads(bot.mcp_config_path.read_text())


def test_load_with_custom_workspace(tmp_path: Path):
    (tmp_path / "bots.yaml").write_text(
        """
bots:
  - id: customws
    workspace: /data/custom-workspace
"""
    )
    cfg = load_bots_config(tmp_path / "bots.yaml")
    bot = cfg.get("customws")
    assert bot is not None
    assert bot.workspace == Path("/data/custom-workspace")


def test_load_duplicate_id_raises(tmp_path: Path):
    (tmp_path / "bots.yaml").write_text(
        """
bots:
  - id: same
  - id: same
"""
    )
    with pytest.raises(ValueError, match="duplicate"):
        load_bots_config(tmp_path / "bots.yaml")


def test_load_missing_persona_file_raises(tmp_path: Path):
    (tmp_path / "bots.yaml").write_text(
        """
bots:
  - id: missing-persona
    persona_file: does-not-exist.md
"""
    )
    with pytest.raises(FileNotFoundError):
        load_bots_config(tmp_path / "bots.yaml")


def test_load_invalid_mcp_json_raises(tmp_path: Path):
    (tmp_path / "bots.yaml").write_text(
        """
bots:
  - id: bad-mcp
    mcp_config: bad.json
"""
    )
    (tmp_path / "bad.json").write_text("not json {{{")
    with pytest.raises(json.JSONDecodeError):
        load_bots_config(tmp_path / "bots.yaml")


def test_load_missing_id_raises(tmp_path: Path):
    (tmp_path / "bots.yaml").write_text(
        """
bots:
  - persona_file: nope.md
"""
    )
    with pytest.raises(ValueError, match="missing 'id'"):
        load_bots_config(tmp_path / "bots.yaml")


def test_claude_args_basic(tmp_path: Path):
    (tmp_path / "bots.yaml").write_text(
        """
bots:
  - id: argbot
    allowed_tools: [Read]
    default_model: claude-sonnet-4-5
"""
    )
    cfg = load_bots_config(tmp_path / "bots.yaml")
    bot = cfg.get("argbot")
    args = bot.claude_args()
    # Persona is empty but we still pass --append-system-prompt ""
    assert "--append-system-prompt" in args
    assert "--allowedTools" in args
    assert args[args.index("--allowedTools") + 1] == "Read"
    assert "--model" in args
    assert args[args.index("--model") + 1] == "claude-sonnet-4-5"
    assert "--permission-mode" in args
    assert args[args.index("--permission-mode") + 1] == "bypassPermissions"


def test_claude_args_with_resume(tmp_path: Path):
    (tmp_path / "bots.yaml").write_text("bots:\n  - id: r\n")
    cfg = load_bots_config(tmp_path / "bots.yaml")
    bot = cfg.get("r")
    args = bot.claude_args(claude_session_id="abc-123")
    assert "--resume" in args
    assert args[args.index("--resume") + 1] == "abc-123"