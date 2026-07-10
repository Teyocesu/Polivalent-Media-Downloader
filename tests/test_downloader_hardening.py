import logging
import os
from pathlib import Path
import subprocess
import sys

import pytest
from yt_dlp.utils import DownloadError

from backend.app import downloader
from backend.app.config import Settings, get_settings
from backend.app.downloader import (
    DownloadTooLarge,
    _attempt_profiles,
    _clean_error,
    _download_options,
    _ensure_size_allowed,
    _map_ytdlp_error,
    _metadata_options,
    fetch_metadata,
)
from backend.app.youtube_auth import MAX_COOKIE_FILE_BYTES, get_youtube_cookie_status
from backend.app.utils import UserFacingError


def make_settings(tmp_path: Path, **overrides) -> Settings:
    values = {
        "app_password": "test-password",
        "token_secret": "test-secret",
        "max_file_mb": 1,
        "download_ttl_minutes": 1,
        "download_timeout_seconds": 30,
        "session_ttl_seconds": 60,
        "allowed_origins": [],
        "allowed_domains": {"youtube.com", "www.youtube.com", "music.youtube.com", "youtu.be"},
        "download_base_dir": tmp_path / "downloads",
        "port": 8000,
        "environment": "development",
    }
    values.update(overrides)
    return Settings(**values)


def test_missing_cookie_file_is_runtime_diagnostic_not_startup_failure(monkeypatch, tmp_path):
    missing = tmp_path / "youtube-cookies.txt"
    monkeypatch.setenv("APP_PASSWORD", "test-password")
    monkeypatch.setenv("YOUTUBE_COOKIES_ENABLED", "true")
    monkeypatch.setenv("YOUTUBE_COOKIES_PATH", str(missing))
    monkeypatch.setenv("YTDLP_SOCKET_TIMEOUT_SECONDS", "7")
    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert settings.ytdlp_socket_timeout_seconds == 7
        assert get_youtube_cookie_status(settings) == {
            "enabled": True,
            "configured": True,
            "readable": False,
            "mode": "file",
        }
    finally:
        get_settings.cache_clear()


def test_blank_production_password_is_rejected_and_blank_paths_use_safe_defaults(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("APP_PASSWORD", "   ")
    monkeypatch.delenv("RENDER", raising=False)
    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="APP_PASSWORD"):
        get_settings()

    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("APP_PASSWORD", "local-password")
    monkeypatch.setenv("APP_SECRET_KEY", "   ")
    monkeypatch.setenv("DOWNLOAD_BASE_DIR", "   ")
    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert settings.token_secret == "local-password"
        assert settings.download_base_dir == Path("/tmp/media-downloads")
    finally:
        get_settings.cache_clear()


def test_production_module_starts_when_configured_cookie_file_is_missing(tmp_path):
    missing = tmp_path / "youtube-cookies.txt"
    env = {
        **os.environ,
        "APP_PASSWORD": "production-test-password",
        "APP_SECRET_KEY": "production-test-secret",
        "ENVIRONMENT": "production",
        "RENDER": "true",
        "DOWNLOAD_BASE_DIR": str(tmp_path / "runtime"),
        "YOUTUBE_COOKIES_ENABLED": "true",
        "YOUTUBE_COOKIES_PATH": str(missing),
        "YOUTUBE_COOKIES_FROM_BROWSER": "none",
    }
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from backend.app.main import _system_debug_payload; "
                "print(_system_debug_payload()['youtubeCookiesReadable'])"
            ),
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False"
    assert str(missing) not in result.stdout + result.stderr


def test_youtube_enables_node_and_only_applies_youtube_cookie_file(tmp_path):
    cookie_file = tmp_path / "youtube-cookies.txt"
    cookie_file.write_text("# Netscape HTTP Cookie File\n")
    settings = make_settings(
        tmp_path,
        youtube_cookies_enabled=True,
        youtube_cookies_path=cookie_file,
        ytdlp_socket_timeout_seconds=9,
    )

    metadata = _metadata_options(
        settings,
        {"debug_code": "default", "options": {}},
        "https://music.youtube.com/watch?v=abcdef",
    )
    assert metadata["js_runtimes"] == {"node": {"path": None}}
    assert metadata["cookiefile"].read() == "# Netscape HTTP Cookie File\n"
    assert metadata["socket_timeout"] == 9
    assert metadata["retries"] == 1
    assert metadata["extractor_retries"] == 1

    youtube_download = _download_options(
        "720",
        tmp_path,
        settings,
        lambda _data: None,
        lambda _data: None,
        url="https://www.youtube.com/watch?v=abcdef",
    )
    assert youtube_download["js_runtimes"] == {"node": {"path": None}}
    assert youtube_download["cookiefile"].read() == "# Netscape HTTP Cookie File\n"

    other_site = _download_options(
        "720",
        tmp_path,
        settings,
        lambda _data: None,
        lambda _data: None,
        url="https://x.com/user/status/123",
    )
    assert "js_runtimes" not in other_site
    assert "cookiefile" not in other_site


def test_size_preflight_counts_only_selected_formats(tmp_path):
    settings = make_settings(tmp_path)

    # Metadata may list a huge 4K format even when the user selected 720p. It
    # must not be rejected merely because that unselected format exists.
    _ensure_size_allowed(
        {"formats": [{"height": 2160, "filesize": 50 * 1024 * 1024}]},
        settings,
    )

    with pytest.raises(DownloadTooLarge) as exc_info:
        _ensure_size_allowed(
            {
                "requested_formats": [
                    {"filesize": 700 * 1024},
                    {"filesize_approx": 400 * 1024},
                ]
            },
            settings,
        )
    assert exc_info.value.status_code == 413


def test_fetch_metadata_does_not_reject_an_unselected_large_format(monkeypatch, tmp_path):
    class FakeYoutubeDL:
        def __init__(self, _options):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, _url, download=False):
            assert download is False
            return {
                "title": "Short clip",
                "extractor_key": "Youtube",
                "formats": [{"height": 2160, "filesize": 50 * 1024 * 1024}],
            }

    monkeypatch.setattr(downloader, "YoutubeDL", FakeYoutubeDL)
    metadata = fetch_metadata("https://www.youtube.com/watch?v=abcdef", make_settings(tmp_path))
    assert metadata["title"] == "Short clip"


def test_network_failure_does_not_multiply_youtube_client_attempts(monkeypatch, tmp_path):
    calls = 0

    class FailingYoutubeDL:
        def __init__(self, _options):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, _url, download=False):
            nonlocal calls
            calls += 1
            raise DownloadError("Could not resolve host: www.youtube.com")

    monkeypatch.setattr(downloader, "YoutubeDL", FailingYoutubeDL)
    with pytest.raises(UserFacingError) as exc_info:
        fetch_metadata("https://www.youtube.com/watch?v=abcdef", make_settings(tmp_path))
    assert "red" in str(exc_info.value)
    assert calls == 1


def test_download_aborts_when_progress_reports_oversized_file(monkeypatch, tmp_path):
    class FakeYoutubeDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def download(self, _urls):
            self.options["progress_hooks"][0](
                {"status": "downloading", "total_bytes": 2 * 1024 * 1024}
            )
            return 0

    monkeypatch.setattr(downloader, "YoutubeDL", FakeYoutubeDL)
    monkeypatch.setattr(
        downloader,
        "_attempt_profiles",
        lambda _url: [{"debug_code": "default", "options": {}}],
    )

    with pytest.raises(DownloadTooLarge) as exc_info:
        downloader.download_media(
            "https://www.youtube.com/watch?v=abcdef",
            "720",
            tmp_path / "job",
            make_settings(tmp_path),
            lambda _event: None,
            lambda: False,
        )
    assert exc_info.value.status_code == 413


def test_failed_attempt_artifacts_cannot_be_delivered_as_the_final_file(monkeypatch, tmp_path):
    calls = 0

    class FakeYoutubeDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def download(self, _urls):
            nonlocal calls
            calls += 1
            output_dir = Path(self.options["outtmpl"]).parent
            if calls == 1:
                (output_dir / "wrong-video-only.mp4").write_bytes(b"x" * 100)
                raise DownloadError("temporary extractor issue")
            (output_dir / "final-with-audio.mp4").write_bytes(b"ok")
            return 0

    monkeypatch.setattr(downloader, "YoutubeDL", FakeYoutubeDL)
    monkeypatch.setattr(
        downloader,
        "_attempt_profiles",
        lambda _url: [
            {"debug_code": "first", "options": {}},
            {"debug_code": "second", "options": {}},
        ],
    )

    output, _ = downloader.download_media(
        "https://www.youtube.com/watch?v=m4Be0hRQGIs",
        "720",
        tmp_path / "job",
        make_settings(tmp_path),
        lambda _event: None,
        lambda: False,
    )
    assert calls == 2
    assert output.name == "final-with-audio.mp4"
    assert not (output.parent / "wrong-video-only.mp4").exists()


def test_mobile_friendly_selectors_and_bounded_retries(tmp_path):
    settings = make_settings(tmp_path)
    options_720 = _download_options(
        "720", tmp_path, settings, lambda _data: None, lambda _data: None
    )
    assert options_720["format"].startswith(
        "bestvideo[height<=?720][vcodec^=avc1]+bestaudio[ext=m4a]"
    )
    assert all("height<=?720" in alternative for alternative in options_720["format"].split("/"))
    assert "max_filesize" not in options_720
    assert options_720["merge_output_format"] == "mp4"
    assert options_720["retries"] == 3
    assert options_720["fragment_retries"] == 3
    assert options_720["concurrent_fragment_downloads"] == 4
    assert options_720["overwrites"] is False

    mp3 = _download_options(
        "mp3", tmp_path, settings, lambda _data: None, lambda _data: None
    )
    assert mp3["format"].startswith("bestaudio[ext=m4a]")
    assert mp3["postprocessors"][0]["preferredquality"] == "192"

    assert len(_attempt_profiles("https://www.youtube.com/watch?v=abcdef")) <= 3
    assert len(_attempt_profiles("https://www.instagram.com/reel/example/")) <= 2


def test_youtube_cookie_rejection_and_rate_limit_errors_are_actionable(tmp_path):
    cookie_file = tmp_path / "youtube-cookies.txt"
    cookie_file.write_text("# Netscape HTTP Cookie File\n")
    configured = make_settings(
        tmp_path,
        youtube_cookies_enabled=True,
        youtube_cookies_path=cookie_file,
    )
    rejected = _map_ytdlp_error(
        DownloadError("The provided YouTube account cookies are no longer valid"),
        configured,
        url="https://www.youtube.com/watch?v=abcdef",
    )
    assert rejected.status_code == 403
    assert "Exportá cookies nuevas" in rejected.message

    malformed = _map_ytdlp_error(
        DownloadError("Failed to parse cookies: invalid Netscape format cookies file"),
        configured,
        url="https://www.youtube.com/watch?v=abcdef",
    )
    assert malformed.status_code == 403
    assert "Exportá cookies nuevas" in malformed.message

    missing = make_settings(
        tmp_path,
        youtube_cookies_enabled=True,
        youtube_cookies_path=tmp_path / "missing.txt",
    )
    no_bot = _map_ytdlp_error(
        DownloadError("Sign in to confirm you're not a bot"),
        missing,
        url="https://www.youtube.com/watch?v=abcdef",
    )
    assert no_bot.status_code == 500
    assert "archivo no existe" in no_bot.message

    rate_limited = _map_ytdlp_error(
        DownloadError("HTTP Error 429: Too Many Requests"),
        make_settings(tmp_path),
        url="https://www.youtube.com/watch?v=abcdef",
    )
    assert rate_limited.status_code == 403
    assert "cookies de YouTube" in rate_limited.message


def test_safe_diagnostics_redact_urls_paths_and_credentials():
    cleaned = _clean_error(
        DownloadError(
            "[youtube] PRIVATE_MEDIA_ID: failed https://example.com/video?access_token=secret "
            "cookie: SESSION=secret /etc/secrets/youtube-cookies.txt"
        )
    )
    assert "example.com" in cleaned
    assert "access_token" not in cleaned
    assert "SESSION" not in cleaned
    assert "PRIVATE_MEDIA_ID" not in cleaned
    assert "/etc/secrets" not in cleaned
    assert "youtube-cookies.txt" not in cleaned


def test_embedded_ytdlp_logger_never_emits_raw_sensitive_values(caplog, tmp_path):
    options = _metadata_options(
        make_settings(tmp_path),
        {"debug_code": "default", "options": {}},
        "https://www.youtube.com/watch?v=abcdef",
    )
    with caplog.at_level(logging.ERROR, logger="backend.app.downloader"):
        options["logger"].error(
            "failed https://example.com/watch?token=raw-secret "
            "cookie: SESSION=raw-secret /mnt/secrets/youtube-cookies.txt"
        )

    assert "example.com" in caplog.text
    assert "raw-secret" not in caplog.text
    assert "/mnt/secrets" not in caplog.text
    assert "youtube-cookies.txt" not in caplog.text


def test_external_unavailable_and_ip_block_errors_are_not_reported_as_update_failures(tmp_path):
    unavailable = _map_ytdlp_error(
        DownloadError("ERROR: [youtube] private-id: Video unavailable"),
        make_settings(tmp_path),
        url="https://www.youtube.com/watch?v=abcdef",
    )
    assert unavailable.status_code == 404
    assert "no está disponible" in unavailable.message
    assert "actualizar yt-dlp" not in unavailable.message

    ip_blocked = _map_ytdlp_error(
        DownloadError("Your IP address is blocked from accessing this post"),
        make_settings(tmp_path),
        url="https://www.tiktok.com/@demo/video/123456",
    )
    assert ip_blocked.status_code == 503
    assert "IP del servidor" in ip_blocked.message


def test_read_only_cookie_source_is_never_rewritten(tmp_path):
    cookie_file = tmp_path / "youtube-cookies.txt"
    original = (
        "# Netscape HTTP Cookie File\n"
        ".youtube.com\tTRUE\t/\tTRUE\t0\tTEST_COOKIE\tplaceholder\n"
    )
    cookie_file.write_text(original)
    cookie_file.chmod(0o440)
    settings = make_settings(
        tmp_path,
        youtube_cookies_enabled=True,
        youtube_cookies_path=cookie_file,
    )

    options = _metadata_options(
        settings,
        {"debug_code": "default", "options": {}},
        "https://www.youtube.com/watch?v=abcdef",
    )
    cookie_buffer = options["cookiefile"]
    with downloader.YoutubeDL(options) as ydl:
        assert len(ydl.cookiejar) == 1

    assert cookie_file.read_text() == original
    assert cookie_buffer.getvalue().startswith("# Netscape HTTP Cookie File")


def test_malformed_or_oversized_cookie_file_fails_without_leaking_contents(
    caplog,
    capsys,
    tmp_path,
):
    sentinel = "COOKIE_VALUE_MUST_NEVER_APPEAR"
    cookie_file = tmp_path / "youtube-cookies.txt"
    cookie_file.write_text(f"# Netscape HTTP Cookie File\nmalformed-{sentinel}\n")
    settings = make_settings(
        tmp_path,
        youtube_cookies_enabled=True,
        youtube_cookies_path=cookie_file,
    )

    with pytest.raises(UserFacingError, match="rechazó"):
        _metadata_options(
            settings,
            {"debug_code": "default", "options": {}},
            "https://www.youtube.com/watch?v=abcdef",
        )
    captured = capsys.readouterr()
    assert sentinel not in captured.out + captured.err + caplog.text

    cookie_file.write_bytes(b"# Netscape HTTP Cookie File\n" + b"x" * MAX_COOKIE_FILE_BYTES)
    with pytest.raises(UserFacingError, match="rechazó"):
        _metadata_options(
            settings,
            {"debug_code": "default", "options": {}},
            "https://www.youtube.com/watch?v=abcdef",
        )


def test_metadata_timeout_is_reported_as_gateway_timeout(monkeypatch, tmp_path):
    class SlowYoutubeDL:
        def __init__(self, _options):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, _url, download=False):
            assert download is False
            return {"title": "too late", "extractor_key": "Youtube"}

    ticks = iter([0.0, 0.0, 31.0])
    monkeypatch.setattr(downloader, "YoutubeDL", SlowYoutubeDL)
    monkeypatch.setattr(downloader.time, "monotonic", lambda: next(ticks))

    with pytest.raises(UserFacingError) as exc_info:
        fetch_metadata("https://www.youtube.com/watch?v=abcdef", make_settings(tmp_path))
    assert exc_info.value.status_code == 504
    assert "tardó demasiado" in exc_info.value.message
