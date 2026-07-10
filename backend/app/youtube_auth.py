import io
import re
from pathlib import Path

from .config import Settings


ALLOWED_BROWSERS = {"chrome", "firefox", "safari", "edge", "brave"}
# Match the effective per-file limit observed in Render builds. Keeping the
# runtime guard aligned also prevents unexpectedly large cookie exports from
# being copied into memory in local or non-Render deployments.
MAX_COOKIE_FILE_BYTES = 500 * 1024


class YoutubeCookieFileError(Exception):
    """Raised without cookie contents when a configured file is malformed."""


class _WritableCookieBuffer(io.StringIO):
    def truncate(self, size: int | None = None) -> int:
        result = super().truncate(size)
        if size == 0:
            self.seek(0)
        return result


def validate_cookie_file(path: Path | None) -> tuple[bool, bool]:
    configured = path is not None
    readable = bool(configured and path.is_file() and path.exists())
    if not readable:
        return configured, False
    try:
        with path.open("rb"):
            return configured, True
    except OSError:
        return configured, False


def get_youtube_cookie_options(settings: Settings) -> dict:
    status = get_youtube_cookie_status(settings)
    if not status["enabled"]:
        return {}
    if status["mode"] == "file" and status["configured"] and status["readable"]:
        try:
            cookie_buffer = _load_validated_cookie_buffer(settings.youtube_cookies_path)
        except OSError:
            return {}
        return {"cookiefile": cookie_buffer}
    if status["mode"] == "browser" and status["configured"] and status["readable"]:
        return {"cookiesfrombrowser": (settings.youtube_cookies_from_browser,)}
    return {}


def _load_validated_cookie_buffer(path: Path | None) -> _WritableCookieBuffer:
    if path is None:
        raise OSError("cookie file unavailable")
    with path.open("rb") as cookie_file:
        raw = cookie_file.read(MAX_COOKIE_FILE_BYTES + 1)
    if len(raw) > MAX_COOKIE_FILE_BYTES:
        raise YoutubeCookieFileError("cookie file too large")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise YoutubeCookieFileError("cookie file encoding is invalid") from exc
    if "\x00" in text:
        raise YoutubeCookieFileError("cookie file contains invalid characters")

    header_found = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped in {"# Netscape HTTP Cookie File", "# HTTP Cookie File"}:
            header_found = True
            continue
        candidate = line[len("#HttpOnly_") :] if line.startswith("#HttpOnly_") else line
        if not candidate.strip() or candidate.startswith("#"):
            continue
        fields = candidate.split("\t")
        valid = (
            len(fields) == 7
            and bool(fields[0])
            and fields[1] in {"TRUE", "FALSE"}
            and fields[2].startswith("/")
            and fields[3] in {"TRUE", "FALSE"}
            and (not fields[4] or re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", fields[4]))
        )
        if not valid:
            raise YoutubeCookieFileError("cookie file is not valid Netscape format")
    if not header_found:
        raise YoutubeCookieFileError("cookie file is not valid Netscape format")

    return _WritableCookieBuffer(text if text.endswith("\n") else f"{text}\n")


def get_youtube_cookie_status(settings: Settings) -> dict:
    if not settings.youtube_cookies_enabled:
        return {
            "enabled": False,
            "configured": False,
            "readable": False,
            "mode": "none",
        }

    file_configured, file_readable = validate_cookie_file(settings.youtube_cookies_path)
    if file_configured:
        return {
            "enabled": True,
            "configured": True,
            "readable": file_readable,
            "mode": "file",
        }

    browser = settings.youtube_cookies_from_browser
    if browser and browser in ALLOWED_BROWSERS and not settings.is_production:
        return {
            "enabled": True,
            "configured": True,
            "readable": True,
            "mode": "browser",
        }

    return {
        "enabled": True,
        "configured": False,
        "readable": False,
        "mode": "none",
    }
