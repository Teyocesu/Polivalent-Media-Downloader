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


def validate_media_url(raw_url: str, settings: Settings) -> ValidatedUrl:
    url = raw_url.strip()
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

    _reject_playlist(parsed.path, parsed.query, hostname)
    return ValidatedUrl(url=url, site=detect_site(hostname), hostname=hostname)


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


def _reject_playlist(path: str, query: str, hostname: str) -> None:
    params = parse_qs(query, keep_blank_values=True)
    is_youtube = hostname in {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}
    if is_youtube and ("list" in params or path.rstrip("/") == "/playlist"):
        raise UserFacingError("Las playlists estan desactivadas en esta version.")
    if "/playlist" in path.lower():
        raise UserFacingError("Las playlists estan desactivadas en esta version.")
