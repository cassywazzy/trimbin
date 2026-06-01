"""Shared helpers for the Trimbin scanners — stdlib only, no dependencies.

Single source of truth for the file-type extension sets, config loading
(config file first, then environment), recursive directory sizing, and
crash-safe JSON writes. Imported by the scanner scripts so these don't drift
out of sync across files.
"""
import json
import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
CONFIG_FILE = DATA_DIR / "trimbin_config.json"

# Canonical extension sets (unify what had drifted between scanners).
VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".wmv", ".flv", ".mov", ".m4v", ".webm",
              ".ts", ".mpg", ".mpeg", ".m2ts", ".iso", ".bdmv", ".vob"}
AUDIO_EXTS = {".mp3", ".flac", ".ogg", ".opus", ".m4a", ".aac", ".wav", ".wma",
              ".alac", ".ape", ".dsd", ".dsf", ".mka"}
SUB_EXTS = {".srt", ".ass", ".ssa", ".sub", ".idx", ".sup", ".vtt", ".pgs"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff", ".tbn"}
META_EXTS = {".nfo", ".xml", ".json", ".txt", ".url", ".website", ".lnk", ".log"}


def load_config():
    """The UI-editable config dict; empty dict on a missing or corrupt file."""
    try:
        return json.loads(CONFIG_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def get_media_roots():
    """Configured library paths: config file first, then env (so env-only setups work)."""
    cfg = load_config()
    raw = cfg.get("MEDIA_LIBRARIES") or os.environ.get("MEDIA_LIBRARIES", "")
    return [p.strip() for p in raw.split(",") if p.strip()]


def dir_size(path):
    """Total byte size of every file under `path` (best-effort, never raises)."""
    total = 0
    try:
        for dirpath, _, filenames in os.walk(path):
            for f in filenames:
                try:
                    total += os.path.getsize(os.path.join(dirpath, f))
                except OSError:
                    pass
    except OSError:
        pass
    return total


def write_json_atomic(path, data, indent=2):
    """Crash-safe JSON write: serialize to a sibling .tmp, then atomically replace."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=indent))
    os.replace(tmp, path)


# --- Live scan progress (polled by the web UI so long scans aren't a dead spinner) ---
import time as _time

PROGRESS_FILE = DATA_DIR / "scan_progress.json"
_last_progress_write = [0.0]


def progress(scan_type, done=0, total=0, current="", phase="scanning", force=False):
    """Write a throttled progress record. Best-effort and never raises — a
    progress write must never break a scan. Throttled to ~2.5 writes/sec."""
    now = _time.time()
    if not force and now - _last_progress_write[0] < 0.4:
        return
    _last_progress_write[0] = now
    try:
        write_json_atomic(PROGRESS_FILE, {
            "active": True, "type": scan_type, "phase": phase,
            "done": done, "total": total, "current": current, "updated": now,
        })
    except Exception:
        pass


def progress_done(scan_type):
    """Mark the scan finished so the UI stops polling."""
    try:
        write_json_atomic(PROGRESS_FILE, {"active": False, "type": scan_type, "updated": _time.time()})
    except Exception:
        pass
