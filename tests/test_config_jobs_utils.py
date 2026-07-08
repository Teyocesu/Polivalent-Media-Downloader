import time

import pytest

from backend.app.config import Settings, get_settings
from backend.app.downloader import DownloadTooLarge, _download_options, _ensure_size_allowed
from backend.app.jobs import DownloadJob, JobManager
from backend.app.security import create_token, verify_token
from backend.app.utils import UserFacingError, sanitize_filename, validate_media_url


def make_settings(tmp_path, max_file_mb=1, ttl_minutes=1):
    return Settings(
        app_password="test-password",
        token_secret="test-secret",
        max_file_mb=max_file_mb,
        download_ttl_minutes=ttl_minutes,
        download_timeout_seconds=5,
        session_ttl_seconds=60,
        allowed_origins=["http://localhost:5173"],
        allowed_domains={
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
        },
        download_base_dir=tmp_path / "downloads",
        port=8000,
        environment="development",
    )


def test_app_password_is_required_in_production(monkeypatch):
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    monkeypatch.setenv("ENVIRONMENT", "production")
    get_settings.cache_clear()
    with pytest.raises(RuntimeError):
        get_settings()

    monkeypatch.setenv("APP_PASSWORD", "test-password")
    monkeypatch.setenv("ENVIRONMENT", "development")
    get_settings.cache_clear()


def test_tokens_are_signed_and_expire(tmp_path):
    settings = make_settings(tmp_path)
    token = create_token(settings)
    assert verify_token(token, settings) is True
    assert verify_token(token + "x", settings) is False

    expired = Settings(**{**settings.__dict__, "session_ttl_seconds": -1})
    assert verify_token(create_token(expired), expired) is False


def test_url_validation_domains_playlists_and_credentials(tmp_path):
    settings = make_settings(tmp_path)
    assert validate_media_url("https://x.com/user/status/123", settings).site == "X / Twitter"
    assert validate_media_url("https://www.instagram.com/reel/demo/", settings).site == "Instagram"

    with pytest.raises(UserFacingError):
        validate_media_url("ftp://youtube.com/watch?v=abc", settings)
    with pytest.raises(UserFacingError):
        validate_media_url("https://user:pass@youtube.com/watch?v=abc", settings)
    with pytest.raises(UserFacingError):
        validate_media_url("https://www.youtube.com/playlist?list=PL123", settings)


def test_size_limits_and_format_options(tmp_path):
    settings = make_settings(tmp_path, max_file_mb=1)
    with pytest.raises(DownloadTooLarge):
        _ensure_size_allowed({"filesize": 2 * 1024 * 1024}, settings)

    best = _download_options("best", tmp_path, settings, lambda data: None, lambda data: None)
    assert "mp4" in best["format"]
    assert best["merge_output_format"] == "mp4"

    mp3 = _download_options("mp3", tmp_path, settings, lambda data: None, lambda data: None)
    assert mp3["postprocessors"][0]["preferredcodec"] == "mp3"


def test_sanitize_filename(tmp_path):
    assert sanitize_filename("../bad name?.mp4") == "bad_name.mp4"
    assert sanitize_filename("...") == "media"


def test_cleanup_expires_old_jobs_and_orphan_dirs(tmp_path):
    settings = make_settings(tmp_path, ttl_minutes=1)
    manager = JobManager(settings)
    try:
        old_dir = settings.download_base_dir / "old-job"
        old_dir.mkdir(parents=True)
        (old_dir / "clip.mp4").write_bytes(b"x")
        old_time = time.time() - 3600

        job = DownloadJob(
            job_id="old-job",
            url="https://youtu.be/dQw4w9WgXcQ",
            quality="best",
            site="YouTube",
            temp_dir=old_dir,
            created_at=old_time,
            updated_at=old_time,
            status="done",
        )
        manager._jobs[job.job_id] = job

        orphan = settings.download_base_dir / "orphan"
        orphan.mkdir()
        (orphan / "partial.part").write_bytes(b"x")
        old_mtime = time.time() - 3600
        orphan.touch()
        for child in orphan.iterdir():
            child.touch()
        import os

        os.utime(orphan, (old_mtime, old_mtime))

        manager.cleanup_expired()
        assert job.status == "expired"
        assert not old_dir.exists()
        assert not orphan.exists()
    finally:
        manager.shutdown()
