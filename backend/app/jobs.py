import logging
import os
import secrets
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from .config import Settings
from .downloader import PHASE_LABELS, DownloadCancelled, DownloadTimedOut, download_media, fetch_metadata
from .utils import UserFacingError, safe_remove_tree, sanitize_filename, validate_media_url


logger = logging.getLogger(__name__)
MAX_OUTSTANDING_JOBS = 3
MAX_TRACKED_JOBS = 128
OUTSTANDING_STATUSES = {"queued", "downloading", "processing", "done"}


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

    def forget_source(self) -> None:
        with self.lock:
            self.url = ""
            self.original_url = None

    def forget_file(self) -> None:
        with self.lock:
            self.file_path = None
            self.filename = None
            self.current_file = None


class JobManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        try:
            self.settings.download_base_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as exc:
            raise RuntimeError("DOWNLOAD_BASE_DIR no se pudo preparar.") from exc
        try:
            self.settings.download_base_dir.chmod(0o700)
        except OSError:
            logger.warning("Could not restrict temporary download directory permissions")
        if not self.settings.download_base_dir.is_dir() or not os.access(
            self.settings.download_base_dir,
            os.W_OK | os.X_OK,
        ):
            raise RuntimeError("DOWNLOAD_BASE_DIR no es una carpeta escribible.")
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
        with self._lock:
            self._make_job_room_locked()
            outstanding = sum(
                job.status in OUTSTANDING_STATUSES for job in self._jobs.values()
            )
            if outstanding >= MAX_OUTSTANDING_JOBS:
                raise UserFacingError(
                    "Ya hay demasiadas descargas pendientes. Terminá, cancelá o guardá una antes de crear otra.",
                    status_code=429,
                )

            for _attempt in range(5):
                job_id = secrets.token_urlsafe(18)
                temp_dir = self.settings.download_base_dir / job_id
                try:
                    temp_dir.mkdir(parents=False, exist_ok=False, mode=0o700)
                except FileExistsError:
                    continue
                break
            else:
                raise RuntimeError("No se pudo crear una carpeta temporal unica.")
            job = DownloadJob(
                job_id=job_id,
                url=url,
                quality=quality,
                site=site,
                temp_dir=temp_dir,
                original_url=original_url,
                steps_total=6 if quality == "mp3" else 5,
            )
            self._jobs[job_id] = job
            try:
                job.future = self._executor.submit(self._run_job, job)
            except Exception:
                self._jobs.pop(job_id, None)
                safe_remove_tree(temp_dir)
                raise
        return job

    def _make_job_room_locked(self) -> None:
        if len(self._jobs) < MAX_TRACKED_JOBS:
            return
        removable = sorted(
            (
                job
                for job in self._jobs.values()
                if job.status in {"error", "expired"}
                and (not job.delivered or not job.temp_dir.exists())
            ),
            key=lambda job: job.updated_at,
        )
        for job in removable:
            if len(self._jobs) < MAX_TRACKED_JOBS:
                break
            self._jobs.pop(job.job_id, None)
            safe_remove_tree(job.temp_dir)
        if len(self._jobs) >= MAX_TRACKED_JOBS:
            raise UserFacingError(
                "Hay demasiadas descargas registradas. Esperá a que expiren o limpiá las anteriores.",
                status_code=429,
            )

    def get_snapshot(self, job_id: str) -> dict | None:
        job = self._get(job_id)
        return job.snapshot() if job else None

    def ensure_file_ready(self, job_id: str) -> None:
        job = self._get(job_id)
        if not job:
            raise FileNotFoundError()
        with job.lock:
            self._get_ready_file(job)

    def claim_file(self, job_id: str) -> tuple[Path, str]:
        job = self._get(job_id)
        if not job:
            raise FileNotFoundError()

        with job.lock:
            file_path = self._get_ready_file(job)
            job.delivered = True
            job.status = "expired"
            job.phase = "expired"
            job.phase_label = PHASE_LABELS["expired"]
            job.message = "Archivo entregado. Se borrara del servidor automaticamente."
            job.updated_at = time.time()
            job.forget_source()
            filename = sanitize_filename(job.filename or file_path.name)
            job.forget_file()
            return file_path, filename

    def finish_delivery(self, job_id: str) -> None:
        job = self._get(job_id)
        if not job:
            return
        safe_remove_tree(job.temp_dir)
        job.forget_file()

    def cancel_job(self, job_id: str) -> bool:
        job = self._get(job_id)
        if not job:
            return False

        with job.lock:
            job.cancel_requested = True
            if job.future and job.future.cancel():
                job.message = "Descarga cancelada."
            elif job.status not in {"done", "error", "expired"}:
                job.message = "Cancelando descarga y limpiando temporales."
            else:
                job.message = "Descarga eliminada y temporales limpiados."
            job.status = "expired"
            job.phase = "expired"
            job.phase_label = PHASE_LABELS["expired"]
            job.updated_at = time.time()
            job.forget_source()
            job.forget_file()

        safe_remove_tree(job.temp_dir)
        return True

    def cleanup_expired(self) -> None:
        cutoff = time.time() - self.settings.download_ttl_minutes * 60
        with self._lock:
            jobs = list(self._jobs.values())

        known_dirs = {job.temp_dir.resolve() for job in jobs}
        purged_jobs: list[DownloadJob] = []
        for job in jobs:
            with job.lock:
                stale = job.updated_at < cutoff
                if stale and job.status in {"queued", "downloading", "processing", "done", "error", "expired"}:
                    job.cancel_requested = True
                    job.status = "expired"
                    job.phase = "expired"
                    job.phase_label = PHASE_LABELS["expired"]
                    job.message = "Descarga expirada y limpiada automaticamente."
                    job.updated_at = time.time()
                    job.forget_source()
                    job.forget_file()
                    safe_remove_tree(job.temp_dir)
                    purged_jobs.append(job)

        if purged_jobs:
            with self._lock:
                for job in purged_jobs:
                    if self._jobs.get(job.job_id) is job:
                        self._jobs.pop(job.job_id, None)

        for child in self.settings.download_base_dir.iterdir():
            try:
                is_stale_orphan = child.resolve() not in known_dirs and child.lstat().st_mtime < cutoff
            except OSError:
                continue
            if is_stale_orphan:
                safe_remove_tree(child)

    def shutdown(self) -> None:
        with self._lock:
            jobs = list(self._jobs.values())
        for job in jobs:
            with job.lock:
                job.cancel_requested = True
                if job.status not in {"error", "expired"}:
                    job.status = "expired"
                    job.phase = "expired"
                    job.phase_label = PHASE_LABELS["expired"]
                    job.message = "Descarga cancelada por cierre del servidor."
                job.forget_source()
                job.forget_file()
            safe_remove_tree(job.temp_dir)
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
            with job.lock:
                if job.cancel_requested or job.delivered or job.status == "expired":
                    return
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
                "job=%s site=%s phase=validating_url",
                job.job_id,
                job.site,
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
                message="Normalizando URL...",
                step=2,
            )
            job.update(
                status="downloading",
                phase="extracting_metadata",
                progress=8,
                message="Obteniendo metadata...",
                step=2,
            )
            validated = validate_media_url(job.url, self.settings)
            with job.lock:
                if job.cancel_requested:
                    raise DownloadCancelled()
                job.url = validated.url
                job.site = validated.site
            fetch_metadata(validated.url, self.settings)
            file_path, filename = download_media(
                validated.url,
                job.quality,
                job.temp_dir,
                self.settings,
                progress,
                lambda: job.cancel_requested,
            )
            file_path = self._resolve_job_file(job, file_path)
            filename = sanitize_filename(filename or file_path.name)
            with job.lock:
                cancelled = job.cancel_requested
                if not cancelled:
                    final_size = file_path.stat().st_size
                    job.update(
                        status="done",
                        phase="done",
                        progress=100,
                        message="Listo para guardar.",
                        downloaded_bytes=final_size,
                        total_bytes=final_size,
                        current_file=filename,
                        filename=filename,
                        file_path=file_path,
                    )
                    job.forget_source()
            if cancelled:
                job.update(status="expired", phase="expired", message="Descarga cancelada.")
                job.forget_source()
                job.forget_file()
                safe_remove_tree(job.temp_dir)
                return
        except DownloadCancelled:
            job.update(status="expired", phase="expired", message="Descarga cancelada.")
            job.forget_source()
            job.forget_file()
            safe_remove_tree(job.temp_dir)
        except DownloadTimedOut:
            job.update(
                status="error",
                phase="error",
                message="La descarga tardó demasiado y fue cancelada.",
            )
            job.forget_source()
            job.forget_file()
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
            job.forget_source()
            job.forget_file()
            safe_remove_tree(job.temp_dir)
        except Exception as exc:
            logger.error("Unexpected job failure error_type=%s", type(exc).__name__)
            if job.cancel_requested:
                job.update(status="expired", phase="expired", message="Descarga cancelada.")
            else:
                job.update(
                    status="error",
                    phase="error",
                    message="Ocurrio un error interno durante la descarga.",
                )
            job.forget_source()
            job.forget_file()
            safe_remove_tree(job.temp_dir)

    @staticmethod
    def _resolve_job_file(job: DownloadJob, file_path: Path | None) -> Path:
        if file_path is None or file_path.is_symlink():
            raise FileNotFoundError()
        temp_root = job.temp_dir.resolve(strict=True)
        resolved = file_path.resolve(strict=True)
        if not resolved.is_file() or not resolved.is_relative_to(temp_root):
            raise ValueError("Job output escaped its temporary directory")
        return resolved

    def _get_ready_file(self, job: DownloadJob) -> Path:
        if job.delivered or job.status == "expired":
            raise JobGone()
        if job.status != "done":
            raise JobNotReady()
        try:
            return self._resolve_job_file(job, job.file_path)
        except (FileNotFoundError, OSError, ValueError):
            job.status = "expired"
            job.phase = "expired"
            job.phase_label = PHASE_LABELS["expired"]
            job.message = "El archivo ya no esta disponible."
            job.forget_source()
            job.forget_file()
            safe_remove_tree(job.temp_dir)
            raise JobGone()
