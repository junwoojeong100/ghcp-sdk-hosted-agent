from __future__ import annotations

from pathlib import Path
from typing import Any
import json

import pytest
from fastapi.testclient import TestClient

from my_chat import create_app
from my_chat.config import Settings


class FakeAgentClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def list_models(self, force_refresh: bool = False) -> dict[str, Any]:
        self.calls.append({"type": "list_models", "force_refresh": force_refresh})
        return {
            "models": [
                {
                    "id": "gpt-5.6-sol",
                    "name": "GPT-5.6 Sol",
                    "reasoning_efforts": ["low", "medium", "high", "xhigh", "max"],
                    "default_reasoning_effort": "high",
                    "billing_multiplier": 1,
                },
                {
                    "id": "claude-haiku-4.5",
                    "name": "Claude Haiku 4.5",
                    "reasoning_efforts": [],
                    "default_reasoning_effort": None,
                    "billing_multiplier": 0.33,
                },
            ],
            "missing_requested_models": [],
        }

    async def chat(self, **payload: Any) -> str:
        self.calls.append(payload)
        if payload.get("output_format") == "pptx":
            return json.dumps(
                {
                    "title": "테스트 발표",
                    "subtitle": "My Chat 테스트",
                    "slides": [
                        {
                            "title": "핵심 내용",
                            "bullets": ["첫 번째 요점", "두 번째 요점"],
                        }
                    ],
                    "sources": [
                        {
                            "title": "GitHub",
                            "url": "https://github.com/",
                        }
                    ],
                },
                ensure_ascii=False,
            )
        return f"가짜 답변: {payload['user_message']}"

    async def chat_stream(self, **payload: Any):
        self.calls.append(payload)
        answer = f"가짜 답변: {payload['user_message']}"
        midpoint = max(1, len(answer) // 2)
        yield answer[:midpoint]
        yield answer[midpoint:]

    async def close(self) -> None:
        return None


@pytest.fixture
def fake_agent() -> FakeAgentClient:
    return FakeAgentClient()


@pytest.fixture
def app(tmp_path: Path, fake_agent: FakeAgentClient):
    settings = Settings(
        app_env="test",
        database_path=tmp_path / "my-chat.db",
        session_secret="test-session-secret-that-is-long-enough",
        bootstrap_password="bootstrap-1234",
        agent_endpoint="http://localhost:8088/responses",
        token_scope="https://ai.azure.com/.default",
        allowed_hosts=("testserver",),
        cookie_secure=False,
        upload_dir=tmp_path / "uploads",
    )
    return create_app(settings=settings, agent_client=fake_agent)


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client
