import logging
import time
from pathlib import Path
from typing import Callable

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from .config import Settings
from .utils import UserFacingError, sanitize_filename


logger = logging.getLogger(__name__)

QUALITY_OPTIONS = {"best", "1080", "720", "480", "mp3"}
PLATFORM_FAILURE_MESSAGE = (
    "No se pudo descargar este video. Probablemente la plataforma cambio algo o "
    "el contenido requiere acceso especial. Proba actualizar yt-dlp."
)


class DownloadCancelled(Exception):
    pass


class DownloadTimedOut(Exception):
    pass


class DownloadTooLarge(UserFacingError):
    pass


ProgressCallback = Callable[[int, str, str | None], None]
CancelCallback = Callable[[], bool]


def fetch_metadata(url: str, settings: Settings) -> dict:
    options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "extract_flat": False,
        "socket_timeout": 20,
    }

    try:
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
    except DownloadError as exc:
        logger.info("yt-dlp metadata failed: %s", _clean_error(exc))
        raise UserFacingError(PLATFORM_FAILURE_MESSAGE) from exc

    _ensure_single_downloadable_item(info)
    _ensure_not_live(info)
    _ensure_size_allowed(info, settings)

    return {
        "title": info.get("title") or "Video sin titulo",
        "site": _site_from_info(info),
        "thumbnail": info.get("thumbnail"),
        "duration": info.get("duration"),
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

    started_at = time.monotonic()
    temp_dir.mkdir(parents=True, exist_ok=True)

    def guard() -> None:
        if check_cancelled():
            raise DownloadCancelled()
        if time.monotonic() - started_at > settings.download_timeout_seconds:
            raise DownloadTimedOut()

    def progress_hook(data: dict) -> None:
        guard()
        status = data.get("status")
        if status == "downloading":
            total = data.get("total_bytes") or data.get("total_bytes_estimate")
            downloaded = data.get("downloaded_bytes") or 0
            percent = 5
            if total:
                percent = max(5, min(94, int(downloaded * 90 / total)))
            progress_callback(percent, "Descargando...", "downloading")
        elif status == "finished":
            progress_callback(95, "Procesando archivo...", "processing")

    def postprocessor_hook(data: dict) -> None:
        guard()
        status = data.get("status")
        if status == "started":
            progress_callback(96, "Procesando con ffmpeg...", "processing")
        elif status == "finished":
            progress_callback(99, "Preparando entrega...", "processing")

    options = _download_options(quality, temp_dir, settings, progress_hook, postprocessor_hook)
    progress_callback(1, "Preparando descarga...", "downloading")

    try:
        with YoutubeDL(options) as ydl:
            code = ydl.download([url])
    except DownloadCancelled:
        raise
    except DownloadTimedOut:
        raise
    except DownloadError as exc:
        logger.info("yt-dlp download failed: %s", _clean_error(exc))
        raise UserFacingError(PLATFORM_FAILURE_MESSAGE) from exc

    if code != 0:
        raise UserFacingError(PLATFORM_FAILURE_MESSAGE)

    output_file = _find_output_file(temp_dir)
    if output_file.stat().st_size > settings.max_file_bytes:
        output_file.unlink(missing_ok=True)
        raise DownloadTooLarge(
            f"El archivo supera el limite configurado de {settings.max_file_mb} MB.",
            status_code=413,
        )

    final_file = _rename_for_delivery(output_file)
    progress_callback(100, "Archivo listo.", "processing")
    return final_file, final_file.name


def _download_options(
    quality: str,
    temp_dir: Path,
    settings: Settings,
    progress_hook: Callable[[dict], None],
    postprocessor_hook: Callable[[dict], None],
) -> dict:
    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "restrictfilenames": True,
        "windowsfilenames": True,
        "outtmpl": str(temp_dir / "%(title).180B-%(id)s.%(ext)s"),
        "merge_output_format": "mp4",
        "format": _format_selector(quality),
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 20,
        "continuedl": True,
        "nooverwrites": True,
        "max_filesize": settings.max_file_bytes,
        "progress_hooks": [progress_hook],
        "postprocessor_hooks": [postprocessor_hook],
        "concurrent_fragment_downloads": 2,
    }
    if quality == "mp3":
        options["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ]
    return options


def _format_selector(quality: str) -> str:
    if quality == "mp3":
        return "bestaudio/best"
    if quality == "best":
        return "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best[ext=mp4]/best"
    return (
        f"bestvideo[height<={quality}][ext=mp4]+bestaudio[ext=m4a]/"
        f"bestvideo[height<={quality}]+bestaudio/"
        f"best[height<={quality}][ext=mp4]/best[height<={quality}]"
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
    estimated = info.get("filesize") or info.get("filesize_approx")
    if not estimated:
        estimates = [
            fmt.get("filesize") or fmt.get("filesize_approx")
            for fmt in info.get("formats", [])
            if fmt.get("filesize") or fmt.get("filesize_approx")
        ]
        estimated = max(estimates) if estimates else None
    if estimated and estimated > settings.max_file_bytes:
        raise DownloadTooLarge(
            f"El archivo estimado supera el limite configurado de {settings.max_file_mb} MB.",
            status_code=413,
        )


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
    return str(exc).replace("\n", " ")[:500]
