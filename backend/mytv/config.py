"""Environment configuration. No absolute paths: everything derives from the project root."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load_env() -> None:
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


_load_env()


def _path(name: str, default: str) -> Path:
    p = Path(os.environ.get(name) or default).expanduser()
    return p if p.is_absolute() else ROOT / p


def _tool(env: str, name: str) -> str:
    explicit = os.environ.get(env)
    if explicit:
        return explicit
    return (
        shutil.which(name)
        or next((p for p in (f"/opt/homebrew/bin/{name}", f"/usr/local/bin/{name}") if os.path.exists(p)), name)
    )


HOST = os.environ.get("MYTV_HOST") or "0.0.0.0"
PORT = int(os.environ.get("MYTV_PORT") or 8400)
PUBLIC_URL = (os.environ.get("MYTV_PUBLIC_URL") or "").rstrip("/")
ADMIN_FROM_LAN = (os.environ.get("MYTV_ADMIN_FROM_LAN") or "true").lower() != "false"
TMDB_API_KEY = os.environ.get("TMDB_API_KEY") or ""

DATA_DIR = _path("MYTV_DATA_DIR", "data")
LIBRARY_DIR = _path("MYTV_LIBRARY_DIR", "library")
CACHE_DIR = DATA_DIR / "cache"  # ready-to-air HLS segments, one folder per title
DOWNLOAD_DIR = DATA_DIR / "downloads"
POSTER_DIR = DATA_DIR / "posters"
LOG_DIR = DATA_DIR / "logs"
DB_PATH = DATA_DIR / "mytv.db"
FRONTEND_DIST = ROOT / "frontend" / "dist"

FFMPEG = _tool("FFMPEG_PATH", "ffmpeg")
FFPROBE = _tool("FFPROBE_PATH", "ffprobe")
USER_AGENT = "MyTV/1.0 (personal home media app)"

CATEGORIES = {
    "classic-movies": "Classic Movies",
    "comedy-movies": "Comedy Movies",
    "sitcoms": "Sitcoms",
    "mystery-thrillers": "Mystery & Thrillers",
}


def ensure_dirs() -> None:
    for d in (DATA_DIR, CACHE_DIR, DOWNLOAD_DIR, POSTER_DIR, LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)
    for c in CATEGORIES:
        (LIBRARY_DIR / c).mkdir(parents=True, exist_ok=True)
