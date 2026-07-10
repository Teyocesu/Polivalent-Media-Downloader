import ipaddress
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse, urlunparse

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


YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
}
TIKTOK_HOSTS = {"tiktok.com", "www.tiktok.com", "vm.tiktok.com", "vt.tiktok.com"}
INSTAGRAM_HOSTS = {"instagram.com", "www.instagram.com"}
TWITTER_HOSTS = {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}


def validate_media_url(raw_url: str, settings: Settings) -> ValidatedUrl:
    original_url = raw_url.strip()
    url = original_url
    if not url:
        raise UserFacingError("Pegá un link para continuar.")
    if "\\" in url or any(ord(character) < 32 or ord(character) == 127 for character in url):
        raise UserFacingError("El link contiene caracteres no permitidos.")

    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError as exc:
        raise UserFacingError("El link no tiene un formato valido.") from exc
    if parsed.scheme != "https":
        raise UserFacingError("El link debe empezar con https://.")
    if parsed.username or parsed.password:
        raise UserFacingError("El link no puede incluir credenciales.")
    if not parsed.hostname:
        raise UserFacingError("No se pudo detectar el dominio del link.")
    if port not in {None, 443}:
        raise UserFacingError("El link usa un puerto que no esta permitido.")

    hostname = _canonical_hostname(parsed.hostname)
    _ensure_not_nonpublic_address(hostname)
    if hostname not in settings.allowed_domains:
        raise UserFacingError("Ese dominio no esta permitido para esta app.")
    _validate_platform_path(parsed.path, hostname)

    normalized_url = normalize_url(url)
    normalized_hostname = _canonical_hostname(urlparse(normalized_url).hostname or hostname)
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
    if hostname in YOUTUBE_HOSTS:
        return _normalize_youtube_url(parsed, hostname)
    if hostname in TIKTOK_HOSTS | INSTAGRAM_HOSTS | TWITTER_HOSTS:
        # Tracking and redirect-style query parameters are not required by the
        # supported extractors. Drop them before any network request so the
        # server only receives the canonical publication path.
        return urlunparse(("https", hostname, parsed.path or "/", "", "", ""))
    return url


def detect_site(hostname: str) -> str:
    if hostname in YOUTUBE_HOSTS:
        return "YouTube"
    if hostname in TIKTOK_HOSTS:
        return "TikTok"
    if hostname in INSTAGRAM_HOSTS:
        return "Instagram"
    if hostname in TWITTER_HOSTS:
        return "X / Twitter"
    return "Otro sitio compatible"


def sanitize_filename(name: str, fallback: str = "media") -> str:
    normalized = name.strip().replace("\x00", "")
    normalized = re.sub(r"[^\w.\- ]+", "", normalized, flags=re.ASCII)
    normalized = re.sub(r"\s+", "_", normalized).strip("._- ")
    if not normalized:
        normalized = fallback
    return normalized[:140]


def safe_remove_tree(path: Path) -> None:
    import shutil

    if path.is_symlink() or path.is_file():
        try:
            path.unlink(missing_ok=True)
        except OSError:
            return
    elif path.exists() and path.is_dir():
        shutil.rmtree(path, ignore_errors=True)


def _normalize_youtube_url(parsed, hostname: str) -> str:
    params = parse_qs(parsed.query, keep_blank_values=True)
    path = parsed.path.rstrip("/")

    if hostname == "youtu.be":
        video_id = _clean_youtube_id(parsed.path.lstrip("/").split("/")[0])
        if video_id:
            return f"https://www.youtube.com/watch?v={video_id}"
        raise UserFacingError("No se pudo detectar el video de YouTube.")

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

    if path == "/playlist" or "list" in params or "/playlist" in path.lower():
        raise UserFacingError("Esta URL es una playlist. Pegá el link de un video individual.")

    raise UserFacingError("Pegá el link de un video individual de YouTube.")


def _clean_youtube_id(value: str) -> str:
    match = re.fullmatch(r"[A-Za-z0-9_-]{11}", value.strip())
    return match.group(0) if match else ""


def _canonical_hostname(hostname: str) -> str:
    try:
        canonical = hostname.encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError as exc:
        raise UserFacingError("El dominio del link no es valido.") from exc
    if not canonical or len(canonical) > 253:
        raise UserFacingError("El dominio del link no es valido.")
    return canonical


def _ensure_not_nonpublic_address(hostname: str) -> None:
    address_text = hostname.split("%", 1)[0]
    try:
        address = ipaddress.ip_address(address_text)
    except ValueError:
        return
    if "%" in hostname or not address.is_global:
        raise UserFacingError("El link apunta a una direccion de red no permitida.")


def _validate_platform_path(path: str, hostname: str) -> None:
    normalized_path = path.rstrip("/") or "/"
    if hostname in YOUTUBE_HOSTS:
        return
    if hostname in TIKTOK_HOSTS:
        if hostname in {"vm.tiktok.com", "vt.tiktok.com"}:
            valid = re.fullmatch(r"/[A-Za-z0-9_-]+", normalized_path)
        else:
            valid = re.fullmatch(
                r"/(?:@[^/]+/video/[0-9]+|t/[A-Za-z0-9_-]+)",
                normalized_path,
            )
    elif hostname in INSTAGRAM_HOSTS:
        valid = re.fullmatch(
            r"/(?:reel|reels|p|tv)/[A-Za-z0-9_-]+|/share/(?:reel|p)/[A-Za-z0-9_-]+",
            normalized_path,
        )
    else:
        valid = re.fullmatch(
            r"/(?:[A-Za-z0-9_]+|i(?:/web)?)/status/[0-9]+(?:/(?:photo|video)/[0-9]+)?",
            normalized_path,
        )
    if not valid:
        raise UserFacingError("El link no corresponde a una publicacion de video compatible.")
