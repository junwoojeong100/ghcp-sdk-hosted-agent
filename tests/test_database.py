from __future__ import annotations

import sqlite3

import pytest

from my_chat.database import Database


def test_production_storage_uses_rollback_journal(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("WEBSITE_INSTANCE_ID", "test-instance")
    database = Database(tmp_path / "my-chat.db")

    database.initialize("Bootstrap1234")

    with sqlite3.connect(database.path) as connection:
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        synchronous = connection.execute("PRAGMA synchronous").fetchone()[0]
        message_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(messages)").fetchall()
        }
    assert journal_mode == "delete"
    assert synchronous == 2
    assert "duration_ms" in message_columns


def test_add_message_rejects_missing_conversation(tmp_path) -> None:
    database = Database(tmp_path / "my-chat.db")
    database.initialize("Bootstrap1234")
    user = database.get_user("user1")
    assert user is not None

    with pytest.raises(ValueError, match="does not belong"):
        database.add_message(
            user.id,
            "missing-conversation",
            "user",
            "hello",
        )

    assert database.list_messages(user.id, "missing-conversation") == []


def test_attachment_batch_rolls_back_on_invalid_message(tmp_path) -> None:
    database = Database(tmp_path / "my-chat.db")
    database.initialize("Bootstrap1234")
    user = database.get_user("user1")
    assert user is not None
    conversation = database.create_conversation(user.id, "gpt-5.6-sol", "low")
    message = database.add_message(
        user.id,
        conversation["id"],
        "user",
        "hello",
        status="pending",
    )

    with pytest.raises(ValueError, match="does not belong"):
        database.add_attachments(
            [
                {
                    "attachment_id": "valid-attachment",
                    "message_id": message["id"],
                    "conversation_id": conversation["id"],
                    "user_id": user.id,
                    "filename": "valid.txt",
                    "mime_type": "text/plain",
                    "size_bytes": 5,
                    "storage_path": "/tmp/valid.txt",
                    "attachment_kind": "upload",
                },
                {
                    "attachment_id": "invalid-attachment",
                    "message_id": "missing-message",
                    "conversation_id": conversation["id"],
                    "user_id": user.id,
                    "filename": "invalid.txt",
                    "mime_type": "text/plain",
                    "size_bytes": 7,
                    "storage_path": "/tmp/invalid.txt",
                    "attachment_kind": "upload",
                },
            ]
        )

    stored = database.list_messages(user.id, conversation["id"])
    assert stored[0]["attachments"] == []


def test_completed_exchange_is_atomic(tmp_path) -> None:
    database = Database(tmp_path / "my-chat.db")
    database.initialize("Bootstrap1234")
    user = database.get_user("user1")
    assert user is not None
    conversation = database.create_conversation(user.id, "gpt-5.6-sol", "low")
    user_message = database.add_message(
        user.id,
        conversation["id"],
        "user",
        "hello",
        status="pending",
    )

    with pytest.raises(ValueError, match="Pending user message"):
        database.complete_message_exchange(
            user_id=user.id,
            conversation_id=conversation["id"],
            user_message_id="missing-message",
            content="answer",
            model="gpt-5.6-sol",
            reasoning_effort="low",
            duration_ms=10,
        )

    messages = database.list_messages(user.id, conversation["id"])
    assert len(messages) == 1
    assert messages[0]["id"] == user_message["id"]
    assert messages[0]["status"] == "pending"

    database.complete_message_exchange(
        user_id=user.id,
        conversation_id=conversation["id"],
        user_message_id=user_message["id"],
        content="answer",
        model="gpt-5.6-sol",
        reasoning_effort="low",
        duration_ms=10,
    )

    messages = database.list_messages(user.id, conversation["id"])
    assert [message["status"] for message in messages] == ["complete", "complete"]
    assert [message["role"] for message in messages] == ["user", "assistant"]
