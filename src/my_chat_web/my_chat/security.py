from __future__ import annotations

import secrets

from fastapi import HTTPException, Request, status

from .database import Database, User


def csrf_token(request: Request) -> str:
    token = request.session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf_token"] = token
    return token


def validate_csrf(request: Request, supplied: str | None) -> None:
    expected = request.session.get("csrf_token")
    if not expected or not supplied or not secrets.compare_digest(expected, supplied):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="CSRF token validation failed.",
        )


def session_user(request: Request) -> User | None:
    username = request.session.get("username")
    session_version = request.session.get("session_version")
    if not isinstance(username, str) or not isinstance(session_version, int):
        return None
    database: Database = request.app.state.database
    user = database.get_user(username)
    if user is None or user.session_version != session_version:
        request.session.clear()
        return None
    return user


def require_api_user(request: Request, allow_password_change: bool = False) -> User:
    user = session_user(request)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required.",
        )
    if user.must_change_password and not allow_password_change:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "password_change_required"},
        )
    return user


def password_validation_error(password: str, username: str) -> str | None:
    if len(password) < 10:
        return "비밀번호는 10자 이상이어야 합니다."
    if username.lower() in password.lower():
        return "비밀번호에 사용자 이름을 포함할 수 없습니다."
    if not any(character.isalpha() for character in password):
        return "비밀번호에는 문자가 하나 이상 필요합니다."
    if not any(character.isdigit() for character in password):
        return "비밀번호에는 숫자가 하나 이상 필요합니다."
    return None
