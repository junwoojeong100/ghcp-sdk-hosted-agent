from __future__ import annotations

from io import BytesIO
import json
import re
import sqlite3

from fastapi.testclient import TestClient
from pptx import Presentation


def csrf_from_html(response) -> str:
    match = re.search(r'name="csrf" value="([^"]+)"', response.text)
    assert match
    return match.group(1)


def finish_first_login(
    client: TestClient,
    username: str,
    new_password: str,
) -> str:
    login_page = client.get("/login")
    response = client.post(
        "/login",
        data={
            "username": username,
            "password": "bootstrap-1234",
            "csrf": csrf_from_html(login_page),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/change-password"

    password_page = client.get("/change-password")
    response = client.post(
        "/change-password",
        data={
            "new_password": new_password,
            "confirm_password": new_password,
            "csrf": csrf_from_html(password_page),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/chat"
    return client.get("/api/me").json()["csrf_token"]


def test_first_login_forces_password_change(client: TestClient) -> None:
    login_page = client.get("/login")
    response = client.post(
        "/login",
        data={
            "username": "user1",
            "password": "bootstrap-1234",
            "csrf": csrf_from_html(login_page),
        },
        follow_redirects=False,
    )

    assert response.headers["location"] == "/change-password"
    assert client.get("/chat", follow_redirects=False).headers["location"] == (
        "/change-password"
    )
    assert client.get("/api/conversations").status_code == 403


def test_static_assets_use_same_origin_relative_urls(client: TestClient) -> None:
    response = client.get("/login")

    assert 'href="/static/app.css"' in response.text
    assert 'href="/static/favicon.svg"' in response.text
    assert 'src="/static/theme.js"' in response.text
    assert 'src="/static/favicon.svg" alt=""' in response.text
    assert 'id="theme-toggle"' in response.text
    assert "<h1>My Chat</h1>" in response.text
    assert "My Chat" in response.text
    assert re.findall(r'<option value="([^"]+)">', response.text) == [
        "user1",
        "user2",
        "user3",
    ]
    assert ">F<" not in response.text
    assert "http://testserver/static" not in response.text

    favicon = client.get("/static/favicon.svg")
    assert favicon.status_code == 200
    assert "<title>My Chat</title>" in favicon.text
    assert "#58a6ff" in favicon.text
    assert "#238c70" not in favicon.text
    app_script = client.get("/static/app.js").text
    assert "function isNearBottom()" in app_script
    assert "state.autoFollow = isNearBottom()" in app_script


def test_first_login_rejects_bootstrap_password(client: TestClient) -> None:
    login_page = client.get("/login")
    client.post(
        "/login",
        data={
            "username": "user1",
            "password": "bootstrap-1234",
            "csrf": csrf_from_html(login_page),
        },
    )
    password_page = client.get("/change-password")

    response = client.post(
        "/change-password",
        data={
            "new_password": "bootstrap-1234",
            "confirm_password": "bootstrap-1234",
            "csrf": csrf_from_html(password_page),
        },
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert "초기 비밀번호와 다른" in response.text


def test_password_change_invalidates_other_sessions(app) -> None:
    with TestClient(app) as first, TestClient(app) as second:
        for client in (first, second):
            login_page = client.get("/login")
            response = client.post(
                "/login",
                data={
                    "username": "user1",
                    "password": "bootstrap-1234",
                    "csrf": csrf_from_html(login_page),
                },
                follow_redirects=False,
            )
            assert response.headers["location"] == "/change-password"

        first_page = first.get("/change-password")
        first.post(
            "/change-password",
            data={
                "new_password": "MySecure1234!",
                "confirm_password": "MySecure1234!",
                "csrf": csrf_from_html(first_page),
            },
            follow_redirects=False,
        )

        response = second.get("/change-password", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"


def test_chat_memory_and_user_isolation(app, fake_agent) -> None:
    with TestClient(app) as user1_client:
        user1_csrf = finish_first_login(user1_client, "user1", "MySecure1234!")
        assert 'id="web-search-select"' in user1_client.get("/chat").text

        models = user1_client.get("/api/models").json()
        assert models["source"] == "copilot"
        assert models["models"][0]["id"] == "gpt-5.6-sol"
        refreshed = user1_client.get("/api/models?refresh=true")
        assert refreshed.status_code == 200
        assert fake_agent.calls[-1] == {
            "type": "list_models",
            "force_refresh": True,
        }

        created = user1_client.post(
            "/api/conversations",
            headers={"X-CSRF-Token": user1_csrf},
            json={"model": "gpt-5.6-sol", "reasoning_effort": "high"},
        )
        assert created.status_code == 201
        conversation_id = created.json()["conversation"]["id"]

        memory = user1_client.put(
            "/api/memory",
            headers={"X-CSRF-Token": user1_csrf},
            json={"content": "항상 한국어로 간결하게 답해줘."},
        )
        assert memory.status_code == 200

        sent = user1_client.post(
            f"/api/conversations/{conversation_id}/messages",
            headers={"X-CSRF-Token": user1_csrf},
            json={
                "content": "오늘 일정 정리해줘",
                "model": "gpt-5.6-sol",
                "reasoning_effort": "high",
                "web_search_mode": "required",
            },
        )
        assert sent.status_code == 200
        assert sent.json()["assistant_message"]["content"].startswith("가짜 답변:")
        assert sent.json()["assistant_message"]["duration_ms"] >= 0
        assert fake_agent.calls[-1]["memory"] == "항상 한국어로 간결하게 답해줘."
        assert fake_agent.calls[-1]["web_search_mode"] == "required"
        assert fake_agent.calls[-1]["model"] == "gpt-5.6-luna"

        history = user1_client.get(
            f"/api/conversations/{conversation_id}"
        ).json()
        assert [message["role"] for message in history["messages"]] == [
            "user",
            "assistant",
        ]

    with TestClient(app) as user2_client:
        finish_first_login(user2_client, "user2", "MySecure1234!")
        assert (
            user2_client.get(f"/api/conversations/{conversation_id}").status_code
            == 404
        )
        assert user2_client.get("/api/memory").json()["memory"]["content"] == ""


def test_conversation_and_memory_deletion(app) -> None:
    with TestClient(app) as client:
        csrf = finish_first_login(client, "user3", "MySecure1234!")
        created = client.post(
            "/api/conversations",
            headers={"X-CSRF-Token": csrf},
            json={"model": "gpt-5.6-sol", "reasoning_effort": "default"},
        ).json()
        conversation_id = created["conversation"]["id"]

        client.put(
            "/api/memory",
            headers={"X-CSRF-Token": csrf},
            json={"content": "테스트 메모리"},
        )
        assert (
            client.delete(
                f"/api/conversations/{conversation_id}",
                headers={"X-CSRF-Token": csrf},
            ).status_code
            == 204
        )
        assert (
            client.delete(
                "/api/memory",
                headers={"X-CSRF-Token": csrf},
            ).status_code
            == 204
        )
        assert client.get("/api/conversations").json()["conversations"] == []
        assert client.get("/api/memory").json()["memory"]["content"] == ""


def test_streaming_chat_returns_deltas(app) -> None:
    with TestClient(app) as client:
        csrf = finish_first_login(client, "user2", "MySecure1234!")
        conversation_id = client.post(
            "/api/conversations",
            headers={"X-CSRF-Token": csrf},
            json={"model": "gpt-5.6-sol", "reasoning_effort": "low"},
        ).json()["conversation"]["id"]

        response = client.post(
            f"/api/conversations/{conversation_id}/messages/stream",
            headers={"X-CSRF-Token": csrf},
            data={
                "content": "스트리밍 테스트",
                "model": "gpt-5.6-sol",
                "reasoning_effort": "low",
                "web_search_mode": "disabled",
                "output_format": "text",
            },
        )

        assert response.status_code == 200
        events = [
            json.loads(line)
            for line in response.text.splitlines()
            if line.strip()
        ]
        deltas = [
            event["delta"] for event in events if event["type"] == "delta"
        ]
        done = next(event for event in events if event["type"] == "done")
        assert "".join(deltas) == "가짜 답변: 스트리밍 테스트"
        assert done["assistant_message"]["duration_ms"] >= 0


def test_file_attachment_is_persisted_and_user_isolated(app, fake_agent) -> None:
    with TestClient(app) as user1_client:
        csrf = finish_first_login(user1_client, "user1", "MySecure1234!")
        conversation_id = user1_client.post(
            "/api/conversations",
            headers={"X-CSRF-Token": csrf},
            json={"model": "gpt-5.6-sol", "reasoning_effort": "default"},
        ).json()["conversation"]["id"]

        sent = user1_client.post(
            f"/api/conversations/{conversation_id}/messages",
            headers={"X-CSRF-Token": csrf},
            data={
                "content": "첨부 내용을 요약해줘",
                "model": "gpt-5.6-sol",
                "reasoning_effort": "default",
                "output_format": "text",
            },
            files={
                "files": (
                    "my-notes.txt",
                    b"My Chat attachment test",
                    "text/plain",
                )
            },
        )

        assert sent.status_code == 200
        attachment = sent.json()["user_message"]["attachments"][0]
        assert attachment["filename"] == "my-notes.txt"
        assert fake_agent.calls[-1]["attachments"][0]["filename"] == (
            "my-notes.txt"
        )
        download = user1_client.get(attachment["download_url"])
        assert download.status_code == 200
        assert download.content == b"My Chat attachment test"

    with TestClient(app) as user2_client:
        finish_first_login(user2_client, "user2", "MySecure1234!")
        assert user2_client.get(attachment["download_url"]).status_code == 404


def test_pptx_generation_returns_downloadable_presentation(app) -> None:
    with TestClient(app) as client:
        csrf = finish_first_login(client, "user3", "MySecure1234!")
        conversation_id = client.post(
            "/api/conversations",
            headers={"X-CSRF-Token": csrf},
            json={"model": "gpt-5.6-sol", "reasoning_effort": "high"},
        ).json()["conversation"]["id"]

        sent = client.post(
            f"/api/conversations/{conversation_id}/messages",
            headers={"X-CSRF-Token": csrf},
            data={
                "content": "여행 계획 PPT를 만들어줘",
                "model": "gpt-5.6-sol",
                "reasoning_effort": "high",
                "output_format": "pptx",
            },
        )

        assert sent.status_code == 200
        assistant = sent.json()["assistant_message"]
        attachment = assistant["attachments"][0]
        assert attachment["filename"].endswith(".pptx")
        download = client.get(attachment["download_url"])
        assert download.status_code == 200
        assert download.content.startswith(b"PK")
        presentation = Presentation(BytesIO(download.content))
        assert len(presentation.slides) == 3


def test_unsupported_attachment_type_is_rejected(app) -> None:
    with TestClient(app) as client:
        csrf = finish_first_login(client, "user3", "MySecure1234!")
        conversation_id = client.post(
            "/api/conversations",
            headers={"X-CSRF-Token": csrf},
            json={"model": "gpt-5.6-sol", "reasoning_effort": "default"},
        ).json()["conversation"]["id"]

        response = client.post(
            f"/api/conversations/{conversation_id}/messages",
            headers={"X-CSRF-Token": csrf},
            data={
                "content": "이 파일을 봐줘",
                "model": "gpt-5.6-sol",
                "reasoning_effort": "default",
            },
            files={"files": ("unsafe.exe", b"MZ", "application/octet-stream")},
        )

        assert response.status_code == 400


def test_csrf_is_required_for_mutations(client: TestClient) -> None:
    finish_first_login(client, "user3", "MySecure1234!")

    response = client.post(
        "/api/conversations",
        json={"model": "gpt-5.6-sol", "reasoning_effort": "default"},
    )

    assert response.status_code == 403


def test_long_answers_are_truncated_only_for_reused_context(
    app,
    fake_agent,
) -> None:
    with TestClient(app) as client:
        csrf = finish_first_login(client, "user1", "MySecure1234!")
        conversation_id = client.post(
            "/api/conversations",
            headers={"X-CSRF-Token": csrf},
            json={"model": "gpt-5.6-sol", "reasoning_effort": "low"},
        ).json()["conversation"]["id"]
        user = app.state.database.get_user("user1")
        assert user is not None
        long_answer = f"{'A' * 12_000}{'Z' * 12_000}"
        app.state.database.add_message(
            user.id,
            conversation_id,
            "assistant",
            long_answer,
            model="gpt-5.6-sol",
            reasoning_effort="low",
        )

        response = client.post(
            f"/api/conversations/{conversation_id}/messages",
            headers={"X-CSRF-Token": csrf},
            json={
                "content": "continue",
                "model": "gpt-5.6-sol",
                "reasoning_effort": "low",
            },
        )

        assert response.status_code == 200
        context = fake_agent.calls[-1]["messages"][0]["content"]
        assert len(context) == 20_000
        assert context.startswith("A")
        assert context.endswith("Z")
        assert "content truncated for conversation context" in context
        stored = client.get(
            f"/api/conversations/{conversation_id}"
        ).json()["messages"][0]["content"]
        assert stored == long_answer


def test_upload_database_failure_removes_staged_files(
    app,
    monkeypatch,
) -> None:
    def fail_add_attachments(_attachments):
        raise sqlite3.OperationalError("simulated failure")

    monkeypatch.setattr(
        app.state.database,
        "add_attachments",
        fail_add_attachments,
    )

    with TestClient(app, raise_server_exceptions=False) as client:
        csrf = finish_first_login(client, "user3", "MySecure1234!")
        conversation_id = client.post(
            "/api/conversations",
            headers={"X-CSRF-Token": csrf},
            json={"model": "gpt-5.6-sol", "reasoning_effort": "default"},
        ).json()["conversation"]["id"]

        response = client.post(
            f"/api/conversations/{conversation_id}/messages",
            headers={"X-CSRF-Token": csrf},
            data={
                "content": "첨부 내용을 요약해줘",
                "model": "gpt-5.6-sol",
                "reasoning_effort": "default",
            },
            files={"files": ("notes.txt", b"sample", "text/plain")},
        )

        assert response.status_code == 500
        assert not [
            path
            for path in app.state.settings.upload_dir.rglob("*")
            if path.is_file()
        ]
        messages = client.get(
            f"/api/conversations/{conversation_id}"
        ).json()["messages"]
        assert messages[0]["status"] == "error"
