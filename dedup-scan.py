#!/usr/bin/env python3
"""Scan media directories for duplicate movies (same title+year, multiple copies)."""
import json
import os
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
CONFIG_FILE = DATA_DIR / "trimbin_config.json"
OUTPUT_FILE = DATA_DIR / "dedup_scan.json"
DEDUP_IGNORE_FILE = DATA_DIR / "dedup_ignore.json"

SEASON_RE = re.compile(r'\bS(?:eason\s*)?\d+\b', re.IGNORECASE)


def load_config():
    try:
        return json.loads(CONFIG_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def get_media_roots():
    cfg = load_config()
    paths = cfg.get("MEDIA_LIBRARIES", "")
    if not paths:
        return []
    return [p.strip() for p in paths.split(",") if p.strip()]


def parse_movie_dir(dirname):
    m = re.match(r'^(.+?)\s*\((\d{4})\)', dirname)
    if m:
        return m.group(1).strip(), int(m.group(2))
    m = re.match(r'^(.+?)[.\s](\d{4})[.\s\[\(]', dirname)
    if m:
        title = m.group(1).replace('.', ' ').strip()
        return title, int(m.group(2))
    return None, None


def is_season_dir(dirname):
    return bool(SEASON_RE.search(dirname))


def normalize_title(title):
    t = title.lower().strip()
    t = re.sub(r'[^a-z0-9\s]', '', t)
    t = re.sub(r'\s+', ' ', t).strip()
    return t


def dir_size_bytes(path):
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


MEDIA_EXTS = {'.mkv', '.mp4', '.avi', '.m2ts', '.ts', '.wmv', '.mov', '.flv', '.mpg', '.mpeg'}
PARTIAL_EXTS = {'.part', '.!qb', '.aria2'}


def inspect_media_dir(path):
    """Return dict with video_files, total_files, partial_files, has_media, largest_video_bytes."""
    video_files = 0
    total_files = 0
    partial_files = []
    largest_video = 0
    has_bdmv = False
    try:
        for dirpath, dirnames, filenames in os.walk(path):
            for f in filenames:
                total_files += 1
                ext = os.path.splitext(f)[1].lower()
                if ext in MEDIA_EXTS:
                    video_files += 1
                    try:
                        sz = os.path.getsize(os.path.join(dirpath, f))
                        if sz > largest_video:
                            largest_video = sz
                    except OSError:
                        pass
                elif ext in PARTIAL_EXTS:
                    partial_files.append(f)
            if "BDMV" in dirnames:
                has_bdmv = True
    except OSError:
        pass
    return {
        "video_files": video_files,
        "total_files": total_files,
        "partial_files": partial_files,
        "has_media": video_files > 0 or has_bdmv,
        "largest_video_bytes": largest_video,
    }


def has_media_files(path):
    return inspect_media_dir(path)["has_media"]


def detect_quality(dirname):
    d = dirname.upper()
    if "2160P" in d or "4K" in d or "UHD" in d:
        return "4K"
    if "1080P" in d:
        return "1080p"
    if "720P" in d:
        return "720p"
    if "480P" in d or "DVDRIP" in d:
        return "480p"
    return "Unknown"


def detect_codec(dirname):
    d = re.sub(r'[._\-]', ' ', dirname.upper())
    if re.search(r'\bAV1\b', d):
        return "AV1"
    if re.search(r'\b(?:X265|H\.?265|HEVC)\b', d):
        return "HEVC"
    if re.search(r'\b(?:X264|H\.?264|AVC)\b', d):
        return "H.264"
    if re.search(r'\bVC[\s\-]?1\b', d):
        return "VC-1"
    return None


def detect_source(dirname):
    d = dirname.upper()
    if "REMUX" in d or "BDREMUX" in d:
        return "Remux"
    if "BLURAY" in d or "BDRIP" in d:
        return "BluRay"
    if re.search(r'\bWEB[\s\-\.]?DL\b', d):
        return "WEB-DL"
    if re.search(r'\bWEB[\s\-\.]?RIP\b', d):
        return "WEBRip"
    if "HDTV" in d:
        return "HDTV"
    if "DVDRIP" in d or "DVD" in d:
        return "DVD"
    return None


def detect_hdr(dirname):
    d = dirname.upper()
    if "HDR10+" in d:
        return "HDR10+"
    if re.search(r'\bDOLBY\s*VISION\b|\bDV\b|\bDOVI\b', d):
        if "HDR" in d:
            return "DV+HDR"
        return "DV"
    if "HDR" in d:
        return "HDR"
    return None


def scan():
    media_roots = get_media_roots()
    if not media_roots:
        print("No media libraries configured. Add them in Settings.")
        OUTPUT_FILE.write_text(json.dumps({
            "total_groups": 0, "total_waste_gb": 0, "duplicates": [],
            "last_scan": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        }))
        return

    groups = defaultdict(list)

    for root in media_roots:
        if not os.path.isdir(root):
            continue
        library = os.path.basename(root)
        for entry in sorted(os.listdir(root)):
            full_path = os.path.join(root, entry)
            if not os.path.isdir(full_path):
                continue
            if entry.startswith('.') or '.trickplay' in entry.lower():
                continue
            if is_season_dir(entry):
                continue

            title, year = parse_movie_dir(entry)
            if not title or not year:
                continue

            norm = normalize_title(title)
            size_bytes = dir_size_bytes(full_path)
            size_gb = round(size_bytes / (1024**3), 1)

            quality = detect_quality(entry)
            codec = detect_codec(entry)
            source = detect_source(entry)
            hdr = detect_hdr(entry)
            is_bdmv = os.path.isdir(os.path.join(full_path, "BDMV"))

            info = inspect_media_dir(full_path)

            label_parts = [quality]
            if hdr:
                label_parts.append(hdr)
            if codec:
                label_parts.append(codec)
            if source:
                label_parts.append(source)
            if is_bdmv:
                label_parts.append("BDMV")

            status = "complete"
            if info["partial_files"]:
                status = "partial"
            elif not info["has_media"]:
                status = "no_media"

            groups[(norm, year)].append({
                "dirname": entry,
                "path": full_path,
                "title": title,
                "year": year,
                "size_gb": size_gb,
                "quality": quality,
                "codec": codec,
                "source": source,
                "hdr": hdr,
                "is_bdmv": is_bdmv,
                "has_media": info["has_media"],
                "video_files": info["video_files"],
                "total_files": info["total_files"],
                "partial_files": len(info["partial_files"]),
                "largest_video_bytes": info["largest_video_bytes"],
                "status": status,
                "label": " · ".join(label_parts),
                "library": library,
            })

    ignored = set()
    try:
        ignored = set(json.loads(DEDUP_IGNORE_FILE.read_text()))
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    duplicates = []
    total_waste_gb = 0
    for (norm, year), entries in sorted(groups.items()):
        if len(entries) < 2:
            continue
        group_key = f"{norm}|{year}"
        if group_key in ignored:
            continue
        entries.sort(key=lambda x: x["size_gb"], reverse=True)
        waste = sum(e["size_gb"] for e in entries[1:])
        total_waste_gb += waste
        duplicates.append({
            "key": group_key,
            "title": entries[0]["title"],
            "year": year,
            "copies": len(entries),
            "total_gb": round(sum(e["size_gb"] for e in entries), 1),
            "waste_gb": round(waste, 1),
            "entries": entries,
        })

    duplicates.sort(key=lambda x: x["waste_gb"], reverse=True)

    result = {
        "total_groups": len(duplicates),
        "total_waste_gb": round(total_waste_gb, 1),
        "duplicates": duplicates,
        "last_scan": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }

    OUTPUT_FILE.write_text(json.dumps(result, indent=2))
    print(f"Found {len(duplicates)} duplicate groups, {total_waste_gb:.1f} GB potential waste")


if __name__ == "__main__":
    scan()
