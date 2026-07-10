import asyncio
import logging
import os
import re
import shutil
import subprocess
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask
from yt_dlp.version import __version__ as ytdlp_version

from .cleanup import cleanup_loop
from .config import get_settings
from .downloader import DownloadTooLarge, fetch_metadata
from .jobs import JobGone, JobManager, JobNotReady
from .rate_limit import FixedWindowRateLimiter
from .security import (
    create_file_ticket,
    create_token,
    verify_file_ticket,
    verify_password,
    verify_token,
)
from .utils import UserFacingError, validate_media_url
from .youtube_auth import get_youtube_cookie_status


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)

settings = get_settings()
job_manager = JobManager(settings)
login_rate_limiter = FixedWindowRateLimiter(max_failures=8, window_seconds=300)
bearer = HTTPBearer(auto_error=False)
FILE_TICKET_COOKIE = "pmd_file_ticket"
FILE_TICKET_TTL_SECONDS = 60


class LoginRequest(BaseModel):
    password: str = Field(min_length=1, max_length=512)


class LoginResponse(BaseModel):
    ok: bool
    token: str


class UrlRequest(BaseModel):
    url: str = Field(min_length=1, max_length=4096)


class DownloadRequest(BaseModel):
    url: str = Field(min_length=1, max_length=4096)
    quality: Literal["best", "1080", "720", "480", "mp3"]


def require_auth(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> None:
    if not credentials or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=401,
            detail="Sesion requerida.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not verify_token(credentials.credentials, settings):
        raise HTTPException(
            status_code=401,
            detail="Sesion expirada o invalida.",
            headers={"WWW-Authenticate": "Bearer"},
        )


def require_debug_auth(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> None:
    require_auth(credentials)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await asyncio.to_thread(job_manager.cleanup_expired)
    cleanup_task = asyncio.create_task(cleanup_loop(job_manager))
    logger.info("Media downloader started system=%s", _system_debug_payload())
    try:
        yield
    finally:
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass
        job_manager.shutdown()


app = FastAPI(title="Private Media Downloader", lifespan=lifespan)

if settings.allowed_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
    )


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; base-uri 'self'; connect-src 'self'; "
        "form-action 'self'; frame-ancestors 'none'; img-src 'self' https: data:; "
        "object-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'",
    )
    response.headers.setdefault(
        "Permissions-Policy",
        "camera=(), geolocation=(), microphone=(), payment=(), usb=()",
    )
    if settings.is_production:
        response.headers.setdefault(
            "Strict-Transport-Security",
            "max-age=31536000; includeSubDomains",
        )
    if request.url.path == "/api" or request.url.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault("Pragma", "no-cache")
        response.headers.setdefault("Expires", "0")
    return response


@app.exception_handler(UserFacingError)
async def handle_user_error(_request: Request, exc: UserFacingError):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})


@app.exception_handler(RequestValidationError)
async def handle_validation_error(_request: Request, _exc: RequestValidationError):
    return JSONResponse(status_code=422, content={"detail": "La solicitud no es valida."})


@app.exception_handler(Exception)
async def handle_unexpected_error(_request: Request, exc: Exception):
    logger.error("Unhandled request failure error_type=%s", type(exc).__name__)
    return JSONResponse(status_code=500, content={"detail": "Ocurrio un error interno."})


@app.get("/health")
async def health():
    return {"ok": True, "status": "healthy"}


@app.post("/api/auth/login", response_model=LoginResponse)
async def login(body: LoginRequest, request: Request):
    limiter_key = _client_key(request)
    if login_rate_limiter.is_limited(limiter_key):
        retry_after = login_rate_limiter.retry_after(limiter_key)
        raise HTTPException(
            status_code=429,
            detail="Demasiados intentos. Espera unos minutos y proba de nuevo.",
            headers={"Retry-After": str(retry_after)},
        )
    if not verify_password(body.password, settings):
        login_rate_limiter.record_failure(limiter_key)
        raise HTTPException(status_code=401, detail="Contrasena incorrecta.")
    login_rate_limiter.reset(limiter_key)
    return {"ok": True, "token": create_token(settings)}


@app.post("/api/info", dependencies=[Depends(require_auth)])
async def info(body: UrlRequest):
    validated = validate_media_url(body.url, settings)
    try:
        metadata = await asyncio.to_thread(fetch_metadata, validated.url, settings)
    except DownloadTooLarge:
        raise
    metadata["site"] = metadata.get("site") or validated.site
    metadata["normalizedUrl"] = validated.url
    metadata["wasNormalized"] = validated.was_normalized
    return metadata


@app.post("/api/download", dependencies=[Depends(require_auth)])
async def download(body: DownloadRequest):
    validated = validate_media_url(body.url, settings)
    job = job_manager.create_job(
        validated.url,
        body.quality,
        validated.site,
        original_url=validated.original_url,
    )
    return {"jobId": job.job_id, "status": "queued"}


@app.get("/api/jobs/{job_id}", dependencies=[Depends(require_auth)])
async def job_status(job_id: str):
    snapshot = job_manager.get_snapshot(job_id)
    if not snapshot:
        raise HTTPException(status_code=404, detail="Job no encontrado.")
    return snapshot


@app.post("/api/files/{job_id}/authorize", dependencies=[Depends(require_auth)])
async def authorize_file_download(job_id: str):
    try:
        job_manager.ensure_file_ready(job_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Archivo no encontrado.") from exc
    except JobGone as exc:
        raise HTTPException(status_code=410, detail="Archivo expirado. Genera una nueva descarga.") from exc
    except JobNotReady as exc:
        raise HTTPException(status_code=409, detail="La descarga todavia no esta lista.") from exc

    download_path = _file_download_path(job_id)
    response = JSONResponse(content={"ok": True, "downloadUrl": download_path})
    response.set_cookie(
        key=FILE_TICKET_COOKIE,
        value=create_file_ticket(job_id, settings, FILE_TICKET_TTL_SECONDS),
        max_age=FILE_TICKET_TTL_SECONDS,
        httponly=True,
        secure=settings.is_production,
        samesite="strict",
        path=download_path,
    )
    return response


@app.get("/api/files/{job_id}")
async def file_download(
    job_id: str,
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
):
    ticket = request.cookies.get(FILE_TICKET_COOKIE, "")
    bearer_is_valid = bool(
        credentials
        and credentials.scheme.lower() == "bearer"
        and verify_token(credentials.credentials, settings)
    )
    if not bearer_is_valid and not verify_file_ticket(ticket, job_id, settings):
        raise HTTPException(
            status_code=401,
            detail="Autorizacion de descarga requerida.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if request.headers.get("range"):
        raise HTTPException(
            status_code=416,
            detail="La descarga es de un solo uso y no admite rangos parciales.",
            headers={"Accept-Ranges": "none"},
        )
    try:
        path, filename = job_manager.claim_file(job_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Archivo no encontrado.") from exc
    except JobGone as exc:
        raise HTTPException(status_code=410, detail="Archivo expirado. Genera una nueva descarga.") from exc
    except JobNotReady as exc:
        raise HTTPException(status_code=409, detail="La descarga todavia no esta lista.") from exc

    response = FileResponse(
        path,
        media_type="application/octet-stream",
        filename=filename,
        headers={"Accept-Ranges": "none"},
        background=BackgroundTask(job_manager.finish_delivery, job_id),
    )
    response.delete_cookie(
        key=FILE_TICKET_COOKIE,
        path=_file_download_path(job_id),
        secure=settings.is_production,
        httponly=True,
        samesite="strict",
    )
    return response


@app.delete("/api/jobs/{job_id}", dependencies=[Depends(require_auth)])
async def cancel_job(job_id: str):
    if not job_manager.cancel_job(job_id):
        raise HTTPException(status_code=404, detail="Job no encontrado.")
    return {"ok": True, "status": "expired"}


@app.get("/api/debug/system", dependencies=[Depends(require_debug_auth)])
async def debug_system():
    return _system_debug_payload()


STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@app.get("/api", include_in_schema=False)
@app.get("/api/{full_path:path}", include_in_schema=False)
async def unknown_api_route(full_path: str = ""):
    raise HTTPException(status_code=404, detail="Endpoint de API no encontrado.")


@app.get("/{full_path:path}", include_in_schema=False)
async def serve_spa(full_path: str):
    requested = (STATIC_DIR / full_path).resolve()
    if STATIC_DIR.exists() and requested.is_file() and STATIC_DIR.resolve() in requested.parents:
        return FileResponse(requested)

    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return FileResponse(index_file)

    raise HTTPException(
        status_code=404,
        detail="Frontend no compilado. Ejecuta npm run build y copia frontend/dist a backend/static.",
    )


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _file_download_path(job_id: str) -> str:
    return f"/api/files/{quote(job_id, safe='')}"


def _system_debug_payload() -> dict:
    youtube_cookie_status = get_youtube_cookie_status(settings)
    node_available, node_version, node_supported = _node_runtime_status()
    temp_dir_ready = settings.download_base_dir.is_dir() and os.access(
        settings.download_base_dir,
        os.W_OK | os.X_OK,
    )
    try:
        temp_storage_ephemeral = settings.download_base_dir.resolve().is_relative_to(
            Path("/tmp").resolve()
        )
    except OSError:
        temp_storage_ephemeral = False
    return {
        "ytDlpVersion": ytdlp_version,
        "ffmpegAvailable": shutil.which("ffmpeg") is not None,
        "ffprobeAvailable": shutil.which("ffprobe") is not None,
        "nodeAvailable": node_available,
        "nodeVersion": node_version,
        "nodeSupported": node_supported,
        "denoAvailable": shutil.which("deno") is not None,
        "tempStorageReady": temp_dir_ready,
        "tempStorageEphemeral": temp_storage_ephemeral,
        "maxFileMb": settings.max_file_mb,
        "downloadTimeoutSeconds": settings.download_timeout_seconds,
        "environment": settings.environment,
        "youtubeCookiesEnabled": youtube_cookie_status["enabled"],
        "youtubeCookiesConfigured": youtube_cookie_status["configured"],
        "youtubeCookiesReadable": youtube_cookie_status["readable"],
        "youtubeCookiesMode": youtube_cookie_status["mode"],
    }


@lru_cache(maxsize=1)
def _node_runtime_status() -> tuple[bool, str | None, bool]:
    executable = shutil.which("node") or shutil.which("nodejs")
    if not executable:
        return False, None, False
    try:
        result = subprocess.run(
            [executable, "--version"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return True, None, False
    if result.returncode != 0:
        return True, None, False
    stdout = result.stdout if isinstance(result.stdout, str) else ""
    match = re.fullmatch(
        r"v?([0-9]+)\.([0-9]+)\.([0-9]+)(?:[-+][A-Za-z0-9._-]+)?",
        stdout.strip(),
    )
    if not match:
        return True, None, False
    version = ".".join(match.groups())
    return True, version, int(match.group(1)) >= 22
