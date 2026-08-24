from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from gateway import (
    SYSTEM_INSTRUCTIONS,
    CopilotGateway,
    build_prompt,
    select_requested_models,
)
from protocol import AgentEnvelope, parse_agent_input


def test_structured_protocol_is_parsed() -> None:
    payload = {
        "protocol": "my-chat/v1",
        "action": "chat",
        "model": "gpt-5.6-sol",
        "reasoning_effort": "high",
        "memory": "한국어 답변 선호",
        "messages": [{"role": "user", "content": "이전 질문"}],
        "user_message": "현재 질문",
    }

    envelope, structured = parse_agent_input(json.dumps(payload))

    assert structured is True
    assert envelope.model == "gpt-5.6-sol"
    assert envelope.messages[0].content == "이전 질문"


def test_plain_prompt_remains_playground_compatible() -> None:
    envelope, structured = parse_agent_input("안녕하세요")

    assert structured is False
    assert envelope.user_message == "안녕하세요"


def test_only_requested_and_latest_models_are_selected() -> None:
    models = [
        SimpleNamespace(id="gpt-5.6-sol"),
        SimpleNamespace(id="gpt-5.5"),
        SimpleNamespace(id="gemini-3.6-flash"),
        SimpleNamespace(id="gemini-3.7-flash"),
        SimpleNamespace(id="mai-code-1-flash"),
        SimpleNamespace(id="mai-code-1.1-flash"),
        SimpleNamespace(id="grok-4.6"),
    ]

    selected = select_requested_models(models)

    assert [model.id for model in selected] == [
        "gpt-5.6-sol",
        "gemini-3.7-flash",
        "mai-code-1.1-flash",
    ]


def test_prompt_quotes_memory_and_history_as_untrusted_context() -> None:
    prompt = build_prompt(
        AgentEnvelope(
            memory="내 선호",
            messages=[{"role": "assistant", "content": "이전 답변"}],
            user_message="새 질문",
        )
    )

    assert "<untrusted_my_chat_context>" in prompt
    assert '"personal_memory": "내 선호"' in prompt
    assert '"current_user_message": "새 질문"' in prompt


def test_web_search_mode_changes_prompt_directive() -> None:
    required = build_prompt(
        AgentEnvelope(
            user_message="최신 정보를 찾아줘",
            web_search_mode="required",
        )
    )
    disabled = build_prompt(
        AgentEnvelope(
            user_message="검색하지 말고 답해줘",
            web_search_mode="disabled",
        )
    )

    assert "MUST use web_search or web_fetch" in required
    assert "Do not call any web tool" in disabled


def test_web_search_is_optional_for_stable_requests() -> None:
    normalized_instructions = " ".join(SYSTEM_INSTRUCTIONS.split())
    assert "Use the available web tools" in (
        normalized_instructions
    )
    assert "Do not search for casual conversation" in normalized_instructions
    assert "When you do not search, answer directly" in normalized_instructions
    assert "omit the Sources section" in normalized_instructions
    assert "MUST call the web_search tool" not in normalized_instructions


def test_chat_skips_redundant_model_discovery() -> None:
    gateway = CopilotGateway()
    gateway._get_client = AsyncMock(
        side_effect=AssertionError("chat should not list models")
    )
    calls: list[str] = []

    async def fake_stream(**kwargs):
        calls.append(kwargs["web_search_mode"])
        yield "Fast answer"

    gateway._stream_sdk_chat = fake_stream

    result = asyncio.run(
        gateway.chat(
            AgentEnvelope(
                model="gpt-5.6-sol",
                reasoning_effort="low",
                user_message="간단히 답해줘",
                web_search_mode="disabled",
            )
        )
    )

    assert result == "Fast answer"
    gateway._get_client.assert_not_awaited()

    searched = asyncio.run(
        gateway.chat(
            AgentEnvelope(
                model="gpt-5.6-sol",
                reasoning_effort="low",
                user_message="최신 정보를 검색해줘",
                web_search_mode="required",
            )
        )
    )
    assert searched == "Fast answer"
    assert calls == ["disabled", "required"]


def test_attachment_and_pptx_options_are_parsed() -> None:
    envelope, structured = parse_agent_input(
        json.dumps(
            {
                "protocol": "my-chat/v1",
                "action": "chat",
                "user_message": "이 자료로 PPT를 만들어줘",
                "output_format": "pptx",
                "web_search_mode": "required",
                "attachments": [
                    {
                        "filename": "notes.txt",
                        "mime_type": "text/plain",
                        "size_bytes": 5,
                        "data_base64": "aGVsbG8=",
                    }
                ],
            }
        )
    )

    assert structured is True
    assert envelope.output_format == "pptx"
    assert envelope.web_search_mode == "required"
    assert envelope.attachments[0].filename == "notes.txt"
    assert '"output_format": "pptx"' in build_prompt(envelope)
