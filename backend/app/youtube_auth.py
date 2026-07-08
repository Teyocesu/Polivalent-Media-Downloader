from pathlib import Path

from .config import Settings


ALLOWED_BROWSERS = {"chrome", "firefox", "safari", "edge", "brave"}


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
        return {"cookiefile": str(settings.youtube_cookies_path)}
    if status["mode"] == "browser" and status["configured"] and status["readable"]:
        return {"cookiesfrombrowser": (settings.youtube_cookies_from_browser,)}
    return {}


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
