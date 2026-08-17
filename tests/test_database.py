from __future__ import annotations

import sqlite3

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
