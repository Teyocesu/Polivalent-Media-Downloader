import base64
import json
import logging
import stat
import subprocess
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.app import jobs as jobs_module
from backend.app import main as main_module
from backend.app.config import Settings
from backend.app.jobs import JobManager
from backend.app.rate_limit import FixedWindowRateLimiter
from backend.app.security import (
    _sign,
    create_file_ticket,
    create_token,
    verify_file_ticket,
    verify_token,
)
from backend.app.utils import UserFacingError, normalize_url, safe_remove_tree, validate_media_url


def make_settings(tmp_path):
    return Settings(
        app_password="test-password",
        token_secret="test-secret",
        max_file_mb=10,
        download_ttl_minutes=1,
        download_timeout_seconds=5,
        session_ttl_seconds=60,
        allowed_origins=["http://localhost:5173"],
        allowed_domains={
            "youtube.com",
            "www.youtube.com",
            "m.youtube.com",
            "music.youtube.com",
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


def auth_headers(token):
    return {"Authorization": f"Bearer {token}"}


def wait_for_job(client, token, job_id, expected_statuses, timeout=3):
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        response = client.get(f"/api/jobs/{job_id}", headers=auth_headers(token))
        assert response.status_code == 200
        if response.json()["status"] in expected_statuses:
            return response.json()
        time.sleep(0.02)
    raise AssertionError(f"Job did not reach {expected_statuses}")


def test_ssrf_boundary_rejects_nonpublic_and_hostile_urls(tmp_path):
    settings = make_settings(tmp_path)
    settings = replace(
        settings,
        allowed_domains=settings.allowed_domains
        | {"127.0.0.1", "10.0.0.1", "169.254.169.254", "::1"},
    )

    blocked = [
        "http://127.0.0.1/video",
        "http://10.0.0.1/video",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/video",
        "http://www.youtube.com/watch?v=abc123",
        "https://user:password@www.youtube.com/watch?v=abc123",
        "https://www.youtube.com:444/watch?v=abc123",
        "https://www.youtube.com:80/watch?v=m4Be0hRQGIs",
        "https://www.youtube.com\\@127.0.0.1/video",
        "file:///etc/passwd",
        "ftp://www.youtube.com/video",
        "data:text/plain,secret",
        "javascript:alert(1)",
        "https://www.youtube.com.evil.test/video",
        "https://www.youtube.com/redirect?q=http://169.254.169.254/latest/meta-data",
        "https://www.instagram.com/accounts/login/?next=http://127.0.0.1/",
        "https://x.com/intent/retweet?url=http://127.0.0.1/",
        "https://www.tiktok.com/login?redirect_url=http://127.0.0.1/",
    ]
    for url in blocked:
        with pytest.raises(UserFacingError):
            validate_media_url(url, settings)

    assert (
        validate_media_url("https://www.tiktok.com/@demo/video/123456", settings).site
        == "TikTok"
    )
    assert validate_media_url("https://vm.tiktok.com/ZMdemo123/", settings).site == "TikTok"
    assert validate_media_url("https://www.instagram.com/p/AbC_123/", settings).site == "Instagram"
    assert validate_media_url("https://x.com/demo/status/123/video/1", settings).site == "X / Twitter"
    assert validate_media_url(
        "https://www.instagram.com/p/AbC_123/?utm_source=share#fragment",
        settings,
    ).url == "https://www.instagram.com/p/AbC_123/"
    assert validate_media_url(
        "https://x.com/demo/status/123?ref_src=twsrc%5Etfw",
        settings,
    ).url == "https://x.com/demo/status/123"


def test_music_youtube_normalizes_to_single_video(tmp_path):
    settings = make_settings(tmp_path)
    url = "https://music.youtube.com/watch?v=m4Be0hRQGIs&list=RDAMVMm4Be0hRQGIs&index=2"
    validated = validate_media_url(url, settings)
    assert validated.url == "https://www.youtube.com/watch?v=m4Be0hRQGIs"
    assert validated.site == "YouTube"
    assert normalize_url(url) == validated.url


def test_malformed_signed_tokens_fail_closed(tmp_path):
    settings = make_settings(tmp_path)
    valid = create_token(settings)
    assert verify_token(valid, settings) is True
    assert verify_token("x" * 5000, settings) is False

    invalid_base64 = "%%%%"
    assert verify_token(f"{invalid_base64}.{_sign(invalid_base64, settings)}", settings) is False

    list_body = base64.urlsafe_b64encode(json.dumps([]).encode()).rstrip(b"=").decode()
    assert verify_token(f"{list_body}.{_sign(list_body, settings)}", settings) is False


def test_file_tickets_are_job_bound_short_lived_and_not_session_tokens(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    clock = {"now": 1000.0}
    monkeypatch.setattr("backend.app.security.time.time", lambda: clock["now"])
    ticket = create_file_ticket("job-one", settings, ttl_seconds=10)

    assert verify_file_ticket(ticket, "job-one", settings) is True
    assert verify_file_ticket(ticket, "job-two", settings) is False
    assert verify_token(ticket, settings) is False
    assert verify_file_ticket(create_token(settings), "job-one", settings) is False

    clock["now"] = 1011.0
    assert verify_file_ticket(ticket, "job-one", settings) is False


def test_rate_limiter_bounds_memory_and_reports_retry_after(monkeypatch):
    clock = {"now": 100.0}
    monkeypatch.setattr("backend.app.rate_limit.time.time", lambda: clock["now"])
    limiter = FixedWindowRateLimiter(max_failures=2, window_seconds=30, max_buckets=2)

    limiter.record_failure("one")
    limiter.record_failure("two")
    limiter.record_failure("three")
    assert len(limiter._buckets) == 2

    limiter.record_failure("three")
    assert limiter.is_limited("three") is True
    assert limiter.retry_after("three") == 30
    clock["now"] = 131.0
    assert limiter.is_limited("three") is False
    assert limiter.retry_after("three") == 0


def test_node_runtime_status_is_sanitized_and_requires_major_22(monkeypatch):
    monkeypatch.setattr(
        main_module.shutil,
        "which",
        lambda name: "/safe/node" if name == "node" else None,
    )
    try:
        monkeypatch.setattr(
            main_module.subprocess,
            "run",
            lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="v24.4.1\n"),
        )
        main_module._node_runtime_status.cache_clear()
        assert main_module._node_runtime_status() == (True, "24.4.1", True)

        monkeypatch.setattr(
            main_module.subprocess,
            "run",
            lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="v20.19.0\n"),
        )
        main_module._node_runtime_status.cache_clear()
        assert main_module._node_runtime_status() == (True, "20.19.0", False)

        monkeypatch.setattr(main_module.shutil, "which", lambda _name: None)
        main_module._node_runtime_status.cache_clear()
        assert main_module._node_runtime_status() == (False, None, False)

        monkeypatch.setattr(main_module.shutil, "which", lambda name: "/safe/node")

        def timeout(*_args, **_kwargs):
            raise subprocess.TimeoutExpired("node", 2, stderr="must-not-leak")

        monkeypatch.setattr(main_module.subprocess, "run", timeout)
        main_module._node_runtime_status.cache_clear()
        assert main_module._node_runtime_status() == (True, None, False)
    finally:
        main_module._node_runtime_status.cache_clear()


def test_job_directories_are_unique_and_private(tmp_path, monkeypatch):
    manager = JobManager(make_settings(tmp_path))
    monkeypatch.setattr(manager, "_run_job", lambda _job: None)
    try:
        first = manager.create_job("https://youtu.be/abc123", "720", "YouTube")
        second = manager.create_job("https://youtu.be/def456", "720", "YouTube")
        assert first.temp_dir != second.temp_dir
        assert stat.S_IMODE(first.temp_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE(second.temp_dir.stat().st_mode) == 0o700
    finally:
        manager.shutdown()


def test_job_submit_failure_removes_unique_temp_dir(tmp_path, monkeypatch):
    manager = JobManager(make_settings(tmp_path))

    def fail_submit(*_args, **_kwargs):
        raise RuntimeError("executor stopped")

    monkeypatch.setattr(manager._executor, "submit", fail_submit)
    try:
        with pytest.raises(RuntimeError, match="executor stopped"):
            manager.create_job("https://youtu.be/abc123", "720", "YouTube")
        assert list(manager.settings.download_base_dir.iterdir()) == []
        assert manager._jobs == {}
    finally:
        manager.shutdown()


def test_job_queue_is_bounded_before_creating_more_temp_directories(tmp_path, monkeypatch):
    manager = JobManager(make_settings(tmp_path))
    monkeypatch.setattr(manager, "_run_job", lambda _job: None)
    try:
        for index in range(3):
            manager.create_job(
                f"https://youtu.be/m4Be0hRQG{index:02d}",
                "720",
                "YouTube",
            )
        with pytest.raises(UserFacingError) as exc_info:
            manager.create_job(
                "https://youtu.be/m4Be0hRQG99",
                "720",
                "YouTube",
            )
        assert exc_info.value.status_code == 429
        assert len(manager._jobs) == 3
        assert len(list(manager.settings.download_base_dir.iterdir())) == 3
    finally:
        manager.shutdown()


def test_job_rejects_output_outside_its_temp_directory(client, token, monkeypatch):
    monkeypatch.setattr(
        jobs_module,
        "fetch_metadata",
        lambda url, settings: {"title": "ok", "availableQualities": ["720"]},
    )

    def unsafe_download(url, quality, temp_dir, settings, progress_callback, check_cancelled):
        outside = temp_dir.parent / "outside.mp4"
        outside.write_bytes(b"must-not-be-served")
        return outside, "../../outside.mp4"

    monkeypatch.setattr(jobs_module, "download_media", unsafe_download)
    created = client.post(
        "/api/download",
        json={"url": "https://youtu.be/dQw4w9WgXcQ", "quality": "720"},
        headers=auth_headers(token),
    )
    job_id = created.json()["jobId"]
    status = wait_for_job(client, token, job_id, {"error"})
    job = main_module.job_manager._jobs[job_id]
    assert status["message"] == "Ocurrio un error interno durante la descarga."
    assert job.url == ""
    assert job.original_url is None
    assert not job.temp_dir.exists()


def test_cookie_ticket_streams_one_use_file_without_exposing_token(client, token, monkeypatch):
    monkeypatch.setattr(
        jobs_module,
        "fetch_metadata",
        lambda url, settings: {"title": "ok", "availableQualities": ["720"]},
    )

    def fake_download(url, quality, temp_dir, settings, progress_callback, check_cancelled):
        output = temp_dir / "mobile-clip.mp4"
        output.write_bytes(b"streamed-video")
        return output, "mobile-clip.mp4"

    monkeypatch.setattr(jobs_module, "download_media", fake_download)
    created = client.post(
        "/api/download",
        json={"url": "https://youtu.be/dQw4w9WgXcQ", "quality": "720"},
        headers=auth_headers(token),
    )
    job_id = created.json()["jobId"]
    wait_for_job(client, token, job_id, {"done"})
    authorize_path = f"/api/files/{job_id}/authorize"

    missing_auth = client.post(authorize_path)
    assert missing_auth.status_code == 401
    missing_ticket = client.get(f"/api/files/{job_id}")
    assert missing_ticket.status_code == 401

    wrong_job_ticket = client.get(
        f"/api/files/{job_id}",
        headers={
            "Cookie": (
                "pmd_file_ticket="
                + create_file_ticket("different-job", main_module.settings)
            )
        },
    )
    assert wrong_job_ticket.status_code == 401

    authorized = client.post(authorize_path, headers=auth_headers(token))
    assert authorized.status_code == 200
    payload = authorized.json()
    assert payload == {"ok": True, "downloadUrl": f"/api/files/{job_id}"}
    assert "?" not in payload["downloadUrl"]
    assert token not in authorized.text
    cookie_header = authorized.headers["set-cookie"]
    assert "pmd_file_ticket=" in cookie_header
    assert "HttpOnly" in cookie_header
    assert "SameSite=strict" in cookie_header
    assert f"Path=/api/files/{job_id}" in cookie_header

    downloaded = client.get(payload["downloadUrl"])
    assert downloaded.status_code == 200
    assert downloaded.content == b"streamed-video"
    assert "attachment" in downloaded.headers["content-disposition"]
    assert "pmd_file_ticket=\"\"" in downloaded.headers["set-cookie"]
    assert "Max-Age=0" in downloaded.headers["set-cookie"]
    delivered_job = main_module.job_manager._jobs[job_id]
    assert not delivered_job.temp_dir.exists()
    assert delivered_job.url == ""
    assert delivered_job.original_url is None
    assert delivered_job.file_path is None
    assert delivered_job.filename is None
    assert delivered_job.current_file is None

    second = client.get(payload["downloadUrl"], headers=auth_headers(token))
    assert second.status_code == 410


def test_unexpected_api_error_hides_message_and_traceback(token, monkeypatch, caplog):
    secret = "APP_SECRET_KEY=do-not-leak"

    def fail_metadata(url, settings):
        raise RuntimeError(secret)

    monkeypatch.setattr(main_module, "fetch_metadata", fail_metadata)
    caplog.set_level(logging.ERROR)
    client = TestClient(main_module.app, raise_server_exceptions=False)
    response = client.post(
        "/api/info",
        json={"url": "https://youtu.be/dQw4w9WgXcQ"},
        headers=auth_headers(token),
    )
    assert response.status_code == 500
    assert response.json() == {"detail": "Ocurrio un error interno."}
    assert secret not in response.text
    assert "Traceback" not in response.text
    assert secret not in caplog.text


def test_safe_remove_tree_unlinks_symlink_without_following_it(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    protected_file = target / "keep.txt"
    protected_file.write_text("keep")
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)

    safe_remove_tree(link)
    assert not link.exists()
    assert protected_file.read_text() == "keep"
