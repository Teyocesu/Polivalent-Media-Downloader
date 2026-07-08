import logging
import secrets
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from .config import Settings
from .downloader import DownloadCancelled, DownloadTimedOut, download_media
from .utils import UserFacingError, safe_remove_tree


logger = logging.getLogger(__name__)


class JobGone(Exception):
    pass


class JobNotReady(Exception):
    pass


@dataclass
class DownloadJob:
    job_id: str
    url: str
    quality: str
    site: str
    temp_dir: Path
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    status: str = "queued"
    progress: int = 0
    message: str = "En cola..."
    filename: str | None = None
    file_path: Path | None = None
    delivered: bool = False
    cancel_requested: bool = False
    future: Future | None = field(default=None, repr=False)
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "jobId": self.job_id,
                "status": self.status,
                "progress": self.progress,
                "message": self.message,
                "filename": self.filename,
            }

    def update(
        self,
        *,
        status: str | None = None,
        progress: int | None = None,
        message: str | None = None,
        filename: str | None = None,
        file_path: Path | None = None,
    ) -> None:
        with self.lock:
            if status:
                self.status = status
            if progress is not None:
                self.progress = max(0, min(100, int(progress)))
            if message:
                self.message = message
            if filename is not None:
                self.filename = filename
            if file_path is not None:
                self.file_path = file_path
            self.updated_at = time.time()


class JobManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.settings.download_base_dir.mkdir(parents=True, exist_ok=True)
        self._jobs: dict[str, DownloadJob] = {}
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="media-download")

    def create_job(self, url: str, quality: str, site: str) -> DownloadJob:
        job_id = secrets.token_urlsafe(18)
        temp_dir = self.settings.download_base_dir / job_id
        temp_dir.mkdir(parents=True, exist_ok=False)
        job = DownloadJob(job_id=job_id, url=url, quality=quality, site=site, temp_dir=temp_dir)
        with self._lock:
            self._jobs[job_id] = job
            job.future = self._executor.submit(self._run_job, job)
        return job

    def get_snapshot(self, job_id: str) -> dict | None:
        job = self._get(job_id)
        return job.snapshot() if job else None

    def claim_file(self, job_id: str) -> tuple[Path, str]:
        job = self._get(job_id)
        if not job:
            raise FileNotFoundError()

        with job.lock:
            if job.delivered or job.status == "expired":
                raise JobGone()
            if job.status != "done":
                raise JobNotReady()
            if not job.file_path or not job.file_path.exists():
                job.status = "expired"
                job.message = "El archivo ya no esta disponible."
                raise JobGone()
            job.delivered = True
            job.status = "expired"
            job.message = "Archivo entregado. Se borrara del servidor automaticamente."
            job.updated_at = time.time()
            return job.file_path, job.filename or job.file_path.name

    def finish_delivery(self, job_id: str) -> None:
        job = self._get(job_id)
        if not job:
            return
        safe_remove_tree(job.temp_dir)

    def cancel_job(self, job_id: str) -> bool:
        job = self._get(job_id)
        if not job:
            return False

        with job.lock:
            job.cancel_requested = True
            if job.future and job.future.cancel():
                job.status = "expired"
                job.message = "Descarga cancelada."
            elif job.status not in {"done", "error", "expired"}:
                job.status = "expired"
                job.message = "Cancelando descarga y limpiando temporales."
            job.updated_at = time.time()

        safe_remove_tree(job.temp_dir)
        return True

    def cleanup_expired(self) -> None:
        cutoff = time.time() - self.settings.download_ttl_minutes * 60
        with self._lock:
            jobs = list(self._jobs.values())

        known_dirs = {job.temp_dir.resolve() for job in jobs}
        for job in jobs:
            with job.lock:
                stale = job.updated_at < cutoff or job.created_at < cutoff
                if stale and job.status in {"queued", "downloading", "processing", "done", "error", "expired"}:
                    job.cancel_requested = True
                    job.status = "expired"
                    job.message = "Descarga expirada y limpiada automaticamente."
                    job.updated_at = time.time()
                    safe_remove_tree(job.temp_dir)

        for child in self.settings.download_base_dir.iterdir():
            try:
                is_stale_orphan = (
                    child.is_dir()
                    and child.resolve() not in known_dirs
                    and child.stat().st_mtime < cutoff
                )
            except OSError:
                continue
            if is_stale_orphan:
                safe_remove_tree(child)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _get(self, job_id: str) -> DownloadJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def _run_job(self, job: DownloadJob) -> None:
        if job.cancel_requested:
            job.update(status="expired", message="Descarga cancelada.")
            safe_remove_tree(job.temp_dir)
            return

        def progress(progress_value: int, message: str, status: str | None) -> None:
            job.update(status=status, progress=progress_value, message=message)

        try:
            file_path, filename = download_media(
                job.url,
                job.quality,
                job.temp_dir,
                self.settings,
                progress,
                lambda: job.cancel_requested,
            )
            if job.cancel_requested:
                job.update(status="expired", message="Descarga cancelada.")
                safe_remove_tree(job.temp_dir)
                return
            job.update(
                status="done",
                progress=100,
                message="Listo para guardar.",
                filename=filename,
                file_path=file_path,
            )
        except DownloadCancelled:
            job.update(status="expired", message="Descarga cancelada.")
            safe_remove_tree(job.temp_dir)
        except DownloadTimedOut:
            job.update(status="error", message="La descarga excedio el tiempo maximo configurado.")
            safe_remove_tree(job.temp_dir)
        except UserFacingError as exc:
            if job.cancel_requested:
                job.update(status="expired", message="Descarga cancelada.")
            else:
                job.update(status="error", message=exc.message)
            safe_remove_tree(job.temp_dir)
        except Exception:
            logger.exception("Unexpected job failure")
            if job.cancel_requested:
                job.update(status="expired", message="Descarga cancelada.")
            else:
                job.update(status="error", message="Ocurrio un error interno durante la descarga.")
            safe_remove_tree(job.temp_dir)
