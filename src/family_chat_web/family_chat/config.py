from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

FAMILY_USERS = ("jw", "yw", "yc", "bm")
FALLBACK_MODELS = (
    {"id": "gpt-5.6-sol", "name": "GPT-5.6 Sol"},
    {"id": "gpt-5.6-terra", "name": "GPT-5.6 Terra"},
    {"id": "gpt-5.6-luna", "name": "GPT-5.6 Luna"},
    {"id": "claude-opus-5", "name": "Claude Opus 5"},
    {"id": "claude-sonnet-5", "name": "Claude Sonnet 5"},
    {"id": "claude-haiku-4.5", "name": "Claude Haiku 4.5"},
    {"id": "gemini-3.7-flash", "name": "Gemini 3.7 Flash"},
    {"id": "mai-code-1.1-flash", "name": "MAI-Code-1.1-Flash"},
)


@dataclass(frozen=True, slots=True)
class Settings:
    app_env: str
    database_path: Path
    session_secret: str
    bootstrap_password: str
    agent_endpoint: str
    token_scope: str
    allowed_hosts: tuple[str, ...]
    cookie_secure: bool
    upload_dir: Path
    max_attachment_bytes: int = 8 * 1024 * 1024
    max_total_attachment_bytes: int = 16 * 1024 * 1024
    max_attachments: int = 5

    @classmethod
    def from_env(cls) -> "Settings":
        app_env = os.getenv("APP_ENV", "development").lower()
        production = app_env == "production"

        default_database = (
            Path("/home/data/family-chat.db")
            if os.getenv("WEBSITE_INSTANCE_ID")
            else Path.cwd() / "data" / "family-chat.db"
        )
        database_path = Path(
            os.getenv("FAMILY_CHAT_DATABASE_PATH", str(default_database))
        ).expanduser()
        upload_dir = Path(
            os.getenv(
                "FAMILY_CHAT_UPLOAD_DIR",
                str(database_path.parent / "uploads"),
            )
        ).expanduser()

        session_secret = os.getenv("APP_SESSION_SECRET", "")
        bootstrap_password = os.getenv("FAMILY_CHAT_BOOTSTRAP_PASSWORD", "")
        if production and len(session_secret) < 32:
            raise RuntimeError(
                "APP_SESSION_SECRET must be at least 32 characters in production."
            )
        if production and len(bootstrap_password) < 10:
            raise RuntimeError(
                "FAMILY_CHAT_BOOTSTRAP_PASSWORD must be at least 10 characters "
                "in production."
            )

        if not session_secret:
            session_secret = "development-only-session-secret-change-me"
        if not bootstrap_password:
            bootstrap_password = "change-me-1234"

        allowed_hosts = tuple(
            host.strip()
            for host in os.getenv("ALLOWED_HOSTS", "*").split(",")
            if host.strip()
        )

        return cls(
            app_env=app_env,
            database_path=database_path,
            session_secret=session_secret,
            bootstrap_password=bootstrap_password,
            agent_endpoint=os.getenv(
                "FOUNDRY_AGENT_ENDPOINT", "http://localhost:8088/responses"
            ),
            token_scope=os.getenv(
                "FOUNDRY_TOKEN_SCOPE", "https://ai.azure.com/.default"
            ),
            allowed_hosts=allowed_hosts or ("*",),
            cookie_secure=production,
            upload_dir=upload_dir,
        )
