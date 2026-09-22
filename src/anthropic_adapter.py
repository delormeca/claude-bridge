"""Anthropic-format adapter — translates /v1/messages requests into REPL turns and back.

Anthropic's /v1/messages request shape (simplified):
{
    "model": "claude-sonnet-4-5",
    "max_tokens": 1024,
    "system": "...optional system prompt...",
    "messages": [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
        {"role": "user", "content": "what's the weather?"}
    ],
    "metadata": {"conversation_id": "abc-123"}
}

Strategy:
  - We use messages[-1] as the user input for THIS turn (the harness already maintains conversation history).
  - We pass the entire messages array as context if Claude Code would benefit (e.g. /compact, /context).
    For v1 we just send messages[-1] verbatim — the REPL keeps its own session history.
  - We return an Anthropic-format MessagesResponse with the REPL's text.

Streaming: NOT implemented in v1. We return a complete MessagesResponse. OpenBot handles per-turn latency fine.
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from pydantic import BaseModel, Field


log = logging.getLogger(__name__)


class ContentBlock(BaseModel):
    type: str = "text"
    text: str


class Message(BaseModel):
    role: str
    content: str | list[ContentBlock]


class Metadata(BaseModel):
    conversation_id: str | None = None
    bot_id: str | None = None


class MessagesRequest(BaseModel):
    model: str
    max_tokens: int = 1024
    system: str | None = None
    messages: list[Message]
    metadata: Metadata | None = None
    # Extra Anthropic fields ignored for now (temperature, tools, stream, etc.)


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0


class MessagesResponse(BaseModel):
    id: str = Field(default_factory=lambda: f"msg_{uuid.uuid4().hex[:24]}")
    type: str = "message"
    role: str = "assistant"
    content: list[ContentBlock]
    model: str
    stop_reason: str = "end_turn"
    stop_sequence: str | None = None
    usage: Usage = Field(default_factory=Usage)


def extract_user_input(req: MessagesRequest) -> str:
    """Pick the last user message from the request as the input to forward to the REPL.

    The REPL keeps its own conversation history, so we only need the new turn.
    If the harness sends multi-turn arrays, we still forward only the last user message.
    """
    for msg in reversed(req.messages):
        if msg.role != "user":
            continue
        if isinstance(msg.content, str):
            return msg.content
        if isinstance(msg.content, list):
            # Join text blocks (ignore tool_use/tool_result blocks; the REPL has its own tool loop)
            parts = [b.text for b in msg.content if b.type == "text"]
            return "\n".join(parts)
    raise ValueError("no user message in request")


def build_response(
    *, model: str, text: str, input_tokens_estimate: int = 0, output_tokens_estimate: int = 0
) -> MessagesResponse:
    """Wrap the REPL's output as an Anthropic-format MessagesResponse."""
    return MessagesResponse(
        model=model,
        content=[ContentBlock(type="text", text=text)],
        usage=Usage(
            input_tokens=input_tokens_estimate,
            output_tokens=output_tokens_estimate,
        ),
    )


def estimate_tokens(text: str) -> int:
    """Rough token estimate (4 chars per token). Replace with real tokenizer when needed."""
    return max(1, len(text) // 4)