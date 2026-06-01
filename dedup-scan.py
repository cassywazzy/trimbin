#!/usr/bin/env python3
"""Scan media directories for duplicate movies and TV/anime series.

Movies: grouped by title+year, cross-referenced with Radarr to catch
cross-language/cross-title duplicates (e.g. 'All About My Mother' vs
'Todo sobre mi madre') by TMDB ID.

Series: top-level show folders that contain episode files are grouped across
ALL libraries (e.g. the same show living in both /tv and /anime) via Sonarr
TVDB IDs, falling back to a cleaned-title key for series Sonarr doesn't track.
"""
import json
import os
import re
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import trimbin_common as tc  # for live scan-progress reporting

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


# Release-junk tokens stripped when deriving a series name from a folder name.
RELEASE_TOKEN_RE = re.compile(
    r'\b(1080p|720p|2160p|480p|4k|uhd|x264|x265|h\.?264|h\.?265|hevc|avc|av1|'
    r'blu[\s\-]?ray|bdrip|bd|brrip|web[\s\-]?dl|webrip|hdtv|dvdrip|dvd|remux|'
    r'dual[\s\-]?audio|multi[\s\-]?audio|flac|aac|ac3|eac3|ddp?\d?|truehd|atmos|'
    r'dts(?:[\s\-]?hd)?|opus|hi10p?|10bit|8bit|hdr10?\+?|dv|dovi|'
    r'complete|batch|season|specials?|ovas?|nced?|ncop?|extras?)\b',
    re.IGNORECASE,
)


def clean_series_title(dirname):
    """Derive a probable series title from a release folder name by stripping
    bracketed/parenthetical tags and release-metadata tokens."""
    t = re.sub(r'\[[^\]]*\]', ' ', dirname)        # [ShadyCrab 1080p ...]
    t = re.sub(r'\([^)]*\)', ' ', t)               # (2009), (BD) ...
    t = re.split(r'\s+\+\s+', t)[0]                # drop "+ OVAs + Specials" tails
    t = RELEASE_TOKEN_RE.sub(' ', t)
    t = re.sub(r'\s+', ' ', t).strip(' -._')
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


def detect_audio(dirname):
    """Best-effort audio-format detection from a release name. Helps distinguish
    duplicate copies that differ mainly by audio (e.g. FLAC 5.1 vs AAC stereo)."""
    d = dirname.upper()
    if "TRUEHD" in d or "ATMOS" in d:
        return "TrueHD"
    if "DTS-HD" in d or "DTSHD" in d or "DTS HD" in d:
        return "DTS-HD"
    if re.search(r'\bDTS\b', d):
        return "DTS"
    if "FLAC" in d:
        return "FLAC"
    if re.search(r'\b(?:DDP|DD\+|EAC3|E-AC-3)\b', d):
        return "DD+"
    if re.search(r'\b(?:AC3|DD5|DD2|DOLBY\s*DIGITAL)\b', d):
        return "AC3"
    if "AAC" in d:
        return "AAC"
    if "OPUS" in d:
        return "Opus"
    return None


EPISODE_RE = re.compile(r'[- .]S\d{1,2}E\d{1,3}[- .]', re.IGNORECASE)


def parse_video_filename(filename):
    """Extract title and year from a video filename (without extension).
    Returns (None, None) for TV episode files."""
    name = os.path.splitext(filename)[0]
    name = re.sub(r'\.trickplay$', '', name, flags=re.IGNORECASE)
    if EPISODE_RE.search(name):
        return None, None
    m = re.match(r'^(.+?)\s*\((\d{4})\)', name)
    if m:
        return m.group(1).strip(), int(m.group(2))
    m = re.match(r'^(.+?)[.\s](\d{4})[.\s\[\(]', name)
    if m:
        title = m.group(1).replace('.', ' ').strip()
        return title, int(m.group(2))
    return None, None


def dir_has_episodes(path):
    """True if any video file under `path` looks like a TV/anime episode."""
    try:
        for dirpath, _, filenames in os.walk(path):
            for f in filenames:
                if os.path.splitext(f)[1].lower() in MEDIA_EXTS and EPISODE_RE.search(f):
                    return True
    except OSError:
        pass
    return False


def _fetch_radarr_movies():
    """Fetch all movies from Radarr for TMDB-based dedup. Returns [] on failure."""
    try:
        config = load_config()
        url = config.get("RADARR_URL", "").rstrip("/")
        key = config.get("RADARR_API_KEY", "")
        if not url or not key:
            return []
        req = urllib.request.Request(f"{url}/api/v3/movie")
        req.add_header("X-Api-Key", key)
        req.add_header("Content-Type", "application/json")
        resp = urllib.request.urlopen(req, timeout=30)
        if resp.status == 200:
            return json.loads(resp.read())
    except Exception as e:
        print(f"Radarr cross-reference unavailable: {e}")
    return []


def _fetch_sonarr_series():
    """Fetch all series from Sonarr for TVDB-based dedup. Returns [] on failure."""
    try:
        config = load_config()
        url = config.get("SONARR_URL", "").rstrip("/")
        key = config.get("SONARR_API_KEY", "")
        if not url or not key:
            return []
        req = urllib.request.Request(f"{url}/api/v3/series")
        req.add_header("X-Api-Key", key)
        req.add_header("Content-Type", "application/json")
        resp = urllib.request.urlopen(req, timeout=30)
        if resp.status == 200:
            return json.loads(resp.read())
    except Exception as e:
        print(f"Sonarr cross-reference unavailable: {e}")
    return []


def scan_collection_folder(full_path, library, groups):
    """Scan a multi-movie collection folder for individual movies."""
    dirname = os.path.basename(full_path)
    found = 0
    try:
        for f in os.listdir(full_path):
            fp = os.path.join(full_path, f)
            ext = os.path.splitext(f)[1].lower()
            if ext not in MEDIA_EXTS:
                continue
            title, year = parse_video_filename(f)
            if not title or not year:
                continue
            norm = normalize_title(title)
            try:
                size_bytes = os.path.getsize(fp)
            except OSError:
                size_bytes = 0
            size_gb = round(size_bytes / (1024**3), 1)
            quality = detect_quality(f)
            codec = detect_codec(f)
            source = detect_source(f)
            hdr = detect_hdr(f)
            label_parts = [quality]
            if hdr:
                label_parts.append(hdr)
            if codec:
                label_parts.append(codec)
            if source:
                label_parts.append(source)
            groups[(norm, year)].append({
                "dirname": f,
                "path": fp,
                "title": title,
                "year": year,
                "size_gb": size_gb,
                "quality": quality,
                "codec": codec,
                "source": source,
                "hdr": hdr,
                "is_bdmv": False,
                "has_media": True,
                "video_files": 1,
                "total_files": 1,
                "partial_files": 0,
                "largest_video_bytes": size_bytes,
                "status": "complete",
                "label": " · ".join(label_parts),
                "library": library,
                "collection": dirname,
            })
            found += 1
    except OSError:
        pass
    return found


def make_series_entry(full_path, entry, library):
    """Build a dedup entry for a top-level TV/anime series folder."""
    info = inspect_media_dir(full_path)
    size_gb = round(dir_size_bytes(full_path) / (1024**3), 1)
    quality = detect_quality(entry)
    codec = detect_codec(entry)
    source = detect_source(entry)
    hdr = detect_hdr(entry)
    audio = detect_audio(entry)
    clean = clean_series_title(entry)
    label_parts = [quality]
    for part in (hdr, codec, audio, source):
        if part:
            label_parts.append(part)
    if info["partial_files"]:
        status = "partial"
    elif not info["has_media"]:
        status = "no_media"
    else:
        status = "complete"
    return {
        "dirname": entry,
        "path": full_path,
        "title": clean,
        "clean_norm": normalize_title(clean),
        "year": 0,
        "size_gb": size_gb,
        "quality": quality,
        "codec": codec,
        "source": source,
        "hdr": hdr,
        "audio": audio,
        "is_bdmv": False,
        "has_media": info["has_media"],
        "video_files": info["video_files"],
        "total_files": info["total_files"],
        "partial_files": len(info["partial_files"]),
        "largest_video_bytes": info["largest_video_bytes"],
        "status": status,
        "label": " · ".join(p for p in label_parts if p),
        "library": library,
        "is_series": True,
    }


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
    series_entries = []
    entries_seen = 0
    total_entries = 0
    for _r in media_roots:
        try:
            total_entries += len(os.listdir(_r))
        except OSError:
            pass

    for root in media_roots:
        if not os.path.isdir(root):
            continue
        library = os.path.basename(root)
        for entry in sorted(os.listdir(root)):
            entries_seen += 1
            tc.progress("dedup", done=entries_seen, total=total_entries,
                        current=os.path.join(root, entry))
            full_path = os.path.join(root, entry)
            if not os.path.isdir(full_path):
                continue
            if entry.startswith('.') or '.trickplay' in entry.lower():
                continue
            if is_season_dir(entry):
                continue

            title, year = parse_movie_dir(entry)
            if not title or not year:
                # Not a "Title (Year)" movie folder: either a TV/anime series
                # folder (has episode files) or a multi-movie collection folder.
                if dir_has_episodes(full_path):
                    series_entries.append(make_series_entry(full_path, entry, library))
                else:
                    scan_collection_folder(full_path, library, groups)
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

    tc.progress("dedup", done=entries_seen, total=total_entries,
                phase="cross-referencing Radarr/Sonarr", force=True)
    # Cross-reference with Radarr to catch cross-language/cross-title duplicates
    all_entries = []
    for entries_list in groups.values():
        all_entries.extend(entries_list)

    radarr_movies = _fetch_radarr_movies()
    if radarr_movies:
        # Build path suffix → TMDB ID lookup from Radarr
        radarr_by_suffix = {}
        radarr_by_alt_title = {}
        for rm in radarr_movies:
            tmdb_id = rm.get("tmdbId")
            rpath = (rm.get("path") or "").rstrip("/")
            if not tmdb_id:
                continue
            if rpath:
                suffix = rpath.lstrip("/").split("/", 1)[-1] if "/" in rpath.lstrip("/") else rpath.lstrip("/")
                radarr_by_suffix[suffix.lower()] = tmdb_id
            # Index by all known titles (original, alt titles)
            for title_obj in rm.get("alternateTitles", []):
                alt = normalize_title(title_obj.get("title", ""))
                yr = rm.get("year", 0)
                if alt:
                    radarr_by_alt_title[(alt, yr)] = tmdb_id
            main_title = normalize_title(rm.get("title", ""))
            orig_title = normalize_title(rm.get("originalTitle", ""))
            yr = rm.get("year", 0)
            if main_title:
                radarr_by_alt_title[(main_title, yr)] = tmdb_id
            if orig_title:
                radarr_by_alt_title[(orig_title, yr)] = tmdb_id

        # Resolve TMDB ID for each scanned entry
        tmdb_groups = defaultdict(list)
        for entry in all_entries:
            tmdb_id = None
            # Try path match
            ep = entry["path"].rstrip("/")
            ep_suffix = ep.rsplit("/", 1)[-1].lower()
            if ep_suffix in radarr_by_suffix:
                tmdb_id = radarr_by_suffix[ep_suffix]
            # Try title+year match against Radarr's alt titles
            if not tmdb_id:
                norm = normalize_title(entry["title"])
                yr = entry["year"]
                tmdb_id = radarr_by_alt_title.get((norm, yr))
            if tmdb_id:
                tmdb_groups[tmdb_id].append(entry)

        for tmdb_id, entries in tmdb_groups.items():
            if len(entries) < 2:
                continue
            # Only add if different from an existing title-based group
            norms = set(normalize_title(e["title"]) for e in entries)
            if len(norms) <= 1:
                norm_key = (norms.pop(), entries[0]["year"])
                if norm_key in groups and len(groups[norm_key]) >= len(entries):
                    continue
            merged_key = f"tmdb-{tmdb_id}"
            groups[merged_key] = entries
        print(f"Radarr cross-reference: checked {len(radarr_movies)} movies, {len(radarr_by_alt_title)} title variants")

    # Cross-reference TV/anime series across ALL libraries (same show living in
    # both /tv and /anime, etc.). Group by Sonarr TVDB ID, falling back to a
    # cleaned-title key for series Sonarr does not track.
    if series_entries:
        sonarr_series = _fetch_sonarr_series()
        son_by_suffix = {}
        son_by_title = {}
        son_title = {}
        son_year = {}
        for ss in sonarr_series:
            tv = ss.get("tvdbId")
            if not tv:
                continue
            spath = (ss.get("path") or "").rstrip("/")
            if spath:
                son_by_suffix[spath.rsplit("/", 1)[-1].lower()] = tv
            nt = normalize_title(ss.get("title", ""))
            if nt:
                son_by_title[nt] = tv
            son_title[tv] = ss.get("title", "")
            son_year[tv] = ss.get("year", 0)

        tvdb_groups = defaultdict(list)
        title_groups = defaultdict(list)
        for e in series_entries:
            leaf = e["path"].rstrip("/").rsplit("/", 1)[-1].lower()
            tv = son_by_suffix.get(leaf) or son_by_title.get(e["clean_norm"])
            if tv:
                if son_title.get(tv):
                    e["title"] = son_title[tv]
                e["year"] = son_year.get(tv, 0)
                tvdb_groups[tv].append(e)
            else:
                title_groups[e["clean_norm"]].append(e)

        for tv, ents in tvdb_groups.items():
            if len(ents) >= 2:
                groups[f"tvdb-{tv}"] = ents
        for ct, ents in title_groups.items():
            if len(ents) >= 2:
                groups[f"series:{ct}"] = ents
        print(f"Sonarr cross-reference: checked {len(sonarr_series)} series, "
              f"{len(series_entries)} series folders scanned")

    ignored = set()
    try:
        ignored = set(json.loads(DEDUP_IGNORE_FILE.read_text()))
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    duplicates = []
    total_waste_gb = 0
    for group_key_raw, entries in sorted(groups.items(), key=lambda x: str(x[0])):
        if len(entries) < 2:
            continue
        if isinstance(group_key_raw, tuple):
            norm, year = group_key_raw
            group_key = f"{norm}|{year}"
            year_val = year
        else:
            group_key = str(group_key_raw)
            year_val = entries[0]["year"] if entries else 0
        if group_key in ignored:
            continue
        entries.sort(key=lambda x: x["size_gb"], reverse=True)
        waste = sum(e["size_gb"] for e in entries[1:])
        total_waste_gb += waste
        duplicates.append({
            "key": group_key,
            "title": entries[0]["title"],
            "year": year_val,
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
    tc.progress_done("dedup")
    print(f"Found {len(duplicates)} duplicate groups, {total_waste_gb:.1f} GB potential waste")


if __name__ == "__main__":
    scan()
