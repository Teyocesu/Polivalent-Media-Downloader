import time

import pytest

from backend.app.config import Settings, get_settings
from yt_dlp.utils import DownloadError

from backend.app.downloader import (
    DownloadTooLarge,
    _download_options,
    _ensure_size_allowed,
    _map_ytdlp_error,
    _metadata_options,
)
from backend.app.jobs import DownloadJob, JobManager
from backend.app.security import create_token, verify_token
from backend.app.utils import UserFacingError, normalize_url, sanitize_filename, validate_media_url
from backend.app.youtube_auth import get_youtube_cookie_options, get_youtube_cookie_status


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
    assert (
        validate_media_url(
            "https://www.youtube.com/watch?v=m4Be0hRQGIs&list=RDm4Be0hRQGIs&start_radio=1",
            settings,
        ).url
        == "https://www.youtube.com/watch?v=m4Be0hRQGIs"
    )

    with pytest.raises(UserFacingError):
        validate_media_url("ftp://youtube.com/watch?v=abc", settings)
    with pytest.raises(UserFacingError):
        validate_media_url("https://user:pass@youtube.com/watch?v=abc", settings)
    with pytest.raises(UserFacingError):
        validate_media_url("https://www.youtube.com/playlist?list=PL123", settings)


def test_normalize_youtube_url_variants(tmp_path):
    assert (
        normalize_url("https://www.youtube.com/watch?v=m4Be0hRQGIs&list=RDm4Be0hRQGIs&start_radio=1")
        == "https://www.youtube.com/watch?v=m4Be0hRQGIs"
    )
    assert normalize_url("https://youtu.be/m4Be0hRQGIs?si=abc") == (
        "https://www.youtube.com/watch?v=m4Be0hRQGIs"
    )
    assert normalize_url("https://m.youtube.com/watch?v=m4Be0hRQGIs&feature=share") == (
        "https://www.youtube.com/watch?v=m4Be0hRQGIs"
    )
    assert normalize_url("https://www.youtube.com/shorts/m4Be0hRQGIs?si=abc") == (
        "https://www.youtube.com/watch?v=m4Be0hRQGIs"
    )
    with pytest.raises(UserFacingError, match="playlist"):
        normalize_url("https://www.youtube.com/playlist?list=PL123")


def test_size_limits_and_format_options(tmp_path):
    settings = make_settings(tmp_path, max_file_mb=1)
    with pytest.raises(DownloadTooLarge):
        _ensure_size_allowed({"filesize": 2 * 1024 * 1024}, settings)

    best = _download_options("best", tmp_path, settings, lambda data: None, lambda data: None)
    assert "mp4" in best["format"]
    assert best["merge_output_format"] == "mp4"
    assert best["concurrent_fragment_downloads"] == 4
    assert "cookiefile" not in best
    assert "cookiesfrombrowser" not in best

    mp3 = _download_options("mp3", tmp_path, settings, lambda data: None, lambda data: None)
    assert mp3["postprocessors"][0]["preferredcodec"] == "mp3"

    selector_720 = _download_options("720", tmp_path, settings, lambda data: None, lambda data: None)
    assert "height<=720" in selector_720["format"]


def test_youtube_cookie_file_options(tmp_path):
    missing = Settings(
        **{
            **make_settings(tmp_path).__dict__,
            "youtube_cookies_enabled": True,
            "youtube_cookies_path": tmp_path / "missing-cookies.txt",
        }
    )
    missing_status = get_youtube_cookie_status(missing)
    assert missing_status == {
        "enabled": True,
        "configured": True,
        "readable": False,
        "mode": "file",
    }
    assert get_youtube_cookie_options(missing) == {}

    cookie_file = tmp_path / "youtube-cookies.txt"
    cookie_file.write_text(
        "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tTEST_COOKIE\tplaceholder\n"
    )
    configured = Settings(
        **{
            **make_settings(tmp_path).__dict__,
            "youtube_cookies_enabled": True,
            "youtube_cookies_path": cookie_file,
        }
    )
    assert get_youtube_cookie_status(configured)["readable"] is True
    assert get_youtube_cookie_options(configured) == {"cookiefile": str(cookie_file)}

    metadata_options = _metadata_options(
        configured,
        {"debug_code": "default", "options": {}},
        "https://www.youtube.com/watch?v=m4Be0hRQGIs",
    )
    assert metadata_options["cookiefile"] == str(cookie_file)

    download_options = _download_options(
        "720",
        tmp_path,
        configured,
        lambda data: None,
        lambda data: None,
        url="https://www.youtube.com/watch?v=m4Be0hRQGIs",
    )
    assert download_options["cookiefile"] == str(cookie_file)

    x_options = _download_options(
        "720",
        tmp_path,
        configured,
        lambda data: None,
        lambda data: None,
        url="https://x.com/user/status/123",
    )
    assert "cookiefile" not in x_options


def test_youtube_browser_cookie_mode_is_local_only(tmp_path):
    local = Settings(
        **{
            **make_settings(tmp_path).__dict__,
            "youtube_cookies_enabled": True,
            "youtube_cookies_from_browser": "chrome",
        }
    )
    assert get_youtube_cookie_status(local)["mode"] == "browser"
    assert get_youtube_cookie_options(local) == {"cookiesfrombrowser": ("chrome",)}

    production = Settings(**{**local.__dict__, "environment": "production"})
    assert get_youtube_cookie_status(production)["mode"] == "none"
    assert get_youtube_cookie_options(production) == {}


def test_error_mapping():
    assert _map_ytdlp_error(DownloadError("Sign in to confirm your age")).status_code == 403
    assert "login" in _map_ytdlp_error(DownloadError("This video is private")).message
    assert "formatos" in _map_ytdlp_error(DownloadError("No video formats found")).message
    assert "red" in _map_ytdlp_error(DownloadError("Connection reset by peer")).message
    assert "red" in _map_ytdlp_error(DownloadError("Could not resolve host: www.youtube.com")).message
    assert "ffmpeg" in _map_ytdlp_error(DownloadError("ffmpeg is not installed")).message
    assert _map_ytdlp_error(DownloadError("This video is DRM protected")).status_code == 403


def test_youtube_no_bot_error_mapping(tmp_path):
    no_cookies = make_settings(tmp_path)
    no_bot = DownloadError("Sign in to confirm you're not a bot. Use --cookies-from-browser or --cookies")
    assert "cookies de YouTube" in _map_ytdlp_error(no_bot, no_cookies).message

    enabled_missing = Settings(
        **{
            **no_cookies.__dict__,
            "youtube_cookies_enabled": True,
            "youtube_cookies_path": tmp_path / "missing.txt",
        }
    )
    assert "archivo no existe" in _map_ytdlp_error(no_bot, enabled_missing).message

    cookie_file = tmp_path / "youtube-cookies.txt"
    cookie_file.write_text("# Netscape HTTP Cookie File\n")
    enabled_readable = Settings(
        **{
            **no_cookies.__dict__,
            "youtube_cookies_enabled": True,
            "youtube_cookies_path": cookie_file,
        }
    )
    assert "rechazó" in _map_ytdlp_error(no_bot, enabled_readable).message


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
