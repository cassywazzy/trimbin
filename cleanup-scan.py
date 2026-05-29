#!/usr/bin/env python3
"""Scan media directories for orphan files and junk safe to delete."""
import json
import os
from datetime import datetime, timezone

import trimbin_common as tc

DATA_DIR = tc.DATA_DIR
OUTPUT_FILE = DATA_DIR / "cleanup_scan.json"

# The video set is shared (canonical). The sidecar/junk sets below stay local —
# they decide what gets *flagged as deletable*, so they're behaviour-sensitive.
VIDEO_EXTS = tc.VIDEO_EXTS
SUBTITLE_EXTS = {".srt", ".ass", ".ssa", ".sub", ".idx", ".sup", ".vtt"}
NFO_EXTS = {".nfo"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".tbn"}
OS_JUNK = {"thumbs.db", ".ds_store", "desktop.ini", "._.ds_store",
           "thumbs.db:encryptable", ".picasa.ini", "picasa.ini",
           ".bridgesort", ".bridgelabelsandratings", ".nomedia"}
JUNK_DIRS = {"@eadir", ".@__thumb", "@thumb", "__macosx", ".fseventsd", ".spotlight-v100"}
SCENE_JUNK_EXTS = {".sfv", ".srr", ".torrent", ".url", ".website", ".lnk"}
EXECUTABLE_EXTS = {".exe", ".bat", ".cmd", ".com", ".msi", ".scr"}
SCENE_JUNK_NAMES = {"rarbg.txt", "www.yify.txt", "etrg.txt", "read.me.txt",
                    "rarbg.com.txt", "rarbg_do_not_mirror.exe"}
SAMPLE_PATTERN = "sample"

get_media_roots = tc.get_media_roots
dir_size_bytes = tc.dir_size


def dir_has_video(dirpath):
    try:
        for f in os.listdir(dirpath):
            if os.path.splitext(f)[1].lower() in VIDEO_EXTS:
                return True
            if f.upper() == "BDMV" and os.path.isdir(os.path.join(dirpath, f)):
                return True
    except OSError:
        pass
    return False


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


def is_empty_dir(path):
    try:
        for _, dirs, files in os.walk(path):
            if files:
                return False
            if not dirs:
                return True
        return True
    except OSError:
        return False


def get_media_entry_root(path, roots):
    """Get the top-level media entry directory for a path (e.g. the movie or show folder)."""
    norm_roots = [os.path.normpath(r) for r in roots]
    parts = os.path.normpath(path)
    parent = os.path.dirname(parts)
    prev = parts
    while parent and os.path.normpath(parent) not in norm_roots:
        prev = parent
        parent = os.path.dirname(parent)
    if os.path.normpath(parent) in norm_roots:
        return prev
    return None


_entry_has_video_cache = {}


def media_entry_has_video(path, roots):
    """Check if the top-level media entry containing this path has video anywhere in its tree."""
    entry_root = get_media_entry_root(path, roots)
    if not entry_root:
        return False
    if entry_root not in _entry_has_video_cache:
        found = False
        try:
            for dirpath, _, filenames in os.walk(entry_root):
                for f in filenames:
                    if os.path.splitext(f)[1].lower() in VIDEO_EXTS:
                        found = True
                        break
                if not found:
                    if os.path.isdir(os.path.join(dirpath, "BDMV")):
                        found = True
                if found:
                    break
        except OSError:
            pass
        _entry_has_video_cache[entry_root] = found
    return _entry_has_video_cache[entry_root]


def scan():
    media_roots = get_media_roots()
    if not media_roots:
        print("No media libraries configured. Add them in Settings.")
        tc.write_json_atomic(OUTPUT_FILE, {
            "last_scan": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "total_items": 0, "total_bytes": 0, "total_gb": 0,
            "by_category": {}, "items": [],
        })
        return

    _entry_has_video_cache.clear()
    items = []
    category_counts = {}
    category_bytes = {}

    def add_item(category, path, size_bytes, label, library):
        items.append({
            "category": category,
            "path": path,
            "size_bytes": size_bytes,
            "size_gb": round(size_bytes / (1024**3), 2),
            "label": label,
            "library": library,
            "is_dir": os.path.isdir(path),
        })
        category_counts[category] = category_counts.get(category, 0) + 1
        category_bytes[category] = category_bytes.get(category, 0) + size_bytes

    for root in media_roots:
        if not os.path.isdir(root):
            continue
        library = os.path.basename(root)

        for dirpath, dirnames, filenames in os.walk(root):
            basename = os.path.basename(dirpath).lower()

            if basename in JUNK_DIRS:
                sz = dir_size_bytes(dirpath)
                add_item("junk_dir", dirpath, sz,
                         f"{os.path.basename(dirpath)} (indexing/OS junk)", library)
                dirnames.clear()
                continue

            if basename.endswith(".trickplay") or basename == "trickplay":
                dirnames.clear()
                continue

            for dn in list(dirnames):
                dl = dn.lower()
                if dl in JUNK_DIRS:
                    full = os.path.join(dirpath, dn)
                    sz = dir_size_bytes(full)
                    add_item("junk_dir", full, sz,
                             f"{dn} (indexing/OS junk)", library)
                    dirnames.remove(dn)

                elif dl == SAMPLE_PATTERN:
                    full = os.path.join(dirpath, dn)
                    sz = dir_size_bytes(full)
                    if sz > 0:
                        add_item("sample", full, sz,
                                 f"{dn}/ in {os.path.basename(dirpath)}", library)
                    dirnames.remove(dn)

            has_video = dir_has_video(dirpath)
            has_ancestor_video = None

            for fn in filenames:
                fl = fn.lower()
                full = os.path.join(dirpath, fn)
                ext = os.path.splitext(fl)[1]

                try:
                    sz = os.path.getsize(full)
                except OSError:
                    continue

                if fl in OS_JUNK:
                    add_item("os_junk", full, sz,
                             f"{fn} in {os.path.basename(dirpath)}", library)
                    continue

                if ext in EXECUTABLE_EXTS:
                    add_item("executable", full, sz, full, library)
                    continue

                if fl in SCENE_JUNK_NAMES:
                    add_item("scene_junk", full, sz,
                             f"{fn} in {os.path.basename(dirpath)}", library)
                    continue

                if ext in SCENE_JUNK_EXTS:
                    add_item("scene_junk", full, sz,
                             f"{fn} in {os.path.basename(dirpath)}", library)
                    continue

                if SAMPLE_PATTERN in fl and ext in VIDEO_EXTS and sz < 100 * 1024 * 1024:
                    add_item("sample", full, sz,
                             f"{fn} in {os.path.basename(dirpath)}", library)
                    continue

                if not has_video and ext in (SUBTITLE_EXTS | NFO_EXTS | IMAGE_EXTS):
                    if has_ancestor_video is None:
                        has_ancestor_video = media_entry_has_video(dirpath, media_roots)
                    if not has_ancestor_video:
                        if ext in SUBTITLE_EXTS:
                            add_item("orphan_sub", full, sz,
                                     f"{fn} in {os.path.basename(dirpath)}", library)
                        elif ext in NFO_EXTS:
                            add_item("orphan_nfo", full, sz,
                                     f"{fn} in {os.path.basename(dirpath)}", library)
                        elif ext in IMAGE_EXTS:
                            add_item("orphan_image", full, sz,
                                     f"{fn} in {os.path.basename(dirpath)}", library)

            if has_video is False and dirpath != root:
                try:
                    is_empty = (len(os.listdir(dirpath)) == 0)
                except OSError:
                    is_empty = False  # unreadable dir — never treat as empty/deletable
                if is_empty and not any(i["path"] == dirpath for i in items):
                    add_item("empty_dir", dirpath, 0,
                             os.path.basename(dirpath), library)

    items.sort(key=lambda x: x["size_bytes"], reverse=True)

    total_bytes = sum(i["size_bytes"] for i in items)

    by_category = {}
    for cat in sorted(category_counts.keys()):
        by_category[cat] = {
            "count": category_counts[cat],
            "size_bytes": category_bytes[cat],
            "size_gb": round(category_bytes[cat] / (1024**3), 2),
        }

    result = {
        "last_scan": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "total_items": len(items),
        "total_bytes": total_bytes,
        "total_gb": round(total_bytes / (1024**3), 2),
        "by_category": by_category,
        "items": items,
    }

    tc.write_json_atomic(OUTPUT_FILE, result)
    print(f"Found {len(items)} cleanup items, {result['total_gb']} GB reclaimable")
    for cat, info in by_category.items():
        print(f"  {cat}: {info['count']} items, {info['size_gb']} GB")


if __name__ == "__main__":
    scan()
