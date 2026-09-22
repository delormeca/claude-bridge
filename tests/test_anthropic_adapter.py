"""Unit tests for the Anthropic adapter."""
from __future__ import annotations

from src.anthropic_adapter import (
    ContentBlock,
    Message,
    MessagesRequest,
    build_response,
    estimate_tokens,
    extract_user_input,
)


def test_extract_user_input_string():
    req = MessagesRequest(
        model="claude-sonnet-4-5",
        messages=[Message(role="user", content="hello")],
    )
    assert extract_user_input(req) == "hello"


def test_extract_user_input_last_message_wins():
    req = MessagesRequest(
        model="claude-sonnet-4-5",
        messages=[
            Message(role="user", content="first"),
            Message(role="assistant", content="reply"),
            Message(role="user", content="second"),
        ],
    )
    assert extract_user_input(req) == "second"


def test_extract_user_input_ignores_assistant_in_between():
    req = MessagesRequest(
        model="claude-sonnet-4-5",
        messages=[
            Message(role="user", content="ask"),
            Message(role="assistant", content="reply"),
            Message(role="user", content="follow up"),
            Message(role="assistant", content="more"),
        ],
    )
    assert extract_user_input(req) == "follow up"


def test_extract_user_input_with_content_blocks():
    req = MessagesRequest(
        model="claude-sonnet-4-5",
        messages=[
            Message(
                role="user",
                content=[
                    ContentBlock(type="text", text="line 1"),
                    ContentBlock(type="text", text="line 2"),
                ],
            ),
        ],
    )
    assert extract_user_input(req) == "line 1\nline 2"


def test_extract_user_input_no_user_raises():
    import pytest
    req = MessagesRequest(
        model="claude-sonnet-4-5",
        messages=[Message(role="assistant", content="just me")],
    )
    with pytest.raises(ValueError):
        extract_user_input(req)


def test_build_response_returns_messages_response():
    resp = build_response(model="claude-sonnet-4-5", text="hi", input_tokens_estimate=1, output_tokens_estimate=1)
    assert resp.role == "assistant"
    assert resp.content[0].text == "hi"
    assert resp.model == "claude-sonnet-4-5"
    assert resp.id.startswith("msg_")
    assert resp.usage.input_tokens == 1


def test_estimate_tokens_min_one():
    assert estimate_tokens("") == 1
    assert estimate_tokens("hi") == 1
    assert estimate_tokens("a" * 400) == 100