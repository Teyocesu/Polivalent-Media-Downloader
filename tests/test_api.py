import time

from backend.app import jobs as jobs_module
from backend.app import main as main_module
from backend.app.downloader import DownloadCancelled, DownloadTimedOut, PLATFORM_FAILURE_MESSAGE
from backend.app.utils import UserFacingError


def auth_headers(token):
    return {"Authorization": f"Bearer {token}"}


def wait_for_job(client, token, job_id, expected_statuses, timeout=3):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        response = client.get(f"/api/jobs/{job_id}", headers=auth_headers(token))
        assert response.status_code == 200
        last = response.json()
        if last["status"] in expected_statuses:
            return last
        time.sleep(0.03)
    raise AssertionError(f"Job did not reach {expected_statuses}; last={last}")


def test_health_and_security_headers(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "status": "healthy"}
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"


def test_login_success_failure_and_rate_limit(client):
    ok = client.post("/api/auth/login", json={"password": "test-password"})
    assert ok.status_code == 200
    assert ok.json()["ok"] is True

    bad = client.post("/api/auth/login", json={"password": "wrong"})
    assert bad.status_code == 401

    for _ in range(7):
        client.post("/api/auth/login", json={"password": "wrong"})
    limited = client.post("/api/auth/login", json={"password": "wrong"})
    assert limited.status_code == 429


def test_protected_endpoints_reject_missing_or_invalid_token(client):
    missing = client.post("/api/info", json={"url": "https://youtu.be/dQw4w9WgXcQ"})
    assert missing.status_code == 401

    invalid = client.post(
        "/api/info",
        json={"url": "https://youtu.be/dQw4w9WgXcQ"},
        headers=auth_headers("bad-token"),
    )
    assert invalid.status_code == 401


def test_cors_preflight_for_allowed_origin(client):
    response = client.options(
        "/api/info",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"


def test_info_validates_urls_and_returns_mocked_metadata(client, token, monkeypatch):
    def fake_fetch_metadata(url, settings):
        return {
            "title": "Short public video",
            "site": "YouTube",
            "thumbnail": "https://example.test/thumb.jpg",
            "duration": 12,
            "uploader": "Demo",
            "availableQualities": ["best", "720", "mp3"],
        }

    monkeypatch.setattr(main_module, "fetch_metadata", fake_fetch_metadata)

    ok = client.post(
        "/api/info",
        json={"url": "https://youtu.be/dQw4w9WgXcQ"},
        headers=auth_headers(token),
    )
    assert ok.status_code == 200
    assert ok.json()["title"] == "Short public video"

    invalid_domain = client.post(
        "/api/info",
        json={"url": "https://example.com/video"},
        headers=auth_headers(token),
    )
    assert invalid_domain.status_code == 400

    invalid_url = client.post(
        "/api/info",
        json={"url": "notaurl"},
        headers=auth_headers(token),
    )
    assert invalid_url.status_code == 400

    playlist = client.post(
        "/api/info",
        json={"url": "https://www.youtube.com/watch?v=abc123&list=PL123"},
        headers=auth_headers(token),
    )
    assert playlist.status_code == 200

    pure_playlist = client.post(
        "/api/info",
        json={"url": "https://www.youtube.com/playlist?list=PL123"},
        headers=auth_headers(token),
    )
    assert pure_playlist.status_code == 400
    assert "playlist" in pure_playlist.json()["detail"].lower()


def test_info_reports_platform_failure_without_stack_trace(client, token, monkeypatch):
    def fake_fetch_metadata(url, settings):
        raise UserFacingError(PLATFORM_FAILURE_MESSAGE)

    monkeypatch.setattr(main_module, "fetch_metadata", fake_fetch_metadata)
    response = client.post(
        "/api/info",
        json={"url": "https://youtu.be/dQw4w9WgXcQ"},
        headers=auth_headers(token),
    )
    assert response.status_code == 400
    assert "actualizar yt-dlp" in response.json()["detail"]
    assert "Traceback" not in response.text


def test_download_invalid_quality_and_missing_token_are_rejected(client, token):
    invalid_quality = client.post(
        "/api/download",
        json={"url": "https://youtu.be/dQw4w9WgXcQ", "quality": "4k"},
        headers=auth_headers(token),
    )
    assert invalid_quality.status_code == 422

    missing_token = client.post(
        "/api/download",
        json={"url": "https://youtu.be/dQw4w9WgXcQ", "quality": "best"},
    )
    assert missing_token.status_code == 401


def test_download_reuses_metadata_validation_before_queueing(client, token, monkeypatch):
    def download_failure(url, quality, temp_dir, settings, progress_callback, check_cancelled):
        raise UserFacingError("Los vivos o streams no estan soportados en esta version.")

    monkeypatch.setattr(jobs_module, "fetch_metadata", lambda url, settings: {"title": "ok"})
    monkeypatch.setattr(jobs_module, "download_media", download_failure)
    response = client.post(
        "/api/download",
        json={"url": "https://youtu.be/dQw4w9WgXcQ", "quality": "best"},
        headers=auth_headers(token),
    )
    assert response.status_code == 200
    status = wait_for_job(client, token, response.json()["jobId"], {"error"})
    assert "streams" in status["message"]


def test_download_file_is_one_use_and_temp_dir_is_removed(client, token, monkeypatch):
    monkeypatch.setattr(
        jobs_module,
        "fetch_metadata",
        lambda url, settings: {"title": "ok", "availableQualities": ["best"]},
    )

    def fake_download_media(url, quality, temp_dir, settings, progress_callback, check_cancelled):
        progress_callback(
            {
                "status": "downloading",
                "phase": "downloading_video",
                "phaseLabel": "Descargando video",
                "progress": 42,
                "downloadPercent": 57.3,
                "speed": "2.4 MB/s",
                "eta": "00:18",
                "downloadedBytes": 12345678,
                "totalBytes": 98765432,
                "message": "Descargando video...",
                "step": 4,
                "stepsTotal": 5,
            }
        )
        output = temp_dir / "clip.mp4"
        output.write_bytes(b"video-bytes")
        return output, "clip.mp4"

    monkeypatch.setattr(jobs_module, "download_media", fake_download_media)

    created = client.post(
        "/api/download",
        json={"url": "https://youtu.be/dQw4w9WgXcQ", "quality": "best"},
        headers=auth_headers(token),
    )
    assert created.status_code == 200
    job_id = created.json()["jobId"]
    done = wait_for_job(client, token, job_id, {"done"})
    assert done["progress"] == 100
    assert done["phase"] == "done"
    assert done["filename"] == "clip.mp4"

    job_dir = main_module.job_manager._jobs[job_id].temp_dir
    first_file = client.get(f"/api/files/{job_id}", headers=auth_headers(token))
    assert first_file.status_code == 200
    assert first_file.content == b"video-bytes"
    assert "attachment" in first_file.headers["content-disposition"]
    assert not job_dir.exists()

    second_file = client.get(f"/api/files/{job_id}", headers=auth_headers(token))
    assert second_file.status_code == 410


def test_delete_job_cancels_and_removes_temp_dir(client, token, monkeypatch):
    monkeypatch.setattr(
        jobs_module,
        "fetch_metadata",
        lambda url, settings: {"title": "ok", "availableQualities": ["720"]},
    )

    def slow_download_media(url, quality, temp_dir, settings, progress_callback, check_cancelled):
        (temp_dir / "partial.part").write_bytes(b"partial")
        progress_callback(
            {
                "status": "downloading",
                "phase": "downloading_video",
                "progress": 10,
                "message": "Descargando video...",
            }
        )
        while not check_cancelled():
            time.sleep(0.01)
        raise DownloadCancelled()

    monkeypatch.setattr(jobs_module, "download_media", slow_download_media)
    created = client.post(
        "/api/download",
        json={"url": "https://youtu.be/dQw4w9WgXcQ", "quality": "720"},
        headers=auth_headers(token),
    )
    assert created.status_code == 200
    job_id = created.json()["jobId"]
    job = main_module.job_manager._jobs[job_id]

    deleted = client.delete(f"/api/jobs/{job_id}", headers=auth_headers(token))
    assert deleted.status_code == 200
    job.future.result(timeout=2)
    assert job.status == "expired"
    assert not job.temp_dir.exists()


def test_download_timeout_becomes_safe_error(client, token, monkeypatch):
    monkeypatch.setattr(
        jobs_module,
        "fetch_metadata",
        lambda url, settings: {"title": "ok", "availableQualities": ["best"]},
    )

    def timeout_download_media(url, quality, temp_dir, settings, progress_callback, check_cancelled):
        raise DownloadTimedOut()

    monkeypatch.setattr(jobs_module, "download_media", timeout_download_media)
    created = client.post(
        "/api/download",
        json={"url": "https://youtu.be/dQw4w9WgXcQ", "quality": "best"},
        headers=auth_headers(token),
    )
    assert created.status_code == 200
    status = wait_for_job(client, token, created.json()["jobId"], {"error"})
    assert "tardó demasiado" in status["message"]


def test_debug_system_requires_no_secret_and_reports_runtime(client):
    response = client.get("/api/debug/system")
    assert response.status_code == 200
    payload = response.json()
    assert "ytDlpVersion" in payload
    assert "ffmpegAvailable" in payload
    assert "ffprobeAvailable" in payload
    assert "nodeAvailable" in payload
    assert "denoAvailable" in payload
    assert "APP_PASSWORD" not in response.text
    assert "test-password" not in response.text
