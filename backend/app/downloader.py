import importlib.util
import logging
import re
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from yt_dlp import YoutubeDL
from yt_dlp.networking.impersonate import ImpersonateTarget
from yt_dlp.utils import DownloadError

from .config import Settings
from .utils import UserFacingError, safe_remove_tree, sanitize_filename
from .youtube_auth import (
    YoutubeCookieFileError,
    get_youtube_cookie_options,
    get_youtube_cookie_status,
)


logger = logging.getLogger(__name__)

QUALITY_OPTIONS = {"best", "1080", "720", "480", "mp3"}
PLATFORM_FAILURE_MESSAGE = (
    "No se pudo leer este video. Probablemente hay que actualizar yt-dlp y redeployar."
)
PRIVATE_OR_LOGIN_MESSAGE = (
    "Este video requiere acceso o login que yt-dlp no pudo autorizar. "
    "La app solo descarga contenido permitido y sin DRM."
)
NO_FORMATS_MESSAGE = "No se encontraron formatos descargables para este video."
NETWORK_MESSAGE = "Hubo un problema de red al conectar con la plataforma."
FFMPEG_MISSING_MESSAGE = "ffmpeg no esta disponible en el servidor. Revisa el Dockerfile o el deploy."
DRM_MESSAGE = "Este contenido parece protegido con DRM o no descargable por esta app."
UNAVAILABLE_MESSAGE = (
    "Este video no está disponible para la app. Puede haber sido eliminado, ser privado "
    "o estar restringido."
)
PLATFORM_IP_BLOCK_MESSAGE = (
    "La plataforma bloqueó temporalmente la IP del servidor. Probá más tarde o desde otro entorno."
)
YOUTUBE_NO_BOT_MESSAGE = (
    "YouTube pidió verificación de cuenta/no-bot. Configurá cookies de YouTube en Render "
    "para descargar este video."
)
YOUTUBE_COOKIES_MISSING_MESSAGE = (
    "Las cookies de YouTube están activadas, pero el archivo no existe o no se puede leer."
)
YOUTUBE_COOKIES_REJECTED_MESSAGE = "YouTube rechazó estas cookies. Exportá cookies nuevas y redeployá."
NETWORK_ERROR_TERMS = (
    "timed out",
    "timeout",
    "temporary failure",
    "connection reset",
    "connection aborted",
    "remote end closed connection",
    "network is unreachable",
    "name or service not known",
    "failed to resolve",
    "could not resolve host",
    "certificate verify failed",
    "http error 5",
)


class DownloadCancelled(Exception):
    pass


class DownloadTimedOut(UserFacingError):
    def __init__(self, message: str = "La operación tardó demasiado y fue cancelada."):
        super().__init__(message, status_code=504)


class DownloadTooLarge(UserFacingError):
    pass


ProgressCallback = Callable[[dict], None]
CancelCallback = Callable[[], bool]


class _SafeYtdlpLogger:
    """Route yt-dlp output through the application logger after redaction."""

    def debug(self, message: str) -> None:
        logger.debug("yt-dlp %s", _clean_error(message))

    def info(self, message: str) -> None:
        logger.info("yt-dlp %s", _clean_error(message))

    def warning(self, message: str) -> None:
        logger.warning("yt-dlp %s", _clean_error(message))

    def error(self, message: str) -> None:
        logger.error("yt-dlp %s", _clean_error(message))


SAFE_YTDLP_LOGGER = _SafeYtdlpLogger()


def fetch_metadata(url: str, settings: Settings) -> dict:
    deadline = time.monotonic() + settings.download_timeout_seconds

    def guard() -> None:
        if time.monotonic() >= deadline:
            raise DownloadTimedOut("La lectura del video tardó demasiado y fue cancelada.")

    info = None
    last_error: Exception | None = None
    for attempt in _attempt_profiles(url):
        guard()
        options = _metadata_options(settings, attempt, url)
        try:
            with YoutubeDL(options) as ydl:
                info = ydl.extract_info(url, download=False)
            guard()
            logger.info(
                "yt-dlp metadata ok attempt=%s host=%s",
                attempt["debug_code"],
                _safe_log_target(url),
            )
            break
        except DownloadError as exc:
            last_error = exc
            logger.info(
                "yt-dlp metadata failed attempt=%s error=%s",
                attempt["debug_code"],
                _clean_error(exc),
            )
            if _is_non_retryable_error(exc):
                break
        except DownloadTimedOut:
            raise
        except Exception as exc:
            last_error = exc
            logger.info(
                "yt-dlp metadata setup failed attempt=%s error=%s",
                attempt["debug_code"],
                _clean_error(exc),
            )
            break

    if info is None:
        raise _map_ytdlp_error(last_error, settings, url=url)

    _ensure_single_downloadable_item(info)
    _ensure_not_live(info)
    return {
        "title": info.get("title") or "Video sin titulo",
        "site": _site_from_info(info),
        "thumbnail": info.get("thumbnail"),
        "duration": info.get("duration"),
        "filesize": info.get("filesize") or info.get("filesize_approx"),
        "uploader": info.get("uploader") or info.get("channel") or info.get("creator"),
        "availableQualities": ["best", "1080", "720", "480", "mp3"],
    }


def download_media(
    url: str,
    quality: str,
    temp_dir: Path,
    settings: Settings,
    progress_callback: ProgressCallback,
    check_cancelled: CancelCallback,
) -> tuple[Path, str]:
    if quality not in QUALITY_OPTIONS:
        raise UserFacingError("Calidad no valida.")

    deadline = time.monotonic() + settings.download_timeout_seconds
    temp_dir.mkdir(parents=True, exist_ok=True)

    _emit(
        progress_callback,
        phase="selecting_format",
        phaseLabel="Seleccionando formato",
        progress=12,
        message="Seleccionando formato...",
        step=3,
        stepsTotal=6 if quality == "mp3" else 5,
    )

    def guard() -> None:
        if check_cancelled():
            raise DownloadCancelled()
        if time.monotonic() >= deadline:
            raise DownloadTimedOut()

    def progress_hook(data: dict) -> None:
        guard()
        status = data.get("status")
        if status == "downloading":
            total = data.get("total_bytes") or data.get("total_bytes_estimate")
            downloaded = data.get("downloaded_bytes") or 0
            if (total and total > settings.max_file_bytes) or downloaded > settings.max_file_bytes:
                raise DownloadTooLarge(
                    "El archivo supera el limite configurado.",
                    status_code=413,
                )
            download_percent = None
            progress = 18
            if total:
                download_percent = round(downloaded * 100 / total, 1)
                progress = max(18, min(92, int(18 + download_percent * 0.74)))
            phase = _download_phase(data, quality)
            _emit(
                progress_callback,
                status="downloading",
                phase=phase,
                phaseLabel=PHASE_LABELS[phase],
                progress=progress,
                downloadPercent=download_percent,
                speed=_format_speed(data.get("speed")),
                eta=_format_eta(data.get("eta")),
                downloadedBytes=downloaded or None,
                totalBytes=total,
                currentFile=_current_filename(data),
                message=f"{PHASE_LABELS[phase]}...",
                step=4,
                stepsTotal=6 if quality == "mp3" else 5,
            )
        elif status == "finished":
            _emit(
                progress_callback,
                status="processing",
                phase="postprocessing",
                phaseLabel="Procesando archivo",
                progress=93,
                message="Procesando archivo...",
                currentFile=_current_filename(data),
                step=5,
                stepsTotal=6 if quality == "mp3" else 5,
            )

    def postprocessor_hook(data: dict) -> None:
        guard()
        status = data.get("status")
        phase = _postprocessor_phase(data, quality)
        if status == "started":
            _emit(
                progress_callback,
                status="processing",
                phase=phase,
                phaseLabel=PHASE_LABELS[phase],
                progress=95,
                message=f"{PHASE_LABELS[phase]}...",
                step=5,
                stepsTotal=6 if quality == "mp3" else 5,
            )
        elif status == "finished":
            _emit(
                progress_callback,
                status="processing",
                phase="preparing_file",
                phaseLabel=PHASE_LABELS["preparing_file"],
                progress=98,
                message="Preparando archivo...",
                step=6 if quality == "mp3" else 5,
                stepsTotal=6 if quality == "mp3" else 5,
            )

    code = 1
    last_error: Exception | None = None
    for attempt in _attempt_profiles(url):
        guard()
        options = _download_options(
            quality,
            temp_dir,
            settings,
            progress_hook,
            postprocessor_hook,
            attempt,
            url,
        )
        guard()
        _emit(
            progress_callback,
            status="downloading",
            phase="selecting_format",
            phaseLabel="Seleccionando formato",
            progress=15,
            message="Seleccionando formato compatible...",
            step=3,
            stepsTotal=6 if quality == "mp3" else 5,
            debugCode=attempt["debug_code"],
        )
        try:
            with YoutubeDL(options) as ydl:
                code = ydl.download([url])
            guard()
            logger.info(
                "yt-dlp download ok attempt=%s host=%s",
                attempt["debug_code"],
                _safe_log_target(url),
            )
            break
        except DownloadCancelled:
            raise
        except DownloadTimedOut:
            raise
        except DownloadTooLarge:
            raise
        except DownloadError as exc:
            last_error = exc
            logger.info(
                "yt-dlp download failed attempt=%s error=%s",
                attempt["debug_code"],
                _clean_error(exc),
            )
            if _is_non_retryable_error(exc):
                break
            _clear_attempt_artifacts(temp_dir)
            code = 1
        except Exception as exc:
            last_error = exc
            logger.info(
                "yt-dlp download setup failed attempt=%s error=%s",
                attempt["debug_code"],
                _clean_error(exc),
            )
            code = 1
            break

    if code != 0:
        raise _map_ytdlp_error(last_error, settings, url=url)

    guard()
    output_file = _find_output_file(temp_dir)
    if output_file.stat().st_size > settings.max_file_bytes:
        output_file.unlink(missing_ok=True)
        raise DownloadTooLarge(
            "El archivo supera el limite configurado.",
            status_code=413,
        )

    final_file = _rename_for_delivery(output_file)
    _emit(
        progress_callback,
        status="processing",
        phase="done",
        phaseLabel=PHASE_LABELS["done"],
        progress=100,
        message="Listo para guardar.",
        step=6 if quality == "mp3" else 5,
        stepsTotal=6 if quality == "mp3" else 5,
    )
    return final_file, final_file.name


PHASE_LABELS = {
    "queued": "En cola",
    "validating_url": "Validando link",
    "normalizing_url": "Normalizando URL",
    "extracting_metadata": "Obteniendo metadata",
    "selecting_format": "Seleccionando formato",
    "downloading": "Descargando",
    "downloading_video": "Descargando video",
    "downloading_audio": "Descargando audio",
    "postprocessing": "Procesando archivo",
    "merging": "Uniendo audio/video con ffmpeg",
    "converting_audio": "Convirtiendo a MP3",
    "preparing_file": "Preparando archivo",
    "done": "Listo para guardar",
    "error": "Error",
    "expired": "Expirado",
}


def _metadata_options(settings: Settings, attempt: dict, url: str | None = None) -> dict:
    options = {
        "quiet": True,
        "no_warnings": True,
        "logger": SAFE_YTDLP_LOGGER,
        "skip_download": True,
        "noplaylist": True,
        "extract_flat": False,
        "socket_timeout": settings.ytdlp_socket_timeout_seconds,
        "retries": 1,
        "fragment_retries": 1,
        "extractor_retries": 1,
    }
    options.update(attempt["options"])
    if url and _is_youtube_url(url):
        _apply_youtube_runtime_options(options, settings)
    return options


def _download_options(
    quality: str,
    temp_dir: Path,
    settings: Settings,
    progress_hook: Callable[[dict], None],
    postprocessor_hook: Callable[[dict], None],
    attempt: dict | None = None,
    url: str | None = None,
) -> dict:
    options = {
        "quiet": True,
        "no_warnings": True,
        "logger": SAFE_YTDLP_LOGGER,
        "noplaylist": True,
        "restrictfilenames": True,
        "windowsfilenames": True,
        "outtmpl": str(temp_dir / "%(title).180B-%(id)s.%(ext)s"),
        "merge_output_format": "mp4",
        "format": _format_selector(quality),
        "retries": 3,
        "fragment_retries": 3,
        "extractor_retries": 2,
        "file_access_retries": 2,
        "socket_timeout": settings.ytdlp_socket_timeout_seconds,
        "continuedl": True,
        "overwrites": False,
        "match_filter": _max_size_match_filter(settings),
        "progress_hooks": [progress_hook],
        "postprocessor_hooks": [postprocessor_hook],
        "concurrent_fragment_downloads": 4,
    }
    if attempt:
        options.update(attempt["options"])
    if url and _is_youtube_url(url):
        _apply_youtube_runtime_options(options, settings)
    if quality == "mp3":
        options["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ]
    return options


def _apply_youtube_runtime_options(options: dict, settings: Settings) -> None:
    # yt-dlp enables Deno by default, but Node must be explicitly enabled. The
    # production image provides a supported Node runtime and the `default`
    # dependency extra provides the matching yt-dlp-ejs package.
    options["js_runtimes"] = {"node": {"path": None}}
    options.update(_youtube_cookie_options(settings))


def _youtube_cookie_options(settings: Settings) -> dict:
    status = get_youtube_cookie_status(settings)
    try:
        options = get_youtube_cookie_options(settings)
    except YoutubeCookieFileError as exc:
        raise UserFacingError(YOUTUBE_COOKIES_REJECTED_MESSAGE, status_code=403) from exc
    if status["enabled"] and not options:
        logger.warning(
            "youtube cookies enabled but unavailable mode=%s configured=%s readable=%s",
            status["mode"],
            status["configured"],
            status["readable"],
        )
    return options


def _format_selector(quality: str) -> str:
    if quality == "mp3":
        return "bestaudio[ext=m4a]/bestaudio/best"
    if quality == "best":
        return (
            "bestvideo[vcodec^=avc1]+bestaudio[ext=m4a]/"
            "bestvideo[ext=mp4]+bestaudio[ext=m4a]/"
            "bestvideo+bestaudio/best[ext=mp4]/best"
        )
    return (
        f"bestvideo[height<=?{quality}][vcodec^=avc1]+bestaudio[ext=m4a]/"
        f"bestvideo[height<=?{quality}][ext=mp4]+bestaudio[ext=m4a]/"
        f"bestvideo[height<=?{quality}]+bestaudio/"
        f"best[height<=?{quality}][ext=mp4]/best[height<=?{quality}]"
    )


def _ensure_single_downloadable_item(info: dict) -> None:
    if info.get("_type") == "playlist":
        raise UserFacingError("Las playlists estan desactivadas en esta version.")
    if info.get("entries") and not info.get("formats"):
        raise UserFacingError("Ese link parece contener mas de un item. Usa un video individual.")


def _ensure_not_live(info: dict) -> None:
    live_status = info.get("live_status")
    if info.get("is_live") or live_status in {"is_live", "is_upcoming", "post_live"}:
        raise UserFacingError("Los vivos o streams no estan soportados en esta version.")


def _ensure_size_allowed(info: dict, settings: Settings) -> None:
    selected = info.get("requested_downloads") or info.get("requested_formats") or []
    selected_sizes = [
        item.get("filesize") or item.get("filesize_approx")
        for item in selected
        if item.get("filesize") or item.get("filesize_approx")
    ]
    estimated = sum(selected_sizes) if selected_sizes else (
        info.get("filesize") or info.get("filesize_approx")
    )
    if estimated and estimated > settings.max_file_bytes:
        raise DownloadTooLarge(
            "El archivo supera el limite configurado.",
            status_code=413,
        )


def _max_size_match_filter(settings: Settings) -> Callable[..., str | None]:
    def check(info: dict, *, incomplete: bool = False) -> str | None:
        if not incomplete:
            _ensure_size_allowed(info, settings)
        return None

    return check


def _site_from_info(info: dict) -> str:
    extractor = (info.get("extractor_key") or info.get("extractor") or "").lower()
    if "youtube" in extractor:
        return "YouTube"
    if "tiktok" in extractor:
        return "TikTok"
    if "instagram" in extractor:
        return "Instagram"
    if "twitter" in extractor or extractor == "x":
        return "X / Twitter"
    return info.get("webpage_url_domain") or "Sitio detectado"


def _find_output_file(temp_dir: Path) -> Path:
    ignored_suffixes = {".part", ".ytdl", ".temp", ".tmp"}
    candidates = [
        path
        for path in temp_dir.iterdir()
        if path.is_file() and path.suffix.lower() not in ignored_suffixes
    ]
    if not candidates:
        raise UserFacingError("La descarga termino, pero no se encontro el archivo final.")
    return max(candidates, key=lambda path: path.stat().st_size)


def _clear_attempt_artifacts(temp_dir: Path) -> None:
    """Prevent a failed fallback from being mistaken for the final output."""
    for child in temp_dir.iterdir():
        safe_remove_tree(child)


def _rename_for_delivery(path: Path) -> Path:
    safe_stem = sanitize_filename(path.stem)
    suffix = path.suffix.lower() or ".mp4"
    target = path.with_name(f"{safe_stem}{suffix}")
    if target == path:
        return path
    counter = 2
    while target.exists():
        target = path.with_name(f"{safe_stem}_{counter}{suffix}")
        counter += 1
    path.replace(target)
    return target


def _clean_error(exc: Exception) -> str:
    message = str(exc).replace("\n", " ")
    message = re.sub(
        r"(\[[A-Za-z0-9:_ -]+\]\s+)[^:\s]+(?=:)",
        r"\1<media-id>",
        message,
    )
    message = re.sub(
        r"(?i)(['\"]?(?:authorization|proxy-authorization|cookie|set-cookie)['\"]?\s*:\s*)"
        r"(['\"])[^'\"]*\2",
        r"\1<credential redacted>",
        message,
    )
    message = re.sub(
        r"(?i)\b(?:authorization|proxy-authorization|cookie|set-cookie)\b\s*[:=]\s*.*$",
        "<credential redacted>",
        message,
    )
    message = re.sub(
        r"(?:https?|ftp|file)://[^\s]+",
        lambda match: f"<url host={_safe_log_target(match.group(0))}>",
        message,
        flags=re.IGNORECASE,
    )
    message = re.sub(r"(?i)(?:data|javascript):[^\s]+", "<url redacted>", message)
    message = re.sub(
        r"(?<![\w:])/(?:[^/\s\"']+/)*[^/\s\"']+",
        "<path>",
        message,
    )
    message = re.sub(r"[A-Za-z]:\\[^\s]+", "<path>", message)
    message = re.sub(r"(?i)cookies?file\s*[:=]\s*\S+", "<cookie path redacted>", message)
    message = re.sub(r"(?i)\S*cookies?\S*\.txt", "<cookie path redacted>", message)
    return message[:500]


def _safe_log_target(url: str) -> str:
    try:
        return (urlparse(url).hostname or "unknown").lower().rstrip(".")
    except (TypeError, ValueError):
        return "unknown"


def _attempt_profiles(url: str) -> list[dict]:
    profiles = [{"debug_code": "default", "options": {}}]
    if _is_youtube_url(url):
        profiles.extend(
            [
                {
                    "debug_code": "youtube_alt_clients_android_vr_web_safari",
                    "options": {
                        "extractor_args": {
                            "youtube": {"player_client": ["android_vr", "web_safari"]}
                        }
                    },
                },
            ]
        )
    if importlib.util.find_spec("curl_cffi"):
        profiles.append(
            {
                "debug_code": "impersonate_chrome",
                "options": {"impersonate": ImpersonateTarget.from_str("chrome")},
            }
        )
    return profiles


def _is_youtube_url(url: str) -> bool:
    hostname = (urlparse(url).hostname or "").lower()
    return hostname in {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtu.be",
    }


def _emit(progress_callback: ProgressCallback, **event) -> None:
    progress_callback(event)


def _download_phase(data: dict, quality: str) -> str:
    if quality == "mp3":
        return "downloading_audio"
    info = data.get("info_dict") or {}
    vcodec = info.get("vcodec")
    acodec = info.get("acodec")
    if vcodec and vcodec != "none" and acodec == "none":
        return "downloading_video"
    if acodec and acodec != "none" and vcodec == "none":
        return "downloading_audio"
    return "downloading"


def _postprocessor_phase(data: dict, quality: str) -> str:
    postprocessor = str(data.get("postprocessor") or data.get("info_dict", {}).get("__postprocessor") or "")
    if quality == "mp3" or re.search("extractaudio|ffmpegextractaudio", postprocessor, re.I):
        return "converting_audio"
    if re.search("merger|ffmpegmerger", postprocessor, re.I):
        return "merging"
    return "postprocessing"


def _format_speed(speed: float | int | None) -> str | None:
    if not speed:
        return None
    return f"{_format_bytes(float(speed))}/s"


def _format_eta(eta: float | int | None) -> str | None:
    if eta is None:
        return None
    try:
        seconds = max(0, int(eta))
    except (TypeError, ValueError):
        return None
    minutes, remaining = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{remaining:02d}"
    return f"{minutes:02d}:{remaining:02d}"


def _format_bytes(value: float | int | None) -> str | None:
    if value is None:
        return None
    size = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return None


def _current_filename(data: dict) -> str | None:
    filename = data.get("filename") or data.get("tmpfilename")
    return Path(filename).name if filename else None


def _map_ytdlp_error(
    exc: Exception | None,
    settings: Settings | None = None,
    *,
    url: str | None = None,
) -> UserFacingError:
    if exc is None:
        return UserFacingError(PLATFORM_FAILURE_MESSAGE)
    message = str(exc)
    lowered = message.lower()
    if "ffmpeg" in lowered and "not installed" in lowered:
        return UserFacingError(FFMPEG_MISSING_MESSAGE, status_code=500)
    if "drm" in lowered:
        return UserFacingError(DRM_MESSAGE, status_code=403)
    if any(
        term in lowered
        for term in (
            "ip address is blocked",
            "blocked your ip",
            "this ip has been blocked",
            "access denied for this ip",
        )
    ):
        return UserFacingError(PLATFORM_IP_BLOCK_MESSAGE, status_code=503)
    if any(
        term in lowered
        for term in (
            "video unavailable",
            "content unavailable",
            "post not found",
            "video has been removed",
            "this tweet is unavailable",
        )
    ):
        return UserFacingError(UNAVAILABLE_MESSAGE, status_code=404)
    is_youtube = bool(url and _is_youtube_url(url))
    if _is_youtube_cookie_rejection(lowered) and (is_youtube or url is None):
        if settings:
            status = get_youtube_cookie_status(settings)
            if status["enabled"] and not (status["configured"] and status["readable"]):
                return UserFacingError(YOUTUBE_COOKIES_MISSING_MESSAGE, status_code=500)
        return UserFacingError(YOUTUBE_COOKIES_REJECTED_MESSAGE, status_code=403)
    if is_youtube and (
        _is_youtube_no_bot_error(lowered)
        or any(term in lowered for term in ("http error 429", "too many requests"))
    ):
        if settings:
            status = get_youtube_cookie_status(settings)
            if status["enabled"] and not (status["configured"] and status["readable"]):
                return UserFacingError(YOUTUBE_COOKIES_MISSING_MESSAGE, status_code=500)
            if status["configured"] and status["readable"]:
                return UserFacingError(YOUTUBE_COOKIES_REJECTED_MESSAGE, status_code=403)
        return UserFacingError(YOUTUBE_NO_BOT_MESSAGE, status_code=403)
    if any(term in lowered for term in ("private", "login", "sign in", "cookies", "age-restricted")):
        return UserFacingError(PRIVATE_OR_LOGIN_MESSAGE, status_code=403)
    if any(
        term in lowered
        for term in (
            "requested format is not available",
            "no video formats",
            "no formats",
            "no downloadable",
        )
    ):
        return UserFacingError(NO_FORMATS_MESSAGE)
    if _is_network_error(lowered):
        return UserFacingError(NETWORK_MESSAGE, status_code=503)
    return UserFacingError(PLATFORM_FAILURE_MESSAGE)


def _is_youtube_no_bot_error(lowered: str) -> bool:
    return (
        "not a bot" in lowered
        or "confirm you are not a bot" in lowered
        or "use --cookies-from-browser" in lowered
        or "use --cookies" in lowered
    )


def _is_youtube_cookie_rejection(lowered: str) -> bool:
    return any(
        term in lowered
        for term in (
            "account cookies are no longer valid",
            "cookies are no longer valid",
            "cookies have expired",
            "expired cookies",
            "invalid cookies",
            "invalid netscape format cookies",
            "does not look like a netscape format cookies",
            "could not open cookie file",
            "unable to open cookie file",
            "failed to load cookies",
            "error loading cookies",
            "failed to parse cookies",
        )
    )


def _is_non_retryable_error(exc: Exception) -> bool:
    lowered = str(exc).lower()
    if _is_youtube_cookie_rejection(lowered):
        return True
    if _is_youtube_no_bot_error(lowered):
        return False
    return (
        ("ffmpeg" in lowered and "not installed" in lowered)
        or "private" in lowered
        or "sign in" in lowered
        or "cookies" in lowered
        or "drm" in lowered
        or "unsupported url" in lowered
        or "not available in your country" in lowered
        or "video unavailable" in lowered
        or _is_network_error(lowered)
    )


def _is_network_error(lowered: str) -> bool:
    return any(term in lowered for term in NETWORK_ERROR_TERMS)
