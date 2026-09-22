"""Tests for the PTY runner output cleaner.

These don't spawn Claude Code — they test the output parsing in isolation.
"""
from src.pty_runner import PROMPT_SENTINEL, ANSI_ESCAPE_RE, ClaudeReplSession


def test_prompt_sentinel_is_known_value():
    assert PROMPT_SENTINEL == "❯"


def test_clean_strips_ansi_escapes():
    # Build a raw output with a few common escape sequences
    raw = "\x1b[31mred text\x1b[0m\n" + PROMPT_SENTINEL
    cleaned = ClaudeReplSession._clean_output(None, raw)
    assert "red text" in cleaned
    assert "\x1b" not in cleaned
    assert PROMPT_SENTINEL not in cleaned


def test_clean_strips_status_markers():
    raw = "Some response text\n✽ Thinking...\nctrl+c to interrupt\nmore text\n" + PROMPT_SENTINEL
    cleaned = ClaudeReplSession._clean_output(None, raw)
    assert "Some response text" in cleaned
    assert "more text" in cleaned
    assert "Thinking" not in cleaned
    assert "ctrl+c" not in cleaned


def test_clean_collapses_blank_lines():
    raw = "line 1\n\n\n\nline 2\n" + PROMPT_SENTINEL
    cleaned = ClaudeReplSession._clean_output(None, raw)
    assert "\n\n\n" not in cleaned
    assert "line 1" in cleaned
    assert "line 2" in cleaned


def test_clean_trims_trailing_prompt():
    raw = "the response" + PROMPT_SENTINEL
    cleaned = ClaudeReplSession._clean_output(None, raw)
    assert cleaned == "the response"


def test_ansi_regex_matches_common_sequences():
    samples = [
        "\x1b[0m",          # reset
        "\x1b[31m",         # red
        "\x1b[1;32m",       # bold green
        "\x1b[?25h",        # private mode
        "\x1b]0;title\x07", # OSC
    ]
    for s in samples:
        assert ANSI_ESCAPE_RE.search(s), f"failed to match {s!r}"


def test_find_claude_session_id_uuid_format():
    raw = (
        "Some intro text\n"
        "session · 7c5dcf5d-1234-5678-9abc-def012345678\n"
        "more text\n" + PROMPT_SENTINEL
    )
    sid = ClaudeReplSession.find_claude_session_id_in_output(None, raw)
    assert sid == "7c5dcf5d-1234-5678-9abc-def012345678"


def test_find_claude_session_id_returns_none_for_no_match():
    raw = "no session id here at all" + PROMPT_SENTINEL
    sid = ClaudeReplSession.find_claude_session_id_in_output(None, raw)
    assert sid is None