import asyncio
import logging
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from starlette.background import BackgroundTask
from yt_dlp.version import __version__ as ytdlp_version

from .cleanup import cleanup_loop
from .config import get_settings
from .downloader import DownloadTooLarge, fetch_metadata
from .jobs import JobGone, JobManager, JobNotReady
from .rate_limit import FixedWindowRateLimiter
from .security import create_token, verify_password, verify_token
from .utils import UserFacingError, validate_media_url


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)

settings = get_settings()
job_manager = JobManager(settings)
login_rate_limiter = FixedWindowRateLimiter(max_failures=8, window_seconds=300)
bearer = HTTPBearer(auto_error=False)


class LoginRequest(BaseModel):
    password: str


class LoginResponse(BaseModel):
    ok: bool
    token: str


class UrlRequest(BaseModel):
    url: str


class DownloadRequest(BaseModel):
    url: str
    quality: Literal["best", "1080", "720", "480", "mp3"]


def require_auth(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> None:
    if not credentials or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="Sesion requerida.")
    if not verify_token(credentials.credentials, settings):
        raise HTTPException(status_code=401, detail="Sesion expirada o invalida.")


def require_debug_auth(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> None:
    if not settings.is_production and credentials is None:
        return
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
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    if request.url.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


@app.exception_handler(UserFacingError)
async def handle_user_error(_request: Request, exc: UserFacingError):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})


@app.exception_handler(Exception)
async def handle_unexpected_error(_request: Request, exc: Exception):
    logger.exception("Unhandled request failure: %s", exc)
    return JSONResponse(status_code=500, content={"detail": "Ocurrio un error interno."})


@app.get("/health")
async def health():
    return {"ok": True, "status": "healthy"}


@app.post("/api/auth/login", response_model=LoginResponse)
async def login(body: LoginRequest, request: Request):
    limiter_key = _client_key(request)
    if login_rate_limiter.is_limited(limiter_key):
        raise HTTPException(
            status_code=429,
            detail="Demasiados intentos. Espera unos minutos y proba de nuevo.",
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


@app.get("/api/files/{job_id}", dependencies=[Depends(require_auth)])
async def file_download(job_id: str):
    try:
        path, filename = job_manager.claim_file(job_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Archivo no encontrado.") from exc
    except JobGone as exc:
        raise HTTPException(status_code=410, detail="Archivo expirado. Genera una nueva descarga.") from exc
    except JobNotReady as exc:
        raise HTTPException(status_code=409, detail="La descarga todavia no esta lista.") from exc

    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=filename,
        background=BackgroundTask(job_manager.finish_delivery, job_id),
    )


@app.delete("/api/jobs/{job_id}", dependencies=[Depends(require_auth)])
async def cancel_job(job_id: str):
    if not job_manager.cancel_job(job_id):
        raise HTTPException(status_code=404, detail="Job no encontrado.")
    return {"ok": True, "status": "expired"}


@app.get("/api/debug/system", dependencies=[Depends(require_debug_auth)])
async def debug_system():
    return _system_debug_payload()


STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


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


def _system_debug_payload() -> dict:
    return {
        "ytDlpVersion": ytdlp_version,
        "ffmpegAvailable": shutil.which("ffmpeg") is not None,
        "ffprobeAvailable": shutil.which("ffprobe") is not None,
        "nodeAvailable": shutil.which("node") is not None or shutil.which("nodejs") is not None,
        "denoAvailable": shutil.which("deno") is not None,
        "tempDir": str(settings.download_base_dir),
        "maxFileMb": settings.max_file_mb,
        "downloadTimeoutSeconds": settings.download_timeout_seconds,
        "environment": settings.environment,
    }
