from __future__ import annotations

import asyncio
import json
import logging

from azure.ai.agentserver.responses import (
    CreateResponse,
    ResponseContext,
    ResponsesAgentServerHost,
    ResponsesServerOptions,
    TextResponse,
)
from dotenv import load_dotenv
from pydantic import ValidationError

from gateway import CopilotGateway
from protocol import parse_agent_input, platform_history_to_turns

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

gateway = CopilotGateway()
app = ResponsesAgentServerHost(
    options=ResponsesServerOptions(default_fetch_history_count=20)
)


def _json_result(**payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False)


@app.response_handler
async def handler(
    request: CreateResponse,
    context: ResponseContext,
    _cancellation_signal: asyncio.Event,
):
    user_input = await context.get_input_text() or ""

    try:
        envelope, is_private_protocol = parse_agent_input(user_input)
        if envelope.action == "list_models":
            result = await gateway.list_models()
            text_source = _json_result(
                ok=True,
                type="models",
                **result,
            )
        else:
            if not is_private_protocol:
                envelope.messages = platform_history_to_turns(
                    await context.get_history()
                )
            request_stream = (
                bool(request.get("stream"))
                if isinstance(request, dict)
                else bool(request.stream)
            )
            if request_stream:
                text_source = gateway.chat_stream(envelope)
            else:
                answer = await gateway.chat(envelope)
                text_source = (
                    _json_result(ok=True, type="chat", content=answer)
                    if is_private_protocol
                    else answer
                )
    except (ValidationError, ValueError) as exc:
        logger.warning("Invalid agent request: %s", exc)
        text_source = _json_result(
            ok=False,
            error="invalid_request",
            detail=str(exc),
        )
    except TimeoutError as exc:
        logger.error("Copilot response timed out: %s", exc)
        text_source = _json_result(
            ok=False,
            error="copilot_timeout",
            detail="The selected model did not respond before the timeout.",
        )
    except RuntimeError as exc:
        logger.error("Copilot runtime error: %s", exc)
        text_source = _json_result(
            ok=False,
            error="copilot_runtime_error",
            detail=str(exc),
        )
    except Exception:
        logger.exception("Unexpected hosted-agent failure")
        text_source = _json_result(
            ok=False,
            error="internal_error",
            detail="The hosted agent failed unexpectedly.",
        )

    async for event in TextResponse(
        context,
        request,
        text=text_source,
    ):
        yield event


app.run()
