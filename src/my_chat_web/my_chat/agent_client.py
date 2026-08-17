from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from urllib.parse import urlparse

import httpx
from azure.core.exceptions import AzureError
from azure.identity.aio import DefaultAzureCredential, ManagedIdentityCredential

from .config import Settings


class AgentServiceError(RuntimeError):
    pass


def _extract_output_text(payload: dict[str, Any]) -> str:
    direct = payload.get("output_text")
    if isinstance(direct, str) and direct:
        return direct

    chunks: list[str] = []
    for output in payload.get("output") or []:
        for content in output.get("content") or []:
            text = content.get("text")
            if content.get("type") == "output_text" and isinstance(text, str):
                chunks.append(text)
    if chunks:
        return "".join(chunks)
    raise AgentServiceError("The Foundry agent response contained no output text.")


class FoundryAgentClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=15, read=210, write=30, pool=30)
        )
        self._credential: DefaultAzureCredential | ManagedIdentityCredential | None = None
        self._models_cache: dict[str, Any] | None = None
        self._models_cached_at = 0.0
        self._models_lock = asyncio.Lock()
        self._agent_session_ids: dict[str, str] = {}
        self._session_init_locks: dict[str, asyncio.Lock] = {}

    @property
    def _is_local(self) -> bool:
        hostname = urlparse(self.settings.agent_endpoint).hostname
        return hostname in {"localhost", "127.0.0.1", "::1"}

    async def _authorization_headers(self) -> dict[str, str]:
        if self._is_local:
            return {}
        if self._credential is None:
            if self.settings.app_env == "production":
                self._credential = ManagedIdentityCredential()
            else:
                self._credential = DefaultAzureCredential()
        try:
            token = await self._credential.get_token(self.settings.token_scope)
        except (AzureError, ImportError, RuntimeError) as exc:
            raise AgentServiceError(
                f"Unable to authenticate to Microsoft Foundry: {exc}"
            ) from exc
        return {"Authorization": f"Bearer {token.token}"}

    async def _invoke_request(
        self,
        envelope: dict[str, Any],
        session_key: str,
    ) -> dict[str, Any]:
        headers = await self._authorization_headers()
        body: dict[str, Any] = {
            "input": json.dumps(envelope, ensure_ascii=False),
            "stream": False,
        }
        session_id = self._agent_session_ids.get(session_key)
        if session_id:
            body["agent_session_id"] = session_id

        response: httpx.Response | None = None
        response_payload: dict[str, Any] | None = None
        for attempt in range(3):
            try:
                response = await self._http.post(
                    self.settings.agent_endpoint,
                    json=body,
                    headers=headers,
                )
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                if attempt == 2:
                    raise AgentServiceError(
                        f"Unable to reach the Foundry agent: {exc}"
                    ) from exc
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            except httpx.HTTPError as exc:
                raise AgentServiceError(
                    f"The Foundry agent request failed after it was sent: {exc}"
                ) from exc

            try:
                candidate_payload = response.json()
                response_payload = (
                    candidate_payload
                    if isinstance(candidate_payload, dict)
                    else None
                )
            except ValueError:
                response_payload = None
            response_session_id = (
                response_payload.get("agent_session_id")
                if response_payload is not None
                else None
            ) or response.headers.get("x-agent-session-id")
            if isinstance(response_session_id, str) and response_session_id:
                self._agent_session_ids[session_key] = response_session_id
                body["agent_session_id"] = response_session_id
            if response.status_code == 424 and attempt < 2:
                await asyncio.sleep(15 * (attempt + 1))
                continue
            if response.status_code == 429 and attempt < 2:
                retry_after = response.headers.get("Retry-After", "5")
                try:
                    delay = min(max(float(retry_after), 1), 30)
                except ValueError:
                    delay = 5
                await asyncio.sleep(delay)
                continue
            if response.status_code not in {424, 429}:
                break

        if response is None:
            raise AgentServiceError("The Foundry agent request was not sent.")
        if response.is_error:
            detail = response.text[:500]
            raise AgentServiceError(
                f"Foundry agent returned HTTP {response.status_code}: {detail}"
            )

        try:
            response_payload = response_payload or response.json()
            protocol_text = _extract_output_text(response_payload)
            result = json.loads(protocol_text)
        except (ValueError, json.JSONDecodeError) as exc:
            raise AgentServiceError(
                "The Foundry agent returned an invalid protocol response."
            ) from exc

        if not isinstance(result, dict):
            raise AgentServiceError("The Foundry agent returned an invalid result.")
        if not result.get("ok"):
            raise AgentServiceError(
                str(result.get("detail") or result.get("error") or "Agent request failed.")
            )
        return result

    async def _invoke(
        self,
        envelope: dict[str, Any],
        session_key: str,
    ) -> dict[str, Any]:
        if session_key in self._agent_session_ids:
            return await self._invoke_request(envelope, session_key)
        lock = self._session_init_locks.setdefault(session_key, asyncio.Lock())
        async with lock:
            return await self._invoke_request(envelope, session_key)

    async def list_models(
        self,
        force_refresh: bool = False,
        session_key: str = "models",
    ) -> dict[str, Any]:
        async with self._models_lock:
            if (
                not force_refresh
                and self._models_cache is not None
                and time.monotonic() - self._models_cached_at < 600
            ):
                return self._models_cache

            result = await self._invoke(
                {
                    "protocol": "my-chat/v1",
                    "action": "list_models",
                },
                session_key,
            )
            self._models_cache = result
            self._models_cached_at = time.monotonic()
            return result

    async def chat(
        self,
        *,
        model: str,
        reasoning_effort: str,
        memory: str,
        messages: list[dict[str, str]],
        user_message: str,
        attachments: list[dict[str, Any]] | None = None,
        output_format: str = "text",
        web_search_mode: str = "auto",
        session_key: str,
    ) -> str:
        result = await self._invoke(
            {
                "protocol": "my-chat/v1",
                "action": "chat",
                "model": model,
                "reasoning_effort": reasoning_effort,
                "memory": memory,
                "messages": messages,
                "user_message": user_message,
                "attachments": attachments or [],
                "output_format": output_format,
                "web_search_mode": web_search_mode,
            },
            session_key,
        )
        content = result.get("content")
        if not isinstance(content, str) or not content.strip():
            raise AgentServiceError("The Foundry agent returned an empty answer.")
        return content

    async def _stream_chat_request(
        self,
        envelope: dict[str, Any],
        session_key: str,
    ):
        headers = await self._authorization_headers()
        body: dict[str, Any] = {
            "input": json.dumps(envelope, ensure_ascii=False),
            "stream": True,
        }
        session_id = self._agent_session_ids.get(session_key)
        if session_id:
            body["agent_session_id"] = session_id

        for attempt in range(3):
            async with self._http.stream(
                "POST",
                self.settings.agent_endpoint,
                json=body,
                headers=headers,
            ) as response:
                response_session_id = response.headers.get("x-agent-session-id")
                if response_session_id:
                    self._agent_session_ids[session_key] = response_session_id
                    body["agent_session_id"] = response_session_id
                if response.status_code == 424 and attempt < 2:
                    await response.aread()
                    await asyncio.sleep(15 * (attempt + 1))
                    continue
                if response.status_code == 429 and attempt < 2:
                    retry_after = response.headers.get("Retry-After", "5")
                    try:
                        delay = min(max(float(retry_after), 1), 30)
                    except ValueError:
                        delay = 5
                    await response.aread()
                    await asyncio.sleep(delay)
                    continue
                if response.is_error:
                    detail = (await response.aread()).decode(
                        "utf-8", errors="replace"
                    )[:500]
                    raise AgentServiceError(
                        f"Foundry agent returned HTTP "
                        f"{response.status_code}: {detail}"
                    )

                completed = False
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if not raw or raw == "[DONE]":
                        continue
                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    response_data = event.get("response")
                    if isinstance(response_data, dict):
                        response_session_id = response_data.get("agent_session_id")
                        if (
                            isinstance(response_session_id, str)
                            and response_session_id
                        ):
                            self._agent_session_ids[session_key] = (
                                response_session_id
                            )
                    event_type = event.get("type")
                    if event_type == "response.output_text.delta":
                        delta = event.get("delta")
                        if isinstance(delta, str) and delta:
                            yield delta
                    elif event_type == "response.completed":
                        completed = True
                    elif event_type in {
                        "response.failed",
                        "response.incomplete",
                    }:
                        raise AgentServiceError(
                            "The Foundry agent stream failed."
                        )
                if not completed:
                    raise AgentServiceError(
                        "The Foundry agent stream ended before completion."
                    )
                return
        raise AgentServiceError("The Foundry agent stream did not start.")

    async def chat_stream(
        self,
        *,
        model: str,
        reasoning_effort: str,
        memory: str,
        messages: list[dict[str, str]],
        user_message: str,
        attachments: list[dict[str, Any]] | None = None,
        output_format: str = "text",
        web_search_mode: str = "auto",
        session_key: str,
    ):
        envelope = {
            "protocol": "my-chat/v1",
            "action": "chat",
            "model": model,
            "reasoning_effort": reasoning_effort,
            "memory": memory,
            "messages": messages,
            "user_message": user_message,
            "attachments": attachments or [],
            "output_format": output_format,
            "web_search_mode": web_search_mode,
        }
        if session_key in self._agent_session_ids:
            async for chunk in self._stream_chat_request(envelope, session_key):
                yield chunk
            return
        lock = self._session_init_locks.setdefault(session_key, asyncio.Lock())
        async with lock:
            async for chunk in self._stream_chat_request(envelope, session_key):
                yield chunk

    async def close(self) -> None:
        await self._http.aclose()
        if self._credential is not None:
            await self._credential.close()
