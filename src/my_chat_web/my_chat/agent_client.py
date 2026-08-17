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

    async def _invoke(self, envelope: dict[str, Any]) -> dict[str, Any]:
        headers = await self._authorization_headers()
        body = {
            "input": json.dumps(envelope, ensure_ascii=False),
            "stream": False,
        }

        response: httpx.Response | None = None
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
            protocol_text = _extract_output_text(response.json())
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

    async def list_models(self, force_refresh: bool = False) -> dict[str, Any]:
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
                }
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
            }
        )
        content = result.get("content")
        if not isinstance(content, str) or not content.strip():
            raise AgentServiceError("The Foundry agent returned an empty answer.")
        return content

    async def close(self) -> None:
        await self._http.aclose()
        if self._credential is not None:
            await self._credential.close()
