from __future__ import annotations

import json
from types import SimpleNamespace

from gateway import build_prompt, select_requested_models
from protocol import AgentEnvelope, parse_agent_input


def test_private_protocol_is_parsed() -> None:
    payload = {
        "protocol": "family-chat/v1",
        "action": "chat",
        "model": "gpt-5.6-sol",
        "reasoning_effort": "high",
        "memory": "한국어 답변 선호",
        "messages": [{"role": "user", "content": "이전 질문"}],
        "user_message": "현재 질문",
    }

    envelope, private = parse_agent_input(json.dumps(payload))

    assert private is True
    assert envelope.model == "gpt-5.6-sol"
    assert envelope.messages[0].content == "이전 질문"


def test_plain_prompt_remains_playground_compatible() -> None:
    envelope, private = parse_agent_input("안녕하세요")

    assert private is False
    assert envelope.user_message == "안녕하세요"


def test_only_requested_and_latest_family_models_are_selected() -> None:
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

    assert "<untrusted_family_chat_context>" in prompt
    assert '"personal_memory": "내 선호"' in prompt
    assert '"current_user_message": "새 질문"' in prompt


def test_attachment_and_pptx_options_are_parsed() -> None:
    envelope, private = parse_agent_input(
        json.dumps(
            {
                "protocol": "family-chat/v1",
                "action": "chat",
                "user_message": "이 자료로 PPT를 만들어줘",
                "output_format": "pptx",
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

    assert private is True
    assert envelope.output_format == "pptx"
    assert envelope.attachments[0].filename == "notes.txt"
    assert '"output_format": "pptx"' in build_prompt(envelope)
