import logging
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


logger = logging.getLogger(__name__)


DEFAULT_ALLOWED_DOMAINS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "youtu.be",
    "tiktok.com",
    "www.tiktok.com",
    "vm.tiktok.com",
    "vt.tiktok.com",
    "instagram.com",
    "www.instagram.com",
    "x.com",
    "www.x.com",
    "twitter.com",
    "www.twitter.com",
}


def _parse_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} debe ser un numero entero.") from exc
    if parsed <= 0:
        raise RuntimeError(f"{name} debe ser mayor que cero.")
    return parsed


def _parse_csv(name: str) -> list[str]:
    value = os.getenv(name, "")
    return [item.strip() for item in value.split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    app_password: str
    token_secret: str
    max_file_mb: int
    download_ttl_minutes: int
    download_timeout_seconds: int
    session_ttl_seconds: int
    allowed_origins: list[str]
    allowed_domains: set[str]
    download_base_dir: Path
    port: int
    environment: str

    @property
    def max_file_bytes(self) -> int:
        return self.max_file_mb * 1024 * 1024

    @property
    def is_production(self) -> bool:
        return self.environment.lower() in {"prod", "production"} or bool(os.getenv("RENDER"))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    environment = os.getenv("ENVIRONMENT", "development")
    is_production = environment.lower() in {"prod", "production"} or bool(os.getenv("RENDER"))
    app_password = os.getenv("APP_PASSWORD")

    if not app_password:
        if is_production:
            raise RuntimeError("APP_PASSWORD es obligatoria en produccion.")
        app_password = "dev-password-change-me"
        logger.warning(
            "APP_PASSWORD no esta configurada. Usando contrasena de desarrollo solo para local."
        )

    token_secret = os.getenv("APP_SECRET_KEY") or app_password

    return Settings(
        app_password=app_password,
        token_secret=token_secret,
        max_file_mb=_parse_int("MAX_FILE_MB", 500),
        download_ttl_minutes=_parse_int("DOWNLOAD_TTL_MINUTES", 15),
        download_timeout_seconds=_parse_int("DOWNLOAD_TIMEOUT_SECONDS", 600),
        session_ttl_seconds=_parse_int("SESSION_TTL_SECONDS", 24 * 60 * 60),
        allowed_origins=_parse_csv("ALLOWED_ORIGINS"),
        allowed_domains=set(DEFAULT_ALLOWED_DOMAINS),
        download_base_dir=Path(os.getenv("DOWNLOAD_BASE_DIR", "/tmp/media-downloads")),
        port=_parse_int("PORT", 8000),
        environment=environment,
    )
