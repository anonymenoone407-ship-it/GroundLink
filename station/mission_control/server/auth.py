"""Bearer and short-lived browser-session authentication."""

import hmac
import secrets
import time

from fastapi import Header, HTTPException, Request, WebSocket

from .config import Settings


def token_matches(candidate: str | None, expected: str) -> bool:
    return candidate is not None and hmac.compare_digest(candidate, expected)


class BrowserSessions:
    """Process-local browser sessions; bearer secrets are never stored."""

    cookie_name = "gl_mc_session"

    def __init__(self, ttl_seconds: int = 3600):
        self.ttl_seconds = ttl_seconds
        self._expires: dict[str, float] = {}

    def create(self) -> str:
        self.prune()
        session_id = secrets.token_urlsafe(32)
        self._expires[session_id] = time.monotonic() + self.ttl_seconds
        return session_id

    def valid(self, session_id: str | None) -> bool:
        if not session_id:
            return False
        expiry = self._expires.get(session_id)
        if expiry is None or expiry <= time.monotonic():
            self._expires.pop(session_id, None)
            return False
        return True

    def revoke(self, session_id: str | None) -> None:
        if session_id:
            self._expires.pop(session_id, None)

    def prune(self) -> None:
        now = time.monotonic()
        for session_id, expiry in tuple(self._expires.items()):
            if expiry <= now:
                self._expires.pop(session_id, None)


def _bearer(authorization: str | None) -> str | None:
    prefix = "Bearer "
    return authorization[len(prefix):] if authorization and authorization.startswith(prefix) else None


def require_control_token(settings: Settings, sessions: BrowserSessions | None = None):
    def dependency(request: Request, authorization: str | None = Header(default=None)) -> None:
        if token_matches(_bearer(authorization), settings.control_token):
            return
        session_id = request.cookies.get(BrowserSessions.cookie_name)
        origin = request.headers.get("origin")
        if sessions is not None and origin in settings.allowed_origins and sessions.valid(session_id):
            return
        raise HTTPException(status_code=401, detail="valid bearer token or browser session required")

    return dependency


def websocket_authorized(
    websocket: WebSocket, settings: Settings, sessions: BrowserSessions | None = None
) -> bool:
    origin = websocket.headers.get("origin")
    token = websocket.query_params.get("token")
    session_id = websocket.cookies.get(BrowserSessions.cookie_name)
    credential_valid = token_matches(token, settings.control_token) or (
        sessions is not None and sessions.valid(session_id)
    )
    return origin in settings.allowed_origins and credential_valid
