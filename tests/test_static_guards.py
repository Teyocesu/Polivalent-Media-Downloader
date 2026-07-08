from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def test_no_unsafe_shell_or_database_code():
    scanned = []
    for path in (ROOT / "backend").rglob("*.py"):
        text = path.read_text()
        scanned.append(path)
        assert "shell=True" not in text
        assert "subprocess" not in text
        assert "os.system" not in text
        if path.name != "youtube_auth.py":
            assert "cookiefile" not in text.lower()
            assert "cookiesfrombrowser" not in text.lower()
        assert "sqlite" not in text.lower()
        assert "sqlalchemy" not in text.lower()
    assert scanned


def test_gitignore_covers_sensitive_and_generated_paths():
    text = (ROOT / ".gitignore").read_text()
    for pattern in [
        ".env",
        "__pycache__/",
        ".venv/",
        "node_modules/",
        "dist/",
        "build/",
        "/tmp/",
        "downloads/",
        "media-downloads/",
        "cookies.txt",
        "youtube-cookies.txt",
        "*.cookies",
        "*cookies*.txt",
        ".DS_Store",
    ]:
        assert pattern in text


def test_dockerignore_covers_youtube_cookie_files():
    text = (ROOT / ".dockerignore").read_text()
    for pattern in ["cookies.txt", "youtube-cookies.txt", "*.cookies", "*cookies*.txt"]:
        assert pattern in text


def test_no_cookie_files_are_tracked():
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    tracked = result.stdout.splitlines()
    forbidden = [
        path
        for path in tracked
        if path.endswith(".cookies")
        or path.endswith("cookies.txt")
        or path.endswith("youtube-cookies.txt")
    ]
    assert forbidden == []


def test_dockerfile_supports_render_single_service_build():
    text = (ROOT / "Dockerfile").read_text()
    assert "ffmpeg" in text
    assert "nodejs" in text
    assert "npm ci" in text
    assert "npm run build" in text
    assert "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}" in text


def test_manifest_is_pwa_ready():
    text = (ROOT / "frontend/public/manifest.json").read_text()
    assert '"display": "standalone"' in text
    assert '"theme_color": "#111316"' in text
    assert '"/icon.svg"' in text
