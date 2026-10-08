"""本机最小登录与角色会话。

账号由环境变量提供，适合当前单机试用：
``SALES_USERNAME``/``SALES_PASSWORD`` 和
``REVIEWER_USERNAME``/``REVIEWER_PASSWORD``。会话保存在当前 API 进程内，
服务重启后需要重新登录；正式多人部署时应替换为持久化用户表和集中式会话。
"""

from __future__ import annotations

from dataclasses import dataclass
import hmac
import os
import secrets
from threading import Lock
from typing import Literal

from fastapi import HTTPException, Request, status


Role = Literal["sales", "reviewer"]
SESSION_COOKIE = "dianping_session"


@dataclass(frozen=True, slots=True)
class AuthUser:
    username: str
    role: Role

    def to_dict(self) -> dict[str, str]:
        return {"username": self.username, "role": self.role}


_sessions: dict[str, AuthUser] = {}
_remembered_users: dict[str, AuthUser] = {}
_sessions_lock = Lock()


def _configured_accounts() -> tuple[tuple[str, str, Role], ...]:
    return (
        (
            os.getenv("SALES_USERNAME", "sales").strip() or "sales",
            os.getenv("SALES_PASSWORD", "sales123"),
            "sales",
        ),
        (
            os.getenv("REVIEWER_USERNAME", "reviewer").strip() or "reviewer",
            os.getenv("REVIEWER_PASSWORD", "reviewer123"),
            "reviewer",
        ),
    )


def authenticate(username: str, password: str) -> AuthUser | None:
    """按配置账号登录；密码比较使用常量时间比较。"""

    for configured_username, configured_password, role in _configured_accounts():
        if hmac.compare_digest(username, configured_username) and hmac.compare_digest(
            password, configured_password
        ):
            user = AuthUser(username=configured_username, role=role)
            with _sessions_lock:
                _remembered_users[user.username] = user
            return user
    return None


def switch_user(request: Request, username: str) -> AuthUser | None:
    """在当前已登录会话中切换到本进程此前成功登录过的账号。

    不保存密码；服务重启后需要至少重新登录一次目标账号。
    """

    if get_user(request) is None:
        return None
    with _sessions_lock:
        return _remembered_users.get(username.strip())


def create_session(user: AuthUser) -> str:
    token = secrets.token_urlsafe(32)
    with _sessions_lock:
        _sessions[token] = user
    return token


def get_user(request: Request) -> AuthUser | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    with _sessions_lock:
        return _sessions.get(token)


def clear_session(request: Request) -> None:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        with _sessions_lock:
            _sessions.pop(token, None)


def require_user(request: Request) -> AuthUser:
    user = get_user(request)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录。")
    return user


def require_role(request: Request, role: Role) -> AuthUser:
    user = require_user(request)
    if user.role != role:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="当前账号没有执行此操作的权限。")
    return user
