from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

PROTOCOL_NAME = "family-chat/v1"
ReasoningEffort = Literal["default", "low", "medium", "high", "xhigh", "max"]
OutputFormat = Literal["text", "pptx"]


class ChatTurn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=20_000)


class ChatAttachment(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    filename: str = Field(min_length=1, max_length=180)
    mime_type: str = Field(min_length=1, max_length=120)
    size_bytes: int = Field(ge=1, le=8 * 1024 * 1024)
    data_base64: str = Field(min_length=1, max_length=12_000_000)


class AgentEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    protocol: Literal["family-chat/v1"] = PROTOCOL_NAME
    action: Literal["chat", "list_models"] = "chat"
    model: str | None = Field(default=None, max_length=100)
    reasoning_effort: ReasoningEffort = "default"
    memory: str = Field(default="", max_length=8_000)
    messages: list[ChatTurn] = Field(default_factory=list, max_length=40)
    user_message: str | None = Field(default=None, max_length=20_000)
    attachments: list[ChatAttachment] = Field(default_factory=list, max_length=5)
    output_format: OutputFormat = "text"


def parse_agent_input(text: str) -> tuple[AgentEnvelope, bool]:
    """Parse the private web protocol, falling back to a normal Playground prompt."""
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        return AgentEnvelope(user_message=text), False

    if not isinstance(raw, dict) or raw.get("protocol") != PROTOCOL_NAME:
        return AgentEnvelope(user_message=text), False

    return AgentEnvelope.model_validate(raw), True


def platform_history_to_turns(history: list[dict[str, Any]]) -> list[ChatTurn]:
    role_map = {"input_text": "user", "output_text": "assistant"}
    turns: list[ChatTurn] = []
    for item in history[-20:]:
        for content in item.get("content") or []:
            role = role_map.get(content.get("type"))
            text = content.get("text")
            if role and isinstance(text, str) and text.strip():
                turns.append(ChatTurn(role=role, content=text))
    return turns[-40:]
