"""Shared bearer token between the Laravel app and the sidecar (TECHNICAL_DESIGN §3.1)."""

import hmac

from fastapi import Header, HTTPException, status

from app.config import settings


def require_token(authorization: str | None = Header(default=None)) -> None:
    expected = settings.analytics_token
    supplied = (authorization or "").removeprefix("Bearer ").strip()
    if not expected or not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or missing token")
