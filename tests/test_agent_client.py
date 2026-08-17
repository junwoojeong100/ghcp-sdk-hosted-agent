from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from my_chat.agent_client import AgentServiceError, FoundryAgentClient
from my_chat.config import Settings


def test_foundry_session_id_is_scoped_and_reused_per_user() -> None:
    seen_sessions: list[str | None] = []
    seen_session_headers: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen_sessions.append(payload.get("agent_session_id"))
        seen_session_headers.append(request.headers.get("x-agent-session-id"))
        return httpx.Response(
            200,
            headers={"x-agent-session-id": "session-123"},
            json={
                "agent_session_id": "session-123",
                "output_text": json.dumps(
                    {"ok": True, "type": "models", "models": []}
                )
            },
        )

    async def scenario() -> None:
        settings = Settings(
            app_env="test",
            database_path=Path("unused.db"),
            session_secret="test-session-secret-that-is-long-enough",
            bootstrap_password="bootstrap-1234",
            agent_endpoint="http://localhost:8088/responses",
            token_scope="https://ai.azure.com/.default",
            allowed_hosts=("testserver",),
            cookie_secure=False,
            upload_dir=Path("uploads"),
        )
        client = FoundryAgentClient(settings)
        await client._http.aclose()
        client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            await client._invoke({"action": "list_models"}, "user:1")
            await client._invoke({"action": "list_models"}, "user:1")
            await client._invoke({"action": "list_models"}, "user:2")
        finally:
            await client.close()

    asyncio.run(scenario())

    assert seen_sessions == [None, "session-123", None]
    assert seen_session_headers == [None, None, None]


def test_foundry_chat_stream_yields_deltas() -> None:
    seen_sessions: list[str | None] = []
    sse = "\n".join(
        [
            'data: {"type":"response.created","response":'
            '{"agent_session_id":"stream-session"}}',
            'data: {"type":"response.output_text.delta","delta":"안녕"}',
            'data: {"type":"response.output_text.delta","delta":"하세요"}',
            'data: {"type":"response.completed","response":'
            '{"agent_session_id":"stream-session"}}',
            "",
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen_sessions.append(payload.get("agent_session_id"))
        return httpx.Response(
            200,
            headers={
                "content-type": "text/event-stream",
                "x-agent-session-id": "stream-session",
            },
            content=sse.encode(),
        )

    async def scenario() -> list[str]:
        settings = Settings(
            app_env="test",
            database_path=Path("unused.db"),
            session_secret="test-session-secret-that-is-long-enough",
            bootstrap_password="bootstrap-1234",
            agent_endpoint="http://localhost:8088/responses",
            token_scope="https://ai.azure.com/.default",
            allowed_hosts=("testserver",),
            cookie_secure=False,
            upload_dir=Path("uploads"),
        )
        client = FoundryAgentClient(settings)
        await client._http.aclose()
        client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            first = [
                chunk
                async for chunk in client.chat_stream(
                    model="gpt-5.6-sol",
                    reasoning_effort="low",
                    memory="",
                    messages=[],
                    user_message="인사해줘",
                    session_key="user:1",
                )
            ]
            second = [
                chunk
                async for chunk in client.chat_stream(
                    model="gpt-5.6-sol",
                    reasoning_effort="low",
                    memory="",
                    messages=[],
                    user_message="다시 인사해줘",
                    session_key="user:1",
                )
            ]
            return first + second
        finally:
            await client.close()

    chunks = asyncio.run(scenario())

    assert chunks == ["안녕", "하세요", "안녕", "하세요"]
    assert seen_sessions == [None, "stream-session"]


def test_foundry_chat_stream_rejects_incomplete_response() -> None:
    sse = "\n".join(
        [
            'data: {"type":"response.output_text.delta","delta":"부분 답변"}',
            "",
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse.encode(),
        )

    async def scenario() -> None:
        settings = Settings(
            app_env="test",
            database_path=Path("unused.db"),
            session_secret="test-session-secret-that-is-long-enough",
            bootstrap_password="bootstrap-1234",
            agent_endpoint="http://localhost:8088/responses",
            token_scope="https://ai.azure.com/.default",
            allowed_hosts=("testserver",),
            cookie_secure=False,
            upload_dir=Path("uploads"),
        )
        client = FoundryAgentClient(settings)
        await client._http.aclose()
        client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            async for _ in client.chat_stream(
                model="gpt-5.6-sol",
                reasoning_effort="low",
                memory="",
                messages=[],
                user_message="인사해줘",
                session_key="user:1",
            ):
                pass
        finally:
            await client.close()

    with pytest.raises(
        AgentServiceError,
        match="ended before completion",
    ):
        asyncio.run(scenario())
