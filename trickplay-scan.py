#!/usr/bin/env python3
"""Scan media libraries for bloated or broken trickplay directories."""
import json
import os
from datetime import datetime, timezone

import trimbin_common as tc

DATA_DIR = tc.DATA_DIR
OUTPUT_FILE = str(DATA_DIR / "trickplay_scan.json")

VIDEO_EXTS = tc.VIDEO_EXTS
SIZE_THRESHOLD = 50 * 1024 * 1024

get_media_roots = tc.get_media_roots


def scan_trickplay():
    media_roots = get_media_roots()
    if not media_roots:
        print("No media libraries configured. Add them in Settings.")
        output = {
            "last_scan": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "total_trickplay_dirs": 0, "total_trickplay_gb": 0,
            "flagged_count": 0, "flagged_gb": 0, "flagged": [],
        }
        tc.write_json_atomic(OUTPUT_FILE, output)
        return output

    results = []
    total_size = 0
    total_dirs = 0
    dirs_seen = 0

    for root_dir in media_roots:
        if not os.path.isdir(root_dir):
            continue
        library = os.path.basename(root_dir)

        for dirpath, dirnames, filenames in os.walk(root_dir):
            dirs_seen += 1
            tc.progress("trickplay", done=dirs_seen, current=dirpath)
            basename = os.path.basename(dirpath)
            if not basename.endswith(".trickplay") and basename != "trickplay":
                continue

            total_dirs += 1
            dir_size = 0
            video_files = []
            file_count = 0

            for sub_root, _, sub_files in os.walk(dirpath):
                for f in sub_files:
                    fp = os.path.join(sub_root, f)
                    try:
                        sz = os.path.getsize(fp)
                    except OSError:
                        continue
                    dir_size += sz
                    file_count += 1
                    ext = os.path.splitext(f)[1].lower()
                    if ext in VIDEO_EXTS:
                        video_files.append({"name": f, "size_gb": round(sz / (1024**3), 2)})

            total_size += dir_size

            if dir_size < SIZE_THRESHOLD and not video_files:
                continue

            parent = os.path.dirname(dirpath)
            movie_name = os.path.basename(parent)
            if parent in media_roots:
                movie_name = basename.replace(".trickplay", "")

            issues = []
            if video_files:
                issues.append("contains_video")
            if dir_size >= SIZE_THRESHOLD:
                issues.append("oversized")

            results.append({
                "path": dirpath,
                "movie": movie_name,
                "library": library,
                "size_bytes": dir_size,
                "size_gb": round(dir_size / (1024**3), 2),
                "file_count": file_count,
                "video_files": video_files,
                "issues": issues,
            })

    results.sort(key=lambda x: x["size_bytes"], reverse=True)

    output = {
        "last_scan": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "total_trickplay_dirs": total_dirs,
        "total_trickplay_gb": round(total_size / (1024**3), 2),
        "flagged_count": len(results),
        "flagged_gb": round(sum(r["size_bytes"] for r in results) / (1024**3), 2),
        "flagged": results,
    }

    tc.write_json_atomic(OUTPUT_FILE, output)
    tc.progress_done("trickplay")

    return output


if __name__ == "__main__":
    data = scan_trickplay()
    print(f"Scanned {data['total_trickplay_dirs']} trickplay dirs, "
          f"total {data['total_trickplay_gb']} GB")
    print(f"Flagged {data['flagged_count']} issues ({data['flagged_gb']} GB)")
    for item in data["flagged"]:
        issues = ", ".join(item["issues"])
        print(f"  {item['size_gb']:6.1f}G  [{issues}]  {item['movie']}")
