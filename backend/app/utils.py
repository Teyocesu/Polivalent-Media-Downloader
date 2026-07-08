import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .config import Settings


class UserFacingError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class ValidatedUrl:
    url: str
    site: str
    hostname: str
    original_url: str
    was_normalized: bool = False


def validate_media_url(raw_url: str, settings: Settings) -> ValidatedUrl:
    original_url = raw_url.strip()
    url = original_url
    if not url:
        raise UserFacingError("Pegá un link para continuar.")

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise UserFacingError("El link debe empezar con http:// o https://.")
    if parsed.username or parsed.password:
        raise UserFacingError("El link no puede incluir credenciales.")
    if not parsed.hostname:
        raise UserFacingError("No se pudo detectar el dominio del link.")

    hostname = parsed.hostname.lower().rstrip(".")
    if hostname not in settings.allowed_domains:
        raise UserFacingError("Ese dominio no esta permitido para esta app.")

    normalized_url = normalize_url(url)
    normalized_hostname = urlparse(normalized_url).hostname or hostname
    return ValidatedUrl(
        url=normalized_url,
        site=detect_site(normalized_hostname),
        hostname=normalized_hostname,
        original_url=original_url,
        was_normalized=normalized_url != original_url,
    )


def normalize_url(raw_url: str) -> str:
    url = raw_url.strip()
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if hostname in {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}:
        return _normalize_youtube_url(parsed, hostname)
    return url


def detect_site(hostname: str) -> str:
    if hostname in {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}:
        return "YouTube"
    if hostname in {"tiktok.com", "www.tiktok.com", "vm.tiktok.com", "vt.tiktok.com"}:
        return "TikTok"
    if hostname in {"instagram.com", "www.instagram.com"}:
        return "Instagram"
    return "X / Twitter"


def sanitize_filename(name: str, fallback: str = "media") -> str:
    normalized = name.strip().replace("\x00", "")
    normalized = re.sub(r"[^\w.\- ]+", "", normalized, flags=re.ASCII)
    normalized = re.sub(r"\s+", "_", normalized).strip("._- ")
    if not normalized:
        normalized = fallback
    return normalized[:140]


def safe_remove_tree(path: Path) -> None:
    import shutil

    if path.exists() and path.is_dir():
        shutil.rmtree(path, ignore_errors=True)


def _normalize_youtube_url(parsed, hostname: str) -> str:
    params = parse_qs(parsed.query, keep_blank_values=True)
    path = parsed.path.rstrip("/")

    if hostname == "youtu.be":
        video_id = _clean_youtube_id(parsed.path.lstrip("/").split("/")[0])
        if video_id:
            return f"https://www.youtube.com/watch?v={video_id}"
        raise UserFacingError("No se pudo detectar el video de YouTube.")

    if path == "/playlist" or (not params.get("v") and "list" in params):
        raise UserFacingError("Esta URL es una playlist. Pegá el link de un video individual.")

    if path == "/watch":
        video_id = _clean_youtube_id((params.get("v") or [""])[0])
        if video_id:
            return f"https://www.youtube.com/watch?v={video_id}"
        raise UserFacingError("No se pudo detectar el video de YouTube.")

    if path.startswith("/shorts/"):
        video_id = _clean_youtube_id(path.split("/", 3)[2] if len(path.split("/")) > 2 else "")
        if video_id:
            return f"https://www.youtube.com/watch?v={video_id}"
        raise UserFacingError("No se pudo detectar el short de YouTube.")

    if "/playlist" in path.lower():
        raise UserFacingError("Esta URL es una playlist. Pegá el link de un video individual.")

    return parsed.geturl()


def _clean_youtube_id(value: str) -> str:
    match = re.match(r"^[A-Za-z0-9_-]{6,}$", value.strip())
    return match.group(0) if match else ""
