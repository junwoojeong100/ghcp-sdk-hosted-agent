from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx

from my_chat.agent_client import FoundryAgentClient
from my_chat.config import Settings


def test_foundry_session_id_is_reused() -> None:
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
            await client._invoke({"action": "list_models"})
            await client._invoke({"action": "list_models"})
        finally:
            await client.close()

    asyncio.run(scenario())

    assert seen_sessions == [None, "session-123"]
    assert seen_session_headers == [None, None]
