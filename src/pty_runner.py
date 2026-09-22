"""PTY runner — wraps a `claude` interactive REPL via pexpect.

Design notes:
  - pexpect is synchronous; we run it in a thread executor to integrate with asyncio.
  - We forward ALL user input verbatim (text + slash commands). Claude Code decides what to do.
  - We wait for the prompt sentinel `❯` to know a turn is complete.
  - We strip TUI decoration (ANSI escape sequences, spinner glyphs, status bars) before returning.
  - bypassPermissions is set per the BotConfig so no permission prompt deadlocks the PTY.

Sandbox / quoting:
  - We spawn with `echo=False` and `encoding='utf-8'` to keep binary noise minimal.
  - We use `setwinsize` to give Claude Code a reasonable terminal size so its TUI renders.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Optional

import pexpect

from .bots import BotConfig


log = logging.getLogger(__name__)


# Claude Code uses `❯` as its input prompt sentinel.
PROMPT_SENTINEL = "❯"

# ANSI escape sequence pattern. Strips color codes, cursor movement, etc.
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[=>]")

# Spinner / status glyphs that Claude Code emits during processing.
# These appear and disappear many times per second while thinking; we strip them from final output.
SPINNER_GLYPHS_RE = re.compile(r"[\u2800-\u28FF\u2300-\u23FF]")  # Braille + clock-face blocks

# Generic Claude Code "thinking" / status line markers we strip from final output.
STATUS_LINE_MARKERS = (
    "esc to interrupt",
    "ctrl+c to interrupt",
    "(esc to interrupt",
    "(ctrl+c to interrupt",
    "esc to exit",
    "thinking",
    "✽ Thinking",
    "✶ Thinking",
    "✻ Thinking",
    "· Thinking",
    "Wesc to interrupt",
)


@dataclass
class TurnResult:
    """The outcome of one user turn — text the REPL produced in response."""

    text: str
    duration_ms: int
    command_detected: bool  # True if the input started with "/" (a slash command)
    raw_output: str  # Full PTY output for debugging


class ClaudeReplError(Exception):
    """Raised when the REPL is in an unrecoverable state."""


class ClaudeReplSession:
    """Wraps one persistent `claude` interactive REPL.

    Lifecycle: spawn → (send → await_response)* → close.
    A session can be respawned with `--resume <sid>` via the same class.
    """

    def __init__(
        self,
        *,
        bot: BotConfig,
        config_dir: str,
        project_dir_name: str,
        cwd: Path,
        claude_session_id: Optional[str] = None,
        claude_binary: Optional[str] = None,
        cols: int = 200,
        rows: int = 50,
    ) -> None:
        self.bot = bot
        self.config_dir = config_dir  # CLAUDE_CONFIG_DIR
        self.project_dir_name = project_dir_name  # CLAUDE_CODE_PROJECT_DIR_NAME
        self.cwd = Path(cwd)
        self.claude_session_id = claude_session_id  # if resuming
        self.claude_binary = claude_binary or shutil.which("claude") or "claude"
        self.cols = cols
        self.rows = rows
        self._child: Optional[pexpect.spawn] = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"repl-{bot.id}")
        self._lock = asyncio.Lock()
        self._spawned_at_ms = 0
        self._last_seen_claude_session_id: Optional[str] = None

    # ---------------- lifecycle ----------------

    async def start(self) -> None:
        """Spawn the `claude` process and wait for it to be ready (prompt sentinel visible)."""
        async with self._lock:
            if self._child is not None and self._child.isalive():
                return
            self._spawned_at_ms = int(time.time() * 1000)
            args = self.bot.claude_args(claude_session_id=self.claude_session_id)
            env = self._build_env()
            log.info(
                "spawning claude REPL bot=%s config_dir=%s resume=%s args_len=%d",
                self.bot.id,
                self.config_dir,
                bool(self.claude_session_id),
                len(args),
            )
            # pexpect.spawn is sync; run in executor
            loop = asyncio.get_running_loop()
            self._child = await loop.run_in_executor(
                self._executor,
                lambda: self._spawn(args, env),
            )
            # Wait for the initial prompt to confirm the REPL is ready.
            await loop.run_in_executor(self._executor, self._await_initial_prompt)

    def _spawn(self, args: list[str], env: dict[str, str]) -> pexpect.spawn:
        child = pexpect.spawn(
            self.claude_binary,
            args=args,
            cwd=str(self.cwd),
            env=env,
            encoding="utf-8",
            echo=False,
            timeout=10,
        )
        try:
            child.setwinsize(self.rows, self.cols)
        except Exception:  # some platforms don't support setwinsize
            pass
        return child

    def _await_initial_prompt(self) -> None:
        """Block until the initial `❯` prompt appears (REPL ready)."""
        assert self._child is not None
        # The first prompt can take a few seconds while Claude Code boots, MCP servers connect, etc.
        # We use a generous timeout but reset between reads.
        deadline = time.time() + 60
        buf = ""
        while time.time() < deadline:
            try:
                self._child.expect("\n", timeout=2)
            except pexpect.TIMEOUT:
                continue
            except pexpect.EOF:
                raise ClaudeReplError("claude REPL exited before producing initial prompt")
            buf += self._child.before or ""
            buf += self._child.after or ""
            if PROMPT_SENTINEL in buf:
                return
        raise ClaudeReplError("timed out waiting for initial claude REPL prompt")

    def _build_env(self) -> dict[str, str]:
        """Build the child env: HOME-based + Claude Code overrides + per-session cred dir."""
        env = os.environ.copy()
        env["CLAUDE_CONFIG_DIR"] = self.config_dir
        env["CLAUDE_CODE_PROJECT_DIR_NAME"] = self.project_dir_name
        # Belt-and-braces: disable any colors from leaking into our captured text in unexpected ways.
        env["NO_COLOR"] = "1"
        env["FORCE_COLOR"] = "0"
        env["TERM"] = "xterm-256color"
        # Ensure PTY-friendly locale
        env["LC_ALL"] = "C.UTF-8"
        env["LANG"] = "C.UTF-8"
        return env

    # ---------------- per-turn I/O ----------------

    async def send_and_await(
        self, text: str, *, timeout_seconds: int
    ) -> TurnResult:
        """Send one user input and wait for the REPL's response (until next prompt sentinel).

        `text` is forwarded verbatim — Claude Code decides if it's a prompt or a slash command.
        """
        async with self._lock:
            if self._child is None or not self._child.isalive():
                raise ClaudeReplError("REPL not running; call start() or respawn() first")
            command = text.lstrip().startswith("/")
            loop = asyncio.get_running_loop()
            start = time.time()
            try:
                result = await asyncio.wait_for(
                    loop.run_in_executor(
                        self._executor, lambda: self._send_and_block(text)
                    ),
                    timeout=timeout_seconds,
                )
            except asyncio.TimeoutError:
                log.warning("turn timed out after %ss; sending interrupt", timeout_seconds)
                await loop.run_in_executor(self._executor, self._interrupt)
                raise ClaudeReplError(
                    f"turn timed out after {timeout_seconds}s; interrupted"
                )
            duration_ms = int((time.time() - start) * 1000)
            return TurnResult(
                text=result["text"],
                duration_ms=duration_ms,
                command_detected=command,
                raw_output=result["raw"],
            )

    def _send_and_block(self, text: str) -> dict:
        """Synchronous core: write the input, read until the next prompt, return cleaned text."""
        assert self._child is not None
        # Write the user's input followed by Enter. pexpect.sendline appends \r\n.
        self._child.sendline(text)
        # Read until we see the prompt sentinel again.
        buf = ""
        deadline = time.time() + 600  # hard cap; outer timeout will cancel if exceeded
        while time.time() < deadline:
            try:
                self._child.expect("\r\n|\r|\n", timeout=30)
            except pexpect.TIMEOUT:
                # If we don't see a newline for a while but the prompt is already in the buffer,
                # we're done.
                if PROMPT_SENTINEL in buf:
                    break
                continue
            except pexpect.EOF:
                raise ClaudeReplError("claude REPL exited mid-turn")
            buf += self._child.before or ""
            buf += self._child.after or ""
            if PROMPT_SENTINEL in buf:
                break
        else:
            raise ClaudeReplError("hard cap reached waiting for claude response")
        cleaned = self._clean_output(buf)
        # Try to extract a session_id announcement Claude Code prints on startup/resume.
        # The format is roughly "· claude · sonnet · session · abc123-def-..." or similar.
        # We don't fail if we don't find one; this is best-effort.
        return {"text": cleaned, "raw": buf}

    def _clean_output(self, raw: str) -> str:
        """Strip ANSI escapes, spinner glyphs, status bars, and trailing prompt sentinel."""
        # Remove ANSI escape sequences
        text = ANSI_ESCAPE_RE.sub("", raw)
        # Remove spinner / block glyphs
        text = SPINNER_GLYPHS_RE.sub("", text)
        # Remove known status line markers
        for marker in STATUS_LINE_MARKERS:
            text = text.replace(marker, "")
        # Collapse multiple blank lines
        text = re.sub(r"\n{3,}", "\n\n", text)
        # Trim trailing prompt sentinel + any whitespace
        idx = text.rfind(PROMPT_SENTINEL)
        if idx != -1:
            text = text[:idx]
        # Strip leading/trailing whitespace
        return text.strip()

    def _interrupt(self) -> None:
        """Send Ctrl+C to the REPL to interrupt the current turn."""
        if self._child and self._child.isalive():
            self._child.send("\x03")

    # ---------------- session id capture ----------------

    def find_claude_session_id_in_output(self, raw: str) -> Optional[str]:
        """Extract a Claude Code session id from REPL output (printed on resume / start)."""
        # Common patterns:
        #   "session id: abc12345-..." or "session · abc12345-..."
        m = re.search(r"session[\s·:]+([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", raw, re.IGNORECASE)
        if m:
            return m.group(1)
        # Newer Claude Code uses shorter ids like "abc12345-..."
        m = re.search(r"\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b", raw)
        if m:
            return m.group(1)
        return None

    @property
    def last_seen_claude_session_id(self) -> Optional[str]:
        return self._last_seen_claude_session_id

    def note_claude_session_id(self, sid: Optional[str]) -> None:
        if sid:
            self._last_seen_claude_session_id = sid

    # ---------------- close ----------------

    async def close(self) -> None:
        async with self._lock:
            if self._child is not None and self._child.isalive():
                try:
                    self._child.sendline("/exit")
                    self._child.expect(pexpect.EOF, timeout=5)
                except Exception:
                    try:
                        self._child.close(force=True)
                    except Exception:
                        pass
                self._child = None
            self._executor.shutdown(wait=False, cancel_futures=True)

    @property
    def is_alive(self) -> bool:
        return self._child is not None and self._child.isalive()