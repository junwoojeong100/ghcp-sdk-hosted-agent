from __future__ import annotations

from io import BytesIO
import re

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
            "username": "jw",
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
    assert ">F<" not in response.text
    assert "http://testserver/static" not in response.text

    favicon = client.get("/static/favicon.svg")
    assert favicon.status_code == 200
    assert "<title>My Chat</title>" in favicon.text
    assert "#58a6ff" in favicon.text
    assert "#238c70" not in favicon.text


def test_first_login_rejects_bootstrap_password(client: TestClient) -> None:
    login_page = client.get("/login")
    client.post(
        "/login",
        data={
            "username": "jw",
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
                    "username": "jw",
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
    with TestClient(app) as jw_client:
        jw_csrf = finish_first_login(jw_client, "jw", "MySecure1234!")

        models = jw_client.get("/api/models").json()
        assert models["source"] == "copilot"
        assert models["models"][0]["id"] == "gpt-5.6-sol"
        refreshed = jw_client.get("/api/models?refresh=true")
        assert refreshed.status_code == 200
        assert fake_agent.calls[-1] == {
            "type": "list_models",
            "force_refresh": True,
        }

        created = jw_client.post(
            "/api/conversations",
            headers={"X-CSRF-Token": jw_csrf},
            json={"model": "gpt-5.6-sol", "reasoning_effort": "high"},
        )
        assert created.status_code == 201
        conversation_id = created.json()["conversation"]["id"]

        memory = jw_client.put(
            "/api/memory",
            headers={"X-CSRF-Token": jw_csrf},
            json={"content": "항상 한국어로 간결하게 답해줘."},
        )
        assert memory.status_code == 200

        sent = jw_client.post(
            f"/api/conversations/{conversation_id}/messages",
            headers={"X-CSRF-Token": jw_csrf},
            json={
                "content": "오늘 일정 정리해줘",
                "model": "gpt-5.6-sol",
                "reasoning_effort": "high",
            },
        )
        assert sent.status_code == 200
        assert sent.json()["assistant_message"]["content"].startswith("가짜 답변:")
        assert sent.json()["assistant_message"]["duration_ms"] >= 0
        assert fake_agent.calls[-1]["memory"] == "항상 한국어로 간결하게 답해줘."

        history = jw_client.get(
            f"/api/conversations/{conversation_id}"
        ).json()
        assert [message["role"] for message in history["messages"]] == [
            "user",
            "assistant",
        ]

    with TestClient(app) as yw_client:
        finish_first_login(yw_client, "yw", "MySecure1234!")
        assert (
            yw_client.get(f"/api/conversations/{conversation_id}").status_code
            == 404
        )
        assert yw_client.get("/api/memory").json()["memory"]["content"] == ""


def test_conversation_and_memory_deletion(app) -> None:
    with TestClient(app) as client:
        csrf = finish_first_login(client, "yc", "MySecure1234!")
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


def test_file_attachment_is_persisted_and_user_isolated(app, fake_agent) -> None:
    with TestClient(app) as jw_client:
        csrf = finish_first_login(jw_client, "jw", "MySecure1234!")
        conversation_id = jw_client.post(
            "/api/conversations",
            headers={"X-CSRF-Token": csrf},
            json={"model": "gpt-5.6-sol", "reasoning_effort": "default"},
        ).json()["conversation"]["id"]

        sent = jw_client.post(
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
        download = jw_client.get(attachment["download_url"])
        assert download.status_code == 200
        assert download.content == b"My Chat attachment test"

    with TestClient(app) as yw_client:
        finish_first_login(yw_client, "yw", "MySecure1234!")
        assert yw_client.get(attachment["download_url"]).status_code == 404


def test_pptx_generation_returns_downloadable_presentation(app) -> None:
    with TestClient(app) as client:
        csrf = finish_first_login(client, "yc", "MySecure1234!")
        conversation_id = client.post(
            "/api/conversations",
            headers={"X-CSRF-Token": csrf},
            json={"model": "gpt-5.6-sol", "reasoning_effort": "high"},
        ).json()["conversation"]["id"]

        sent = client.post(
            f"/api/conversations/{conversation_id}/messages",
            headers={"X-CSRF-Token": csrf},
            data={
                "content": "가족 여행 계획 PPT를 만들어줘",
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
        csrf = finish_first_login(client, "bm", "MySecure1234!")
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
    finish_first_login(client, "bm", "MySecure1234!")

    response = client.post(
        "/api/conversations",
        json={"model": "gpt-5.6-sol", "reasoning_effort": "default"},
    )

    assert response.status_code == 403
