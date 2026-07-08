import os
import shutil
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
TEST_DOWNLOAD_DIR = Path("/tmp/media-downloader-pytest")

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("APP_PASSWORD", "test-password")
os.environ.setdefault("APP_SECRET_KEY", "test-secret")
os.environ.setdefault("MAX_FILE_MB", "1")
os.environ.setdefault("DOWNLOAD_TTL_MINUTES", "1")
os.environ.setdefault("DOWNLOAD_TIMEOUT_SECONDS", "5")
os.environ.setdefault("ALLOWED_ORIGINS", "http://localhost:5173")
os.environ.setdefault("DOWNLOAD_BASE_DIR", str(TEST_DOWNLOAD_DIR))

from backend.app import main  # noqa: E402


@pytest.fixture(autouse=True)
def reset_runtime_state():
    shutil.rmtree(TEST_DOWNLOAD_DIR, ignore_errors=True)
    TEST_DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    main.job_manager._jobs.clear()
    main.login_rate_limiter._buckets.clear()
    yield
    for job in list(main.job_manager._jobs.values()):
        job.cancel_requested = True
    main.job_manager._jobs.clear()
    main.login_rate_limiter._buckets.clear()
    shutil.rmtree(TEST_DOWNLOAD_DIR, ignore_errors=True)


@pytest.fixture
def client():
    return TestClient(main.app)


@pytest.fixture
def token(client):
    response = client.post("/api/auth/login", json={"password": "test-password"})
    assert response.status_code == 200
    return response.json()["token"]
