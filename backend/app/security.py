import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

from .config import Settings


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def verify_password(password: str, settings: Settings) -> bool:
    return hmac.compare_digest(password, settings.app_password)


def create_token(settings: Settings) -> str:
    payload: dict[str, Any] = {
        "exp": int(time.time()) + settings.session_ttl_seconds,
        "nonce": secrets.token_urlsafe(18),
    }
    body = _b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature = _sign(body, settings)
    return f"{body}.{signature}"


def verify_token(token: str, settings: Settings) -> bool:
    try:
        body, signature = token.split(".", 1)
        expected = _sign(body, settings)
        if not hmac.compare_digest(signature, expected):
            return False
        payload = json.loads(_b64decode(body))
        expires_at = int(payload.get("exp", 0))
    except (ValueError, json.JSONDecodeError, TypeError):
        return False
    return expires_at > int(time.time())


def _sign(body: str, settings: Settings) -> str:
    digest = hmac.new(
        settings.token_secret.encode("utf-8"),
        body.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return _b64encode(digest)
