import base64
import binascii
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

from .config import Settings


SESSION_TOKEN_PURPOSE = "session"
FILE_TICKET_PURPOSE = "file-download"


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.b64decode(data + padding, altchars=b"-_", validate=True)


def verify_password(password: str, settings: Settings) -> bool:
    return hmac.compare_digest(password, settings.app_password)


def create_token(settings: Settings) -> str:
    payload: dict[str, Any] = {
        "purpose": SESSION_TOKEN_PURPOSE,
        "exp": int(time.time()) + settings.session_ttl_seconds,
        "nonce": secrets.token_urlsafe(18),
    }
    return _create_signed_token(payload, settings)


def create_file_ticket(job_id: str, settings: Settings, ttl_seconds: int = 60) -> str:
    payload: dict[str, Any] = {
        "purpose": FILE_TICKET_PURPOSE,
        "job_id": job_id,
        "exp": int(time.time()) + ttl_seconds,
        "nonce": secrets.token_urlsafe(18),
    }
    return _create_signed_token(payload, settings)


def _create_signed_token(payload: dict[str, Any], settings: Settings) -> str:
    body = _b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature = _sign(body, settings)
    return f"{body}.{signature}"


def verify_token(token: str, settings: Settings) -> bool:
    payload = _verify_signed_payload(token, settings)
    return bool(payload and payload.get("purpose") == SESSION_TOKEN_PURPOSE)


def verify_file_ticket(ticket: str, job_id: str, settings: Settings) -> bool:
    payload = _verify_signed_payload(ticket, settings)
    if not payload or payload.get("purpose") != FILE_TICKET_PURPOSE:
        return False
    ticket_job_id = payload.get("job_id")
    if not isinstance(ticket_job_id, str):
        return False
    return hmac.compare_digest(ticket_job_id.encode("utf-8"), job_id.encode("utf-8"))


def _verify_signed_payload(token: str, settings: Settings) -> dict[str, Any] | None:
    if not token or len(token) > 4096 or token.count(".") != 1:
        return None
    try:
        body, signature = token.split(".", 1)
        expected = _sign(body, settings)
        if not hmac.compare_digest(signature, expected):
            return None
        payload = json.loads(_b64decode(body))
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("nonce"), str)
            or not payload["nonce"]
        ):
            return None
        expires_at = int(payload.get("exp", 0))
    except (
        AttributeError,
        binascii.Error,
        UnicodeDecodeError,
        ValueError,
        json.JSONDecodeError,
        OverflowError,
        TypeError,
    ):
        return None
    return payload if expires_at > int(time.time()) else None


def _sign(body: str, settings: Settings) -> str:
    digest = hmac.new(
        settings.token_secret.encode("utf-8"),
        body.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return _b64encode(digest)
