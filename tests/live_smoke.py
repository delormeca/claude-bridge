"""Live smoke test for the PTY runner against a real Claude Code session.

Run with:
    cd claude-bridge
    source .venv/bin/activate
    python tests/live_smoke.py

This spawns a real `claude` REPL with a minimal bot config and sends:
  - /help (slash command)
  - a free-text prompt
  - /clear
  - /cost

Then asserts the responses look like Claude Code's responses.

NOTE: This uses the host's default Claude Code credentials (Keychain on macOS).
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.bots import BotConfig
from src.pty_runner import ClaudeReplSession


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s :: %(message)s")


async def main() -> int:
    if not shutil.which("claude"):
        print("ERROR: 'claude' CLI not on PATH", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory(prefix="claude-bridge-live-") as tmp:
        ws = Path(tmp) / "workspace"
        ws.mkdir()

        bot = BotConfig(
            id="live-smoke",
            persona_text="You are a smoke test bot. Reply concisely.",
            allowed_tools=[],
            mcp_config_path=None,
            agents_config_path=None,
            default_model="claude-sonnet-4-5",
            workspace=ws,
        )
        # Use the host's default credentials (Keychain on macOS).
        config_dir = str(Path.home() / ".claude")
        project_dir_name = "live-smoke"

        print(f"→ spawning REPL (config_dir={config_dir}, project={project_dir_name})")
        repl = ClaudeReplSession(
            bot=bot,
            config_dir=config_dir,
            project_dir_name=project_dir_name,
            cwd=ws,
        )
        try:
            await repl.start()
        except Exception as e:
            print(f"ERROR: failed to start REPL: {e}", file=sys.stderr)
            return 2

        try:
            print("\n--- /help ---")
            r = await repl.send_and_await("/help", timeout_seconds=60)
            print(r.text[:1500])
            assert len(r.text) > 0, "empty response to /help"

            print("\n--- free text 'Reply with just the word PONG' ---")
            r = await repl.send_and_await("Reply with just the word PONG.", timeout_seconds=60)
            print(r.text[:500])
            assert "PONG" in r.text, f"expected PONG in response, got: {r.text[:200]}"

            print("\n--- /cost ---")
            r = await repl.send_and_await("/cost", timeout_seconds=60)
            print(r.text[:500])
            assert "cost" in r.text.lower() or "$" in r.text or "Total" in r.text, \
                f"unexpected /cost response: {r.text[:200]}"

            print("\n--- /clear ---")
            r = await repl.send_and_await("/clear", timeout_seconds=60)
            print(repr(r.text[:200]))

            print("\n--- post-clear free text 'Reply with just CLEARED' ---")
            r = await repl.send_and_await("Reply with just the word CLEARED.", timeout_seconds=60)
            print(r.text[:500])
            assert "CLEARED" in r.text, f"expected CLEARED, got: {r.text[:200]}"
        finally:
            await repl.close()

    print("\n✓ LIVE SMOKE PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))