from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import time
import uuid
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import (
    FastAPI,
    Form,
    HTTPException,
    Request,
    status,
)
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.datastructures import UploadFile as StarletteUploadFile
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .agent_client import AgentServiceError, FoundryAgentClient
from .config import CHAT_USERS, FALLBACK_MODELS, Settings
from .database import Database, User
from .presentation import (
    PresentationFormatError,
    build_presentation,
    parse_deck_response,
)
from .security import (
    csrf_token,
    password_validation_error,
    require_api_user,
    session_user,
    validate_csrf,
)

ReasoningEffort = Literal["default", "low", "medium", "high", "xhigh", "max"]
OutputFormat = Literal["text", "pptx"]
WebSearchMode = Literal["auto", "required", "disabled"]
FAST_WEB_SEARCH_MODEL = "gpt-5.6-luna"
logger = logging.getLogger(__name__)

ALLOWED_ATTACHMENT_TYPES = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".json": "application/json",
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


class ConversationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    model: str = Field(min_length=1, max_length=100)
    reasoning_effort: ReasoningEffort = "default"


class ConversationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str | None = Field(default=None, max_length=80)
    model: str | None = Field(default=None, max_length=100)
    reasoning_effort: ReasoningEffort | None = None


class MessageCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    content: str = Field(min_length=1, max_length=12_000)
    model: str = Field(min_length=1, max_length=100)
    reasoning_effort: ReasoningEffort = "default"
    output_format: OutputFormat = "text"
    web_search_mode: WebSearchMode = "auto"


class MemoryUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(max_length=8_000)


class PasswordUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_password: str = Field(min_length=1, max_length=200)
    new_password: str = Field(min_length=1, max_length=200)
    confirm_password: str = Field(min_length=1, max_length=200)


def _template_context(
    request: Request,
    *,
    user: User | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "request": request,
        "user": user,
        "error": error,
        "csrf_token": csrf_token(request),
        "chat_users": CHAT_USERS,
    }


def _api_csrf(request: Request) -> None:
    validate_csrf(request, request.headers.get("X-CSRF-Token"))


def _safe_filename(filename: str) -> str:
    name = Path(filename).name
    name = re.sub(r"[\x00-\x1f\x7f]+", "", name).strip()
    return (name or "attachment")[:180]


def _remove_files(paths: list[str], *, strict: bool = False) -> None:
    failures: list[str] = []
    for value in paths:
        try:
            Path(value).unlink(missing_ok=True)
        except OSError:
            failures.append(value)
            logger.exception("Unable to remove attachment file: %s", value)
    if failures and strict:
        raise OSError(f"Unable to remove {len(failures)} attachment file(s).")


async def _parse_message_request(
    request: Request,
    settings: Settings,
) -> tuple[MessageCreate, list[StarletteUploadFile]]:
    content_type = request.headers.get("content-type", "")
    files: list[StarletteUploadFile] = []
    try:
        if content_type.startswith(
            ("multipart/form-data", "application/x-www-form-urlencoded")
        ):
            form = await request.form(
                max_files=settings.max_attachments,
                max_fields=20,
                max_part_size=settings.max_attachment_bytes,
            )
            files = [
                item
                for item in form.getlist("files")
                if isinstance(item, StarletteUploadFile)
            ]
            content = str(form.get("content") or "").strip()
            if not content and files:
                content = "첨부 파일을 분석해줘."
            payload = MessageCreate.model_validate(
                {
                    "content": content,
                    "model": form.get("model"),
                    "reasoning_effort": form.get("reasoning_effort", "default"),
                    "output_format": form.get("output_format", "text"),
                    "web_search_mode": form.get("web_search_mode", "auto"),
                }
            )
        else:
            payload = MessageCreate.model_validate(await request.json())
    except (ValidationError, ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    if payload.output_format == "text" and re.search(
        r"(pptx?|파워포인트|슬라이드).*(만들|생성|작성|create|make)",
        payload.content,
        re.IGNORECASE,
    ):
        payload = payload.model_copy(update={"output_format": "pptx"})
    return payload, files


async def _store_uploads(
    *,
    settings: Settings,
    database: Database,
    user_id: int,
    conversation_id: str,
    message_id: str,
    files: list[StarletteUploadFile],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not files:
        return [], []
    if len(files) > settings.max_attachments:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"파일은 한 번에 최대 {settings.max_attachments}개까지 첨부할 수 있습니다.",
        )

    validated: list[tuple[str, str, bytes]] = []
    total_size = 0
    for upload in files:
        filename = _safe_filename(upload.filename or "attachment")
        suffix = Path(filename).suffix.lower()
        if suffix not in ALLOWED_ATTACHMENT_TYPES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"지원하지 않는 파일 형식입니다: {filename}",
            )
        content = await upload.read(settings.max_attachment_bytes + 1)
        if not content:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"빈 파일은 첨부할 수 없습니다: {filename}",
            )
        if len(content) > settings.max_attachment_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"파일 크기는 각각 {settings.max_attachment_bytes // (1024 * 1024)}MB 이하여야 합니다.",
            )
        total_size += len(content)
        if total_size > settings.max_total_attachment_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"전체 첨부 파일은 {settings.max_total_attachment_bytes // (1024 * 1024)}MB 이하여야 합니다.",
            )
        mime_type = ALLOWED_ATTACHMENT_TYPES[suffix]
        validated.append((filename, mime_type, content))

    public_items: list[dict[str, Any]] = []
    agent_items: list[dict[str, Any]] = []
    target_dir = settings.upload_dir / str(user_id) / conversation_id
    target_dir.mkdir(parents=True, exist_ok=True)

    for filename, mime_type, content in validated:
        attachment_id = str(uuid.uuid4())
        storage_path = target_dir / f"{attachment_id}{Path(filename).suffix.lower()}"
        staging_path = target_dir / f".{attachment_id}.uploading"
        try:
            staging_path.write_bytes(content)
            staging_path.chmod(0o600)
            os.replace(staging_path, storage_path)
            public_item = database.add_attachment(
                attachment_id=attachment_id,
                message_id=message_id,
                conversation_id=conversation_id,
                user_id=user_id,
                filename=filename,
                mime_type=mime_type,
                size_bytes=len(content),
                storage_path=str(storage_path),
            )
        except Exception:
            staging_path.unlink(missing_ok=True)
            storage_path.unlink(missing_ok=True)
            raise
        public_items.append(public_item)
        agent_items.append(
            {
                "filename": filename,
                "mime_type": mime_type,
                "size_bytes": len(content),
                "data_base64": base64.b64encode(content).decode("ascii"),
            }
        )
    return public_items, agent_items


def _presentation_download_name(title: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|]+', "-", title).strip(" .-")
    return f"{(cleaned or 'My Chat Presentation')[:100]}.pptx"


def create_app(
    settings: Settings | None = None,
    agent_client: FoundryAgentClient | Any | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    database = Database(settings.database_path)
    agent_client = agent_client or FoundryAgentClient(settings)
    templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        database.initialize(settings.bootstrap_password)
        settings.upload_dir.mkdir(parents=True, exist_ok=True)

        async def warm_models() -> None:
            try:
                await app.state.agent_client.list_models()
            except AgentServiceError as exc:
                logger.warning("Agent model warmup failed: %s", exc)
            except Exception:
                logger.exception("Unexpected agent model warmup failure")

        warmup_task = asyncio.create_task(warm_models())
        yield
        if not warmup_task.done():
            warmup_task.cancel()
        await asyncio.gather(warmup_task, return_exceptions=True)
        close = getattr(app.state.agent_client, "close", None)
        if close:
            result = close()
            if hasattr(result, "__await__"):
                await result

    app = FastAPI(
        title="My Chat",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.database = database
    app.state.agent_client = agent_client
    app.state.chat_locks = defaultdict(asyncio.Lock)

    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=list(settings.allowed_hosts),
    )
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        session_cookie="my_chat_session",
        max_age=60 * 60 * 24 * 7,
        same_site="lax",
        https_only=settings.cookie_secure,
    )
    app.mount(
        "/static",
        StaticFiles(directory=Path(__file__).parent / "static"),
        name="static",
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=()"
        )
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "style-src 'self'; "
            "script-src 'self'; "
            "img-src 'self' data:; "
            "connect-src 'self'; "
            "frame-ancestors 'none'; "
            "base-uri 'self'; "
            "form-action 'self'"
        )
        if settings.cookie_secure:
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )
        return response

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request):
        user = session_user(request)
        if user is None:
            return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
        destination = "/change-password" if user.must_change_password else "/chat"
        return RedirectResponse(destination, status_code=status.HTTP_303_SEE_OTHER)

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        user = session_user(request)
        if user is not None:
            destination = "/change-password" if user.must_change_password else "/chat"
            return RedirectResponse(destination, status_code=status.HTTP_303_SEE_OTHER)
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context=_template_context(request),
        )

    @app.post("/login", response_class=HTMLResponse)
    async def login(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
        csrf: str = Form(...),
    ):
        validate_csrf(request, csrf)
        if username not in CHAT_USERS:
            result = None
        else:
            result = database.authenticate(username, password)

        if result is None or result.user is None:
            error = (
                "로그인 시도가 잠겼습니다. 15분 뒤 다시 시도해 주세요."
                if result and result.error == "locked"
                else "사용자 이름 또는 비밀번호가 올바르지 않습니다."
            )
            return templates.TemplateResponse(
                request=request,
                name="login.html",
                context=_template_context(request, error=error),
                status_code=status.HTTP_401_UNAUTHORIZED,
            )

        request.session.clear()
        request.session["username"] = result.user.username
        request.session["session_version"] = result.user.session_version
        csrf_token(request)
        destination = (
            "/change-password" if result.user.must_change_password else "/chat"
        )
        return RedirectResponse(destination, status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/logout")
    async def logout(request: Request, csrf: str = Form(...)):
        validate_csrf(request, csrf)
        request.session.clear()
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)

    @app.get("/change-password", response_class=HTMLResponse)
    async def change_password_page(request: Request):
        user = session_user(request)
        if user is None:
            return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
        if not user.must_change_password:
            return RedirectResponse("/chat", status_code=status.HTTP_303_SEE_OTHER)
        return templates.TemplateResponse(
            request=request,
            name="change_password.html",
            context=_template_context(request, user=user),
        )

    @app.post("/change-password", response_class=HTMLResponse)
    async def change_password(
        request: Request,
        new_password: str = Form(...),
        confirm_password: str = Form(...),
        csrf: str = Form(...),
    ):
        validate_csrf(request, csrf)
        user = session_user(request)
        if user is None:
            return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
        if not user.must_change_password:
            return RedirectResponse("/chat", status_code=status.HTTP_303_SEE_OTHER)

        error = password_validation_error(new_password, user.username)
        if new_password != confirm_password:
            error = "새 비밀번호가 서로 일치하지 않습니다."
        if database.verify_password(user.id, new_password):
            error = "초기 비밀번호와 다른 새 비밀번호를 사용해 주세요."
        if error:
            return templates.TemplateResponse(
                request=request,
                name="change_password.html",
                context=_template_context(request, user=user, error=error),
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        database.change_password(user.id, new_password)
        updated_user = database.get_user(user.username)
        if updated_user is None:
            raise RuntimeError("User disappeared after password update.")
        request.session["session_version"] = updated_user.session_version
        return RedirectResponse("/chat", status_code=status.HTTP_303_SEE_OTHER)

    @app.get("/chat", response_class=HTMLResponse)
    async def chat_page(request: Request):
        user = session_user(request)
        if user is None:
            return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
        if user.must_change_password:
            return RedirectResponse(
                "/change-password", status_code=status.HTTP_303_SEE_OTHER
            )
        return templates.TemplateResponse(
            request=request,
            name="chat.html",
            context=_template_context(request, user=user),
        )

    @app.get("/api/me")
    async def me(request: Request):
        user = require_api_user(request)
        return {
            "username": user.username,
            "csrf_token": csrf_token(request),
        }

    @app.get("/api/models")
    async def models(request: Request, refresh: bool = False):
        require_api_user(request)
        try:
            result = await app.state.agent_client.list_models(
                force_refresh=refresh
            )
            return {
                "models": result.get("models", []),
                "missing_requested_models": result.get(
                    "missing_requested_models", []
                ),
                "source": "copilot",
                "warning": None,
            }
        except AgentServiceError as exc:
            fallback = [
                {
                    **model,
                    "reasoning_efforts": [],
                    "default_reasoning_effort": None,
                    "billing_multiplier": None,
                }
                for model in FALLBACK_MODELS
            ]
            return {
                "models": fallback,
                "missing_requested_models": [],
                "source": "configured_fallback",
                "warning": f"실시간 모델 목록을 불러오지 못했습니다: {exc}",
            }

    @app.get("/api/conversations")
    async def conversations(request: Request):
        user = require_api_user(request)
        return {"conversations": database.list_conversations(user.id)}

    @app.post("/api/conversations", status_code=status.HTTP_201_CREATED)
    async def create_conversation(request: Request, payload: ConversationCreate):
        _api_csrf(request)
        user = require_api_user(request)
        conversation = database.create_conversation(
            user.id,
            payload.model,
            payload.reasoning_effort,
        )
        return {"conversation": conversation}

    @app.get("/api/conversations/{conversation_id}")
    async def conversation(request: Request, conversation_id: str):
        user = require_api_user(request)
        item = database.get_conversation(user.id, conversation_id)
        if item is None:
            raise HTTPException(status_code=404, detail="Conversation not found.")
        return {
            "conversation": item,
            "messages": database.list_messages(user.id, conversation_id),
        }

    @app.patch("/api/conversations/{conversation_id}")
    async def update_conversation(
        request: Request,
        conversation_id: str,
        payload: ConversationUpdate,
    ):
        _api_csrf(request)
        user = require_api_user(request)
        item = database.update_conversation(
            user.id,
            conversation_id,
            title=payload.title,
            model=payload.model,
            reasoning_effort=payload.reasoning_effort,
        )
        if item is None:
            raise HTTPException(status_code=404, detail="Conversation not found.")
        return {"conversation": item}

    @app.delete("/api/conversations/{conversation_id}", status_code=204)
    async def delete_conversation(request: Request, conversation_id: str):
        _api_csrf(request)
        user = require_api_user(request)
        attachment_paths = database.attachment_paths_for_conversation(
            user.id,
            conversation_id,
        )
        try:
            _remove_files(attachment_paths, strict=True)
        except OSError as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="첨부 파일을 정리하지 못해 대화를 삭제하지 않았습니다.",
            ) from exc
        if not database.delete_conversation(user.id, conversation_id):
            raise HTTPException(status_code=404, detail="Conversation not found.")
        return None

    @app.delete("/api/conversations", status_code=204)
    async def delete_all_conversations(request: Request):
        _api_csrf(request)
        user = require_api_user(request)
        attachment_paths = database.attachment_paths_for_user(user.id)
        try:
            _remove_files(attachment_paths, strict=True)
        except OSError as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="첨부 파일을 정리하지 못해 채팅 기록을 삭제하지 않았습니다.",
            ) from exc
        database.delete_all_conversations(user.id)
        return None

    async def prepare_user_message(
        *,
        user: User,
        conversation_id: str,
        payload: MessageCreate,
        files: list[StarletteUploadFile],
    ) -> tuple[
        dict[str, Any],
        list[dict[str, Any]],
        list[dict[str, str]],
    ]:
        prior_messages = database.list_messages(user.id, conversation_id)
        usable_history = [
            {"role": message["role"], "content": message["content"]}
            for message in prior_messages[-30:]
            if message["status"] == "complete"
        ]
        user_message = database.add_message(
            user.id,
            conversation_id,
            "user",
            payload.content,
            model=payload.model,
            reasoning_effort=payload.reasoning_effort,
            status="pending",
        )
        try:
            public_attachments, agent_attachments = await _store_uploads(
                settings=settings,
                database=database,
                user_id=user.id,
                conversation_id=conversation_id,
                message_id=user_message["id"],
                files=files,
            )
        except HTTPException as exc:
            database.mark_message_error(
                user.id,
                user_message["id"],
                str(exc.detail),
            )
            raise
        user_message["attachments"] = public_attachments
        database.set_title_from_first_message(
            user.id, conversation_id, payload.content
        )
        database.update_conversation(
            user.id,
            conversation_id,
            model=payload.model,
            reasoning_effort=payload.reasoning_effort,
        )
        return user_message, agent_attachments, usable_history

    @app.post("/api/conversations/{conversation_id}/messages")
    async def send_message(
        request: Request,
        conversation_id: str,
    ):
        _api_csrf(request)
        user = require_api_user(request)
        payload, files = await _parse_message_request(request, settings)
        item = database.get_conversation(user.id, conversation_id)
        if item is None:
            raise HTTPException(status_code=404, detail="Conversation not found.")

        lock = app.state.chat_locks[user.id]
        if lock.locked():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Another message is still being processed.",
            )

        async with lock:
            user_message, agent_attachments, usable_history = (
                await prepare_user_message(
                    user=user,
                    conversation_id=conversation_id,
                    payload=payload,
                    files=files,
                )
            )

            response_started_at = time.monotonic()
            agent_model = (
                FAST_WEB_SEARCH_MODEL
                if payload.web_search_mode == "required"
                else payload.model
            )
            try:
                answer = await app.state.agent_client.chat(
                    model=agent_model,
                    reasoning_effort=payload.reasoning_effort,
                    memory=database.get_memory(user.id)["content"],
                    messages=usable_history,
                    user_message=payload.content,
                    attachments=agent_attachments,
                    output_format=payload.output_format,
                    web_search_mode=payload.web_search_mode,
                )
            except AgentServiceError as exc:
                database.mark_message_error(user.id, user_message["id"], str(exc))
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=str(exc),
                ) from exc
            except Exception as exc:
                database.mark_message_error(
                    user.id,
                    user_message["id"],
                    "Unexpected hosted-agent failure.",
                )
                logger.exception("Unexpected hosted-agent call failure")
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail="Hosted Agent 호출 중 예기치 않은 오류가 발생했습니다.",
                ) from exc

            database.mark_message_complete(user.id, user_message["id"])
            user_message["status"] = "complete"
            if payload.output_format == "pptx":
                try:
                    deck = parse_deck_response(answer)
                    attachment_id = str(uuid.uuid4())
                    output_path = (
                        settings.upload_dir
                        / str(user.id)
                        / conversation_id
                        / f"{attachment_id}.pptx"
                    )
                    slide_count = build_presentation(deck, output_path)
                    assistant_message = database.add_message(
                        user.id,
                        conversation_id,
                        "assistant",
                        f"{slide_count}장짜리 PPT를 만들었습니다. 아래 파일을 다운로드하세요.",
                        model=agent_model,
                        reasoning_effort=payload.reasoning_effort,
                        duration_ms=round(
                            (time.monotonic() - response_started_at) * 1000
                        ),
                    )
                    generated = database.add_attachment(
                        attachment_id=attachment_id,
                        message_id=assistant_message["id"],
                        conversation_id=conversation_id,
                        user_id=user.id,
                        filename=_presentation_download_name(deck.title),
                        mime_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
                        size_bytes=output_path.stat().st_size,
                        storage_path=str(output_path),
                        attachment_kind="generated",
                    )
                    assistant_message["attachments"] = [generated]
                except (PresentationFormatError, OSError, ValueError) as exc:
                    if "output_path" in locals():
                        output_path.unlink(missing_ok=True)
                    database.mark_message_error(
                        user.id,
                        user_message["id"],
                        str(exc),
                    )
                    raise HTTPException(
                        status_code=status.HTTP_502_BAD_GATEWAY,
                        detail="PPT 슬라이드 생성 형식을 처리하지 못했습니다.",
                    ) from exc
            else:
                assistant_message = database.add_message(
                    user.id,
                    conversation_id,
                    "assistant",
                    answer,
                    model=agent_model,
                    reasoning_effort=payload.reasoning_effort,
                    duration_ms=round(
                        (time.monotonic() - response_started_at) * 1000
                    ),
                )

        return {
            "user_message": user_message,
            "assistant_message": assistant_message,
            "conversation": database.get_conversation(user.id, conversation_id),
        }

    @app.post("/api/conversations/{conversation_id}/messages/stream")
    async def stream_message(request: Request, conversation_id: str):
        _api_csrf(request)
        user = require_api_user(request)
        payload, files = await _parse_message_request(request, settings)
        if payload.output_format != "text":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Streaming is available for text answers only.",
            )
        if database.get_conversation(user.id, conversation_id) is None:
            raise HTTPException(status_code=404, detail="Conversation not found.")

        lock = app.state.chat_locks[user.id]
        if lock.locked():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Another message is still being processed.",
            )

        async def events():
            user_message: dict[str, Any] | None = None
            async with lock:
                try:
                    user_message, agent_attachments, usable_history = (
                        await prepare_user_message(
                            user=user,
                            conversation_id=conversation_id,
                            payload=payload,
                            files=files,
                        )
                    )
                    yield json.dumps(
                        {"type": "started", "user_message": user_message},
                        ensure_ascii=False,
                    ) + "\n"
                    response_started_at = time.monotonic()
                    agent_model = (
                        FAST_WEB_SEARCH_MODEL
                        if payload.web_search_mode == "required"
                        else payload.model
                    )
                    answer_parts: list[str] = []
                    async for chunk in app.state.agent_client.chat_stream(
                        model=agent_model,
                        reasoning_effort=payload.reasoning_effort,
                        memory=database.get_memory(user.id)["content"],
                        messages=usable_history,
                        user_message=payload.content,
                        attachments=agent_attachments,
                        output_format="text",
                        web_search_mode=payload.web_search_mode,
                    ):
                        answer_parts.append(chunk)
                        yield json.dumps(
                            {"type": "delta", "delta": chunk},
                            ensure_ascii=False,
                        ) + "\n"

                    answer = "".join(answer_parts).strip()
                    if not answer:
                        raise AgentServiceError(
                            "The Foundry agent returned an empty answer."
                        )
                    database.mark_message_complete(user.id, user_message["id"])
                    user_message["status"] = "complete"
                    assistant_message = database.add_message(
                        user.id,
                        conversation_id,
                        "assistant",
                        answer,
                        model=agent_model,
                        reasoning_effort=payload.reasoning_effort,
                        duration_ms=round(
                            (time.monotonic() - response_started_at) * 1000
                        ),
                    )
                    yield json.dumps(
                        {
                            "type": "done",
                            "user_message": user_message,
                            "assistant_message": assistant_message,
                            "conversation": database.get_conversation(
                                user.id, conversation_id
                            ),
                        },
                        ensure_ascii=False,
                    ) + "\n"
                except AgentServiceError as exc:
                    if user_message is not None:
                        database.mark_message_error(
                            user.id, user_message["id"], str(exc)
                        )
                    yield json.dumps(
                        {"type": "error", "detail": str(exc)},
                        ensure_ascii=False,
                    ) + "\n"
                except Exception:
                    if user_message is not None:
                        database.mark_message_error(
                            user.id,
                            user_message["id"],
                            "Unexpected hosted-agent failure.",
                        )
                    logger.exception("Unexpected streaming agent call failure")
                    yield json.dumps(
                        {
                            "type": "error",
                            "detail": (
                                "Hosted Agent 호출 중 예기치 않은 오류가 "
                                "발생했습니다."
                            ),
                        },
                        ensure_ascii=False,
                    ) + "\n"

        return StreamingResponse(
            events(),
            media_type="application/x-ndjson",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/api/attachments/{attachment_id}")
    async def download_attachment(request: Request, attachment_id: str):
        user = require_api_user(request)
        attachment = database.get_attachment(user.id, attachment_id)
        if attachment is None:
            raise HTTPException(status_code=404, detail="Attachment not found.")
        file_path = Path(attachment["storage_path"])
        if not file_path.is_file():
            raise HTTPException(status_code=404, detail="Attachment file is missing.")
        return FileResponse(
            file_path,
            media_type=attachment["mime_type"],
            filename=attachment["filename"],
            headers={"Cache-Control": "private, no-store"},
        )

    @app.get("/api/memory")
    async def memory(request: Request):
        user = require_api_user(request)
        return {"memory": database.get_memory(user.id)}

    @app.put("/api/memory")
    async def update_memory(request: Request, payload: MemoryUpdate):
        _api_csrf(request)
        user = require_api_user(request)
        return {"memory": database.set_memory(user.id, payload.content.strip())}

    @app.delete("/api/memory", status_code=204)
    async def delete_memory(request: Request):
        _api_csrf(request)
        user = require_api_user(request)
        database.delete_memory(user.id)
        return None

    @app.put("/api/password")
    async def update_password(request: Request, payload: PasswordUpdate):
        _api_csrf(request)
        user = require_api_user(request)
        if not database.verify_password(user.id, payload.current_password):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="현재 비밀번호가 올바르지 않습니다.",
            )
        if database.verify_password(user.id, payload.new_password):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="현재 비밀번호와 다른 새 비밀번호를 사용해 주세요.",
            )
        if payload.new_password != payload.confirm_password:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="새 비밀번호가 서로 일치하지 않습니다.",
            )
        error = password_validation_error(payload.new_password, user.username)
        if error:
            raise HTTPException(status_code=400, detail=error)
        database.change_password(user.id, payload.new_password)
        updated_user = database.get_user(user.username)
        if updated_user is None:
            raise RuntimeError("User disappeared after password update.")
        request.session["session_version"] = updated_user.session_version
        return {"ok": True}

    return app
