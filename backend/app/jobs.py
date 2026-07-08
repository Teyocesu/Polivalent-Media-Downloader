import logging
import secrets
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from .config import Settings
from .downloader import PHASE_LABELS, DownloadCancelled, DownloadTimedOut, download_media, fetch_metadata
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
    original_url: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    status: str = "queued"
    phase: str = "queued"
    phase_label: str = "En cola"
    progress: int = 0
    message: str = "En cola..."
    download_percent: float | None = None
    speed: str | None = None
    eta: str | None = None
    downloaded_bytes: int | None = None
    total_bytes: int | None = None
    current_file: str | None = None
    step: int = 1
    steps_total: int = 6
    debug_code: str | None = None
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
                "phase": self.phase,
                "phaseLabel": self.phase_label,
                "progress": self.progress,
                "downloadPercent": self.download_percent,
                "speed": self.speed,
                "eta": self.eta,
                "downloadedBytes": self.downloaded_bytes,
                "totalBytes": self.total_bytes,
                "currentFile": self.current_file,
                "step": self.step,
                "stepsTotal": self.steps_total,
                "message": self.message,
                "filename": self.filename,
            }

    def update(
        self,
        *,
        status: str | None = None,
        phase: str | None = None,
        phase_label: str | None = None,
        progress: int | None = None,
        message: str | None = None,
        download_percent: float | None = None,
        speed: str | None = None,
        eta: str | None = None,
        downloaded_bytes: int | None = None,
        total_bytes: int | None = None,
        current_file: str | None = None,
        step: int | None = None,
        steps_total: int | None = None,
        debug_code: str | None = None,
        filename: str | None = None,
        file_path: Path | None = None,
    ) -> None:
        with self.lock:
            if status:
                self.status = status
            if phase:
                self.phase = phase
                self.phase_label = phase_label or PHASE_LABELS.get(phase, self.phase_label)
            elif phase_label:
                self.phase_label = phase_label
            if progress is not None:
                self.progress = max(0, min(100, int(progress)))
            if message:
                self.message = message
            if download_percent is not None:
                self.download_percent = download_percent
            if speed is not None:
                self.speed = speed
            if eta is not None:
                self.eta = eta
            if downloaded_bytes is not None:
                self.downloaded_bytes = downloaded_bytes
            if total_bytes is not None:
                self.total_bytes = total_bytes
            if current_file is not None:
                self.current_file = current_file
            if step is not None:
                self.step = step
            if steps_total is not None:
                self.steps_total = steps_total
            if debug_code is not None:
                self.debug_code = debug_code
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

    def create_job(
        self,
        url: str,
        quality: str,
        site: str,
        original_url: str | None = None,
    ) -> DownloadJob:
        job_id = secrets.token_urlsafe(18)
        temp_dir = self.settings.download_base_dir / job_id
        temp_dir.mkdir(parents=True, exist_ok=False)
        job = DownloadJob(
            job_id=job_id,
            url=url,
            quality=quality,
            site=site,
            temp_dir=temp_dir,
            original_url=original_url,
            steps_total=6 if quality == "mp3" else 5,
        )
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
            job.phase = "expired"
            job.phase_label = PHASE_LABELS["expired"]
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
                job.phase = "expired"
                job.phase_label = PHASE_LABELS["expired"]
                job.message = "Descarga cancelada."
            elif job.status not in {"done", "error", "expired"}:
                job.status = "expired"
                job.phase = "expired"
                job.phase_label = PHASE_LABELS["expired"]
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
                    job.phase = "expired"
                    job.phase_label = PHASE_LABELS["expired"]
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
            job.update(status="expired", phase="expired", message="Descarga cancelada.")
            safe_remove_tree(job.temp_dir)
            return

        def progress(event: dict) -> None:
            job.update(
                status=event.get("status"),
                phase=event.get("phase"),
                phase_label=event.get("phaseLabel"),
                progress=event.get("progress"),
                message=event.get("message"),
                download_percent=event.get("downloadPercent"),
                speed=event.get("speed"),
                eta=event.get("eta"),
                downloaded_bytes=event.get("downloadedBytes"),
                total_bytes=event.get("totalBytes"),
                current_file=event.get("currentFile"),
                step=event.get("step"),
                steps_total=event.get("stepsTotal"),
                debug_code=event.get("debugCode"),
            )

        try:
            logger.info(
                "job=%s site=%s normalized_url=%s phase=validating_url",
                job.job_id,
                job.site,
                job.url,
            )
            job.update(
                status="downloading",
                phase="validating_url",
                progress=2,
                message="Validando link...",
                step=1,
            )
            job.update(
                status="downloading",
                phase="normalizing_url",
                progress=5,
                message="Limpiando URL...",
                step=2,
            )
            job.update(
                status="downloading",
                phase="extracting_metadata",
                progress=8,
                message="Obteniendo metadata...",
                step=2,
            )
            fetch_metadata(job.url, self.settings)
            file_path, filename = download_media(
                job.url,
                job.quality,
                job.temp_dir,
                self.settings,
                progress,
                lambda: job.cancel_requested,
            )
            if job.cancel_requested:
                job.update(status="expired", phase="expired", message="Descarga cancelada.")
                safe_remove_tree(job.temp_dir)
                return
            job.update(
                status="done",
                phase="done",
                progress=100,
                message="Listo para guardar.",
                filename=filename,
                file_path=file_path,
            )
        except DownloadCancelled:
            job.update(status="expired", phase="expired", message="Descarga cancelada.")
            safe_remove_tree(job.temp_dir)
        except DownloadTimedOut:
            job.update(
                status="error",
                phase="error",
                message="La descarga tardó demasiado y fue cancelada.",
            )
            safe_remove_tree(job.temp_dir)
        except UserFacingError as exc:
            if job.cancel_requested:
                job.update(status="expired", phase="expired", message="Descarga cancelada.")
            else:
                logger.info(
                    "job=%s site=%s phase=error error_type=%s",
                    job.job_id,
                    job.site,
                    type(exc).__name__,
                )
                job.update(status="error", phase="error", message=exc.message)
            safe_remove_tree(job.temp_dir)
        except Exception:
            logger.exception("Unexpected job failure")
            if job.cancel_requested:
                job.update(status="expired", phase="expired", message="Descarga cancelada.")
            else:
                job.update(
                    status="error",
                    phase="error",
                    message="Ocurrio un error interno durante la descarga.",
                )
            safe_remove_tree(job.temp_dir)
