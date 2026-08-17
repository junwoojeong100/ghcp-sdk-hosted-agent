from __future__ import annotations

import os
import sqlite3
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from .config import CHAT_USERS

Role = Literal["user", "assistant"]


def utc_now() -> datetime:
    return datetime.now(UTC)


def utc_iso(value: datetime | None = None) -> str:
    return (value or utc_now()).isoformat()


@dataclass(frozen=True, slots=True)
class User:
    id: int
    username: str
    must_change_password: bool
    locked_until: str | None
    session_version: int


@dataclass(frozen=True, slots=True)
class LoginResult:
    user: User | None
    error: Literal["invalid", "locked"] | None = None
    locked_until: str | None = None


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.password_hasher = PasswordHasher(
            time_cost=2,
            memory_cost=19_456,
            parallelism=1,
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def initialize(self, bootstrap_password: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            network_storage = bool(os.getenv("WEBSITE_INSTANCE_ID")) or str(
                self.path
            ).startswith("/home/")
            connection.execute(
                "PRAGMA journal_mode = DELETE"
                if network_storage
                else "PRAGMA journal_mode = WAL"
            )
            connection.execute(
                "PRAGMA synchronous = FULL"
                if network_storage
                else "PRAGMA synchronous = NORMAL"
            )
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    must_change_password INTEGER NOT NULL DEFAULT 1,
                    failed_attempts INTEGER NOT NULL DEFAULT 0,
                    locked_until TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    password_changed_at TEXT,
                    session_version INTEGER NOT NULL DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    title TEXT NOT NULL,
                    model TEXT NOT NULL,
                    reasoning_effort TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_conversations_user_updated
                    ON conversations(user_id, updated_at DESC);

                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL
                        REFERENCES conversations(id) ON DELETE CASCADE,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                    content TEXT NOT NULL,
                    model TEXT,
                    reasoning_effort TEXT,
                    status TEXT NOT NULL DEFAULT 'complete'
                        CHECK (status IN ('pending', 'complete', 'error')),
                    error TEXT,
                    duration_ms INTEGER,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_messages_conversation_created
                    ON messages(conversation_id, created_at);

                CREATE TABLE IF NOT EXISTS attachments (
                    id TEXT PRIMARY KEY,
                    message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                    conversation_id TEXT NOT NULL
                        REFERENCES conversations(id) ON DELETE CASCADE,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    filename TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    storage_path TEXT NOT NULL,
                    attachment_kind TEXT NOT NULL DEFAULT 'upload'
                        CHECK (attachment_kind IN ('upload', 'generated')),
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_attachments_message
                    ON attachments(message_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_attachments_user
                    ON attachments(user_id, created_at);

                CREATE TABLE IF NOT EXISTS memories (
                    user_id INTEGER PRIMARY KEY
                        REFERENCES users(id) ON DELETE CASCADE,
                    content TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            user_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(users)").fetchall()
            }
            if "session_version" not in user_columns:
                connection.execute(
                    """
                    ALTER TABLE users
                    ADD COLUMN session_version INTEGER NOT NULL DEFAULT 1
                    """
                )
            message_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(messages)"
                ).fetchall()
            }
            if "duration_ms" not in message_columns:
                connection.execute(
                    "ALTER TABLE messages ADD COLUMN duration_ms INTEGER"
                )

            existing = {
                row["username"]
                for row in connection.execute("SELECT username FROM users")
            }
            now = utc_iso()
            password_hash = self.password_hasher.hash(bootstrap_password)
            for username in CHAT_USERS:
                if username not in existing:
                    connection.execute(
                        """
                        INSERT INTO users (
                            username, password_hash, must_change_password,
                            created_at, updated_at
                        ) VALUES (?, ?, 1, ?, ?)
                        """,
                        (username, password_hash, now, now),
                    )

    @staticmethod
    def _user_from_row(row: sqlite3.Row | None) -> User | None:
        if row is None:
            return None
        return User(
            id=row["id"],
            username=row["username"],
            must_change_password=bool(row["must_change_password"]),
            locked_until=row["locked_until"],
            session_version=int(row["session_version"]),
        )

    def get_user(self, username: str) -> User | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, username, must_change_password, locked_until,
                       session_version
                FROM users WHERE username = ?
                """,
                (username,),
            ).fetchone()
        return self._user_from_row(row)

    def authenticate(self, username: str, password: str) -> LoginResult:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE username = ?",
                (username,),
            ).fetchone()
            if row is None:
                return LoginResult(user=None, error="invalid")

            locked_until = row["locked_until"]
            if locked_until and datetime.fromisoformat(locked_until) > utc_now():
                return LoginResult(
                    user=None,
                    error="locked",
                    locked_until=locked_until,
                )

            try:
                valid = self.password_hasher.verify(row["password_hash"], password)
            except (VerifyMismatchError, InvalidHashError):
                valid = False

            if not valid:
                failed_attempts = int(row["failed_attempts"]) + 1
                new_locked_until = (
                    utc_iso(utc_now() + timedelta(minutes=15))
                    if failed_attempts >= 5
                    else None
                )
                connection.execute(
                    """
                    UPDATE users
                    SET failed_attempts = ?, locked_until = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        0 if new_locked_until else failed_attempts,
                        new_locked_until,
                        utc_iso(),
                        row["id"],
                    ),
                )
                return LoginResult(
                    user=None,
                    error="locked" if new_locked_until else "invalid",
                    locked_until=new_locked_until,
                )

            new_hash = (
                self.password_hasher.hash(password)
                if self.password_hasher.check_needs_rehash(row["password_hash"])
                else row["password_hash"]
            )
            connection.execute(
                """
                UPDATE users
                SET password_hash = ?, failed_attempts = 0, locked_until = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (new_hash, utc_iso(), row["id"]),
            )
            return LoginResult(user=self._user_from_row(row))

    def verify_password(self, user_id: int, password: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT password_hash FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
        if row is None:
            return False
        try:
            return self.password_hasher.verify(row["password_hash"], password)
        except (VerifyMismatchError, InvalidHashError):
            return False

    def change_password(self, user_id: int, new_password: str) -> None:
        now = utc_iso()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE users
                SET password_hash = ?, must_change_password = 0,
                    failed_attempts = 0, locked_until = NULL,
                    password_changed_at = ?, updated_at = ?,
                    session_version = session_version + 1
                WHERE id = ?
                """,
                (self.password_hasher.hash(new_password), now, now, user_id),
            )

    def create_conversation(
        self,
        user_id: int,
        model: str,
        reasoning_effort: str,
        title: str = "새 대화",
    ) -> dict[str, Any]:
        conversation_id = str(uuid.uuid4())
        now = utc_iso()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO conversations (
                    id, user_id, title, model, reasoning_effort,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    conversation_id,
                    user_id,
                    title[:80],
                    model,
                    reasoning_effort,
                    now,
                    now,
                ),
            )
        return self.get_conversation(user_id, conversation_id) or {}

    def list_conversations(self, user_id: int) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, title, model, reasoning_effort, created_at, updated_at
                FROM conversations
                WHERE user_id = ?
                ORDER BY updated_at DESC
                """,
                (user_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_conversation(
        self, user_id: int, conversation_id: str
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, title, model, reasoning_effort, created_at, updated_at
                FROM conversations
                WHERE id = ? AND user_id = ?
                """,
                (conversation_id, user_id),
            ).fetchone()
        return dict(row) if row else None

    def update_conversation(
        self,
        user_id: int,
        conversation_id: str,
        *,
        title: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any] | None:
        updates: list[str] = []
        values: list[Any] = []
        if title is not None:
            updates.append("title = ?")
            values.append(title.strip()[:80] or "새 대화")
        if model is not None:
            updates.append("model = ?")
            values.append(model)
        if reasoning_effort is not None:
            updates.append("reasoning_effort = ?")
            values.append(reasoning_effort)
        if not updates:
            return self.get_conversation(user_id, conversation_id)

        updates.append("updated_at = ?")
        values.append(utc_iso())
        values.extend((conversation_id, user_id))
        with self._connect() as connection:
            cursor = connection.execute(
                f"""
                UPDATE conversations SET {", ".join(updates)}
                WHERE id = ? AND user_id = ?
                """,
                values,
            )
            if cursor.rowcount == 0:
                return None
        return self.get_conversation(user_id, conversation_id)

    def set_title_from_first_message(
        self, user_id: int, conversation_id: str, content: str
    ) -> None:
        title = " ".join(content.strip().split())
        if len(title) > 42:
            title = f"{title[:42].rstrip()}..."
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE conversations
                SET title = ?, updated_at = ?
                WHERE id = ? AND user_id = ? AND title = '새 대화'
                """,
                (title or "새 대화", utc_iso(), conversation_id, user_id),
            )

    def delete_conversation(self, user_id: int, conversation_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM conversations WHERE id = ? AND user_id = ?",
                (conversation_id, user_id),
            )
        return cursor.rowcount > 0

    def delete_all_conversations(self, user_id: int) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM conversations WHERE user_id = ?",
                (user_id,),
            )
        return cursor.rowcount

    def add_message(
        self,
        user_id: int,
        conversation_id: str,
        role: Role,
        content: str,
        *,
        model: str | None = None,
        reasoning_effort: str | None = None,
        status: str = "complete",
        error: str | None = None,
        duration_ms: int | None = None,
    ) -> dict[str, Any]:
        message_id = str(uuid.uuid4())
        created_at = utc_iso()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO messages (
                    id, conversation_id, user_id, role, content, model,
                    reasoning_effort, status, error, duration_ms, created_at
                )
                SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                WHERE EXISTS (
                    SELECT 1 FROM conversations WHERE id = ? AND user_id = ?
                )
                """,
                (
                    message_id,
                    conversation_id,
                    user_id,
                    role,
                    content,
                    model,
                    reasoning_effort,
                    status,
                    error,
                    duration_ms,
                    created_at,
                    conversation_id,
                    user_id,
                ),
            )
            if cursor.rowcount == 0:
                raise ValueError("Message conversation does not belong to the user.")
            connection.execute(
                """
                UPDATE conversations SET updated_at = ?
                WHERE id = ? AND user_id = ?
                """,
                (created_at, conversation_id, user_id),
            )
        return {
            "id": message_id,
            "conversation_id": conversation_id,
            "role": role,
            "content": content,
            "model": model,
            "reasoning_effort": reasoning_effort,
            "status": status,
            "error": error,
            "duration_ms": duration_ms,
            "created_at": created_at,
            "attachments": [],
        }

    @staticmethod
    def _attachment_result(
        *,
        attachment_id: str,
        message_id: str,
        filename: str,
        mime_type: str,
        size_bytes: int,
        attachment_kind: str,
        created_at: str,
    ) -> dict[str, Any]:
        return {
            "id": attachment_id,
            "message_id": message_id,
            "filename": filename,
            "mime_type": mime_type,
            "size_bytes": size_bytes,
            "kind": attachment_kind,
            "created_at": created_at,
            "download_url": f"/api/attachments/{attachment_id}",
        }

    def add_attachments(
        self,
        attachments: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        with self._connect() as connection:
            for attachment in attachments:
                created_at = utc_iso()
                cursor = connection.execute(
                    """
                    INSERT INTO attachments (
                        id, message_id, conversation_id, user_id, filename,
                        mime_type, size_bytes, storage_path, attachment_kind,
                        created_at
                    )
                    SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    WHERE EXISTS (
                        SELECT 1 FROM messages
                        WHERE id = ? AND conversation_id = ? AND user_id = ?
                    )
                    """,
                    (
                        attachment["attachment_id"],
                        attachment["message_id"],
                        attachment["conversation_id"],
                        attachment["user_id"],
                        attachment["filename"],
                        attachment["mime_type"],
                        attachment["size_bytes"],
                        attachment["storage_path"],
                        attachment["attachment_kind"],
                        created_at,
                        attachment["message_id"],
                        attachment["conversation_id"],
                        attachment["user_id"],
                    ),
                )
                if cursor.rowcount == 0:
                    raise ValueError(
                        "Attachment message does not belong to the user."
                    )
                results.append(
                    self._attachment_result(
                        attachment_id=attachment["attachment_id"],
                        message_id=attachment["message_id"],
                        filename=attachment["filename"],
                        mime_type=attachment["mime_type"],
                        size_bytes=attachment["size_bytes"],
                        attachment_kind=attachment["attachment_kind"],
                        created_at=created_at,
                    )
                )
        return results

    def add_attachment(
        self,
        *,
        attachment_id: str,
        message_id: str,
        conversation_id: str,
        user_id: int,
        filename: str,
        mime_type: str,
        size_bytes: int,
        storage_path: str,
        attachment_kind: Literal["upload", "generated"] = "upload",
    ) -> dict[str, Any]:
        return self.add_attachments(
            [
                {
                    "attachment_id": attachment_id,
                    "message_id": message_id,
                    "conversation_id": conversation_id,
                    "user_id": user_id,
                    "filename": filename,
                    "mime_type": mime_type,
                    "size_bytes": size_bytes,
                    "storage_path": storage_path,
                    "attachment_kind": attachment_kind,
                }
            ]
        )[0]

    def complete_message_exchange(
        self,
        *,
        user_id: int,
        conversation_id: str,
        user_message_id: str,
        content: str,
        model: str,
        reasoning_effort: str,
        duration_ms: int,
    ) -> dict[str, Any]:
        assistant_message_id = str(uuid.uuid4())
        created_at = utc_iso()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO messages (
                    id, conversation_id, user_id, role, content, model,
                    reasoning_effort, status, error, duration_ms, created_at
                )
                SELECT ?, ?, ?, 'assistant', ?, ?, ?, 'complete', NULL, ?, ?
                WHERE EXISTS (
                    SELECT 1 FROM conversations WHERE id = ? AND user_id = ?
                )
                """,
                (
                    assistant_message_id,
                    conversation_id,
                    user_id,
                    content,
                    model,
                    reasoning_effort,
                    duration_ms,
                    created_at,
                    conversation_id,
                    user_id,
                ),
            )
            if cursor.rowcount == 0:
                raise ValueError("Conversation does not belong to the user.")
            cursor = connection.execute(
                """
                UPDATE messages SET status = 'complete', error = NULL
                WHERE id = ? AND conversation_id = ? AND user_id = ?
                  AND role = 'user' AND status = 'pending'
                """,
                (user_message_id, conversation_id, user_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Pending user message was not found.")
            connection.execute(
                """
                UPDATE conversations SET updated_at = ?
                WHERE id = ? AND user_id = ?
                """,
                (created_at, conversation_id, user_id),
            )
        return {
            "id": assistant_message_id,
            "conversation_id": conversation_id,
            "role": "assistant",
            "content": content,
            "model": model,
            "reasoning_effort": reasoning_effort,
            "status": "complete",
            "error": None,
            "duration_ms": duration_ms,
            "created_at": created_at,
            "attachments": [],
        }

    def complete_presentation_exchange(
        self,
        *,
        user_id: int,
        conversation_id: str,
        user_message_id: str,
        content: str,
        model: str,
        reasoning_effort: str,
        duration_ms: int,
        attachment_id: str,
        filename: str,
        mime_type: str,
        size_bytes: int,
        storage_path: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        assistant_message_id = str(uuid.uuid4())
        created_at = utc_iso()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO messages (
                    id, conversation_id, user_id, role, content, model,
                    reasoning_effort, status, error, duration_ms, created_at
                )
                SELECT ?, ?, ?, 'assistant', ?, ?, ?, 'complete', NULL, ?, ?
                WHERE EXISTS (
                    SELECT 1 FROM conversations WHERE id = ? AND user_id = ?
                )
                """,
                (
                    assistant_message_id,
                    conversation_id,
                    user_id,
                    content,
                    model,
                    reasoning_effort,
                    duration_ms,
                    created_at,
                    conversation_id,
                    user_id,
                ),
            )
            if cursor.rowcount == 0:
                raise ValueError("Conversation does not belong to the user.")
            connection.execute(
                """
                INSERT INTO attachments (
                    id, message_id, conversation_id, user_id, filename,
                    mime_type, size_bytes, storage_path, attachment_kind,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'generated', ?)
                """,
                (
                    attachment_id,
                    assistant_message_id,
                    conversation_id,
                    user_id,
                    filename,
                    mime_type,
                    size_bytes,
                    storage_path,
                    created_at,
                ),
            )
            cursor = connection.execute(
                """
                UPDATE messages SET status = 'complete', error = NULL
                WHERE id = ? AND conversation_id = ? AND user_id = ?
                  AND role = 'user' AND status = 'pending'
                """,
                (user_message_id, conversation_id, user_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Pending user message was not found.")
            connection.execute(
                """
                UPDATE conversations SET updated_at = ?
                WHERE id = ? AND user_id = ?
                """,
                (created_at, conversation_id, user_id),
            )

        assistant_message = {
            "id": assistant_message_id,
            "conversation_id": conversation_id,
            "role": "assistant",
            "content": content,
            "model": model,
            "reasoning_effort": reasoning_effort,
            "status": "complete",
            "error": None,
            "duration_ms": duration_ms,
            "created_at": created_at,
            "attachments": [],
        }
        attachment = self._attachment_result(
            attachment_id=attachment_id,
            message_id=assistant_message_id,
            filename=filename,
            mime_type=mime_type,
            size_bytes=size_bytes,
            attachment_kind="generated",
            created_at=created_at,
        )
        assistant_message["attachments"] = [attachment]
        return assistant_message, attachment

    def mark_message_error(self, user_id: int, message_id: str, error: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE messages SET status = 'error', error = ?
                WHERE id = ? AND user_id = ?
                """,
                (error[:500], message_id, user_id),
            )

    def mark_message_complete(self, user_id: int, message_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE messages SET status = 'complete', error = NULL
                WHERE id = ? AND user_id = ?
                """,
                (message_id, user_id),
            )

    def list_messages(
        self, user_id: int, conversation_id: str, limit: int = 200
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM (
                    SELECT id, conversation_id, role, content, model,
                           reasoning_effort, status, error, duration_ms,
                           created_at
                    FROM messages
                    WHERE conversation_id = ? AND user_id = ?
                    ORDER BY created_at DESC
                    LIMIT ?
                )
                ORDER BY created_at ASC
                """,
                (conversation_id, user_id, limit),
            ).fetchall()
            message_ids = [row["id"] for row in rows]
            attachments_by_message: dict[str, list[dict[str, Any]]] = {
                message_id: [] for message_id in message_ids
            }
            if message_ids:
                placeholders = ",".join("?" for _ in message_ids)
                attachment_rows = connection.execute(
                    f"""
                    SELECT id, message_id, filename, mime_type, size_bytes,
                           attachment_kind, created_at
                    FROM attachments
                    WHERE user_id = ? AND message_id IN ({placeholders})
                    ORDER BY created_at ASC
                    """,
                    (user_id, *message_ids),
                ).fetchall()
                for attachment in attachment_rows:
                    item = {
                        "id": attachment["id"],
                        "message_id": attachment["message_id"],
                        "filename": attachment["filename"],
                        "mime_type": attachment["mime_type"],
                        "size_bytes": attachment["size_bytes"],
                        "kind": attachment["attachment_kind"],
                        "created_at": attachment["created_at"],
                        "download_url": f"/api/attachments/{attachment['id']}",
                    }
                    attachments_by_message[attachment["message_id"]].append(item)

        messages: list[dict[str, Any]] = []
        for row in rows:
            message = dict(row)
            message["attachments"] = attachments_by_message.get(row["id"], [])
            messages.append(message)
        return messages

    def get_attachment(
        self, user_id: int, attachment_id: str
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, message_id, conversation_id, filename, mime_type,
                       size_bytes, storage_path, attachment_kind, created_at
                FROM attachments
                WHERE id = ? AND user_id = ?
                """,
                (attachment_id, user_id),
            ).fetchone()
        return dict(row) if row else None

    def attachment_paths_for_conversation(
        self, user_id: int, conversation_id: str
    ) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT storage_path FROM attachments
                WHERE user_id = ? AND conversation_id = ?
                """,
                (user_id, conversation_id),
            ).fetchall()
        return [row["storage_path"] for row in rows]

    def attachment_paths_for_user(self, user_id: int) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT storage_path FROM attachments WHERE user_id = ?",
                (user_id,),
            ).fetchall()
        return [row["storage_path"] for row in rows]

    def get_memory(self, user_id: int) -> dict[str, str]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT content, updated_at FROM memories WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if row is None:
            return {"content": "", "updated_at": ""}
        return dict(row)

    def set_memory(self, user_id: int, content: str) -> dict[str, str]:
        updated_at = utc_iso()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO memories (user_id, content, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE
                SET content = excluded.content, updated_at = excluded.updated_at
                """,
                (user_id, content, updated_at),
            )
        return {"content": content, "updated_at": updated_at}

    def delete_memory(self, user_id: int) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM memories WHERE user_id = ?",
                (user_id,),
            )
        return cursor.rowcount > 0

    @staticmethod
    def user_dict(user: User) -> dict[str, Any]:
        return asdict(user)
