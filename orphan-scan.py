#!/usr/bin/env python3
"""Scan the qBittorrent download root for orphan files — entries on disk that no
torrent points at anymore (failed imports, removed-but-kept torrents, manual
leftovers).

Unlike the standalone cron, this records BOTH the *real* on-disk size
(st_blocks) and the *apparent* allocated size (st_size). qBittorrent
pre-allocates the full file size for a download, so an abandoned BD-remux can
report as 30 GB while occupying 18 MB on disk — the apparent number is what made
the cron's reports so misleading. We surface both and sort/total by real size.
"""
import http.cookiejar
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import trimbin_common as tc

DATA_DIR = tc.DATA_DIR
OUTPUT_FILE = DATA_DIR / "orphan_scan.json"

cfg = tc.load_config()


def conf(key, default=""):
    return cfg.get(key) or os.environ.get(key, default)


QBIT_URL = conf("QBIT_URL").rstrip("/")
DOWNLOAD_ROOT = conf("QBIT_DOWNLOAD_ROOT", "/downloads").rstrip("/")
# Category subdirs that hold many torrents — scanned one level deeper so an
# orphan inside e.g. /downloads/music is found, not the whole music/ dir.
CAT_SUBDIRS = {"mam", "books", "comics", "music"}


def qbit_torrents():
    """All torrents qBittorrent knows about, or raise. Logs in only when a
    username is set (the homelab relies on the auth-subnet whitelist)."""
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    user = conf("QBIT_USERNAME")
    if user:
        data = urllib.parse.urlencode(
            {"username": user, "password": conf("QBIT_PASSWORD")}).encode()
        req = urllib.request.Request(QBIT_URL + "/api/v2/auth/login", data=data, method="POST")
        req.add_header("Referer", QBIT_URL)
        if opener.open(req, timeout=15).read().decode(errors="replace").strip() != "Ok.":
            raise RuntimeError("qBittorrent login rejected (check username/password)")
    raw = opener.open(
        urllib.request.Request(QBIT_URL + "/api/v2/torrents/info?filter=all"), timeout=30).read()
    return json.loads(raw)


def disk_and_apparent(path):
    """(real_on_disk_bytes, apparent_bytes) for a file or directory tree.
    real uses st_blocks*512 (actual allocation); apparent uses st_size."""
    real = apparent = 0
    if os.path.isdir(path) and not os.path.islink(path):
        for dirpath, _, filenames in os.walk(path):
            for f in filenames:
                try:
                    st = os.lstat(os.path.join(dirpath, f))
                    real += st.st_blocks * 512
                    apparent += st.st_size
                except OSError:
                    pass
    else:
        try:
            st = os.lstat(path)
            real = st.st_blocks * 512
            apparent = st.st_size
        except OSError:
            pass
    return real, apparent


def classify(real, apparent):
    """Safety tier for an orphan.
    - failed : allocated >> on-disk → abandoned/sparse download, dead weight.
    - stub   : <1 MiB leftover (renamed-out folder, .torrent/.nfo cruft).
    - complete: real content of meaningful size — the only download copy, review first.
    """
    if real < 1024 * 1024:
        return "stub"
    if apparent > 200 * 1024 * 1024 and real < apparent * 0.5:
        return "failed"
    return "complete"


def _empty_result(error=None):
    return {
        "last_scan": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "download_root": DOWNLOAD_ROOT,
        "error": error,
        "total_items": 0,
        "total_real_bytes": 0,
        "total_apparent_bytes": 0,
        "by_tier": {},
        "items": [],
    }


def scan():
    if not QBIT_URL:
        tc.write_json_atomic(OUTPUT_FILE, _empty_result("QBIT_URL not configured — set it in Settings."))
        print("QBIT_URL not configured")
        return
    if not os.path.isdir(DOWNLOAD_ROOT):
        tc.write_json_atomic(OUTPUT_FILE, _empty_result(
            f"Download root {DOWNLOAD_ROOT} is not mounted into the container."))
        print(f"download root {DOWNLOAD_ROOT} not found")
        return

    torrents = qbit_torrents()

    # Build the set of disk entries any torrent references.
    owned = set()                              # top-level names directly owned
    inside = {c: set() for c in CAT_SUBDIRS}   # names owned inside a category subdir
    for t in torrents:
        cp = t.get("content_path", "")
        if not cp.startswith(DOWNLOAD_ROOT + "/"):
            continue
        parts = cp[len(DOWNLOAD_ROOT) + 1:].split("/")
        if parts[0] in CAT_SUBDIRS and len(parts) > 1:
            inside[parts[0]].add(parts[1])
        else:
            owned.add(parts[0])

    items = []
    seen = 0

    def add(name, full):
        nonlocal seen
        seen += 1
        tc.progress("orphans", done=seen, current=full)
        real, apparent = disk_and_apparent(full)
        items.append({
            "name": name,
            "path": full,
            "real_bytes": real,
            "apparent_bytes": apparent,
            "is_dir": os.path.isdir(full),
            "tier": classify(real, apparent),
        })

    for entry in sorted(os.listdir(DOWNLOAD_ROOT)):
        full = os.path.join(DOWNLOAD_ROOT, entry)
        if entry in CAT_SUBDIRS:
            try:
                subs = sorted(os.listdir(full))
            except OSError:
                subs = []
            for sub in subs:
                if sub not in inside[entry]:
                    add(f"{entry}/{sub}", os.path.join(full, sub))
            continue
        if entry in owned:
            continue
        add(entry, full)

    items.sort(key=lambda x: x["real_bytes"], reverse=True)

    by_tier = {}
    for it in items:
        b = by_tier.setdefault(it["tier"], {"count": 0, "real_bytes": 0, "apparent_bytes": 0})
        b["count"] += 1
        b["real_bytes"] += it["real_bytes"]
        b["apparent_bytes"] += it["apparent_bytes"]

    result = {
        "last_scan": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "download_root": DOWNLOAD_ROOT,
        "error": None,
        "total_items": len(items),
        "total_real_bytes": sum(i["real_bytes"] for i in items),
        "total_apparent_bytes": sum(i["apparent_bytes"] for i in items),
        "by_tier": by_tier,
        "items": items,
    }
    tc.write_json_atomic(OUTPUT_FILE, result)
    tc.progress_done("orphans")
    print(f"Found {len(items)} orphans — "
          f"{result['total_real_bytes'] / 1e9:.2f} GB on disk "
          f"({result['total_apparent_bytes'] / 1e9:.1f} GB apparent)")


if __name__ == "__main__":
    try:
        scan()
    except Exception:
        tc.progress_done("orphans")
        import traceback
        traceback.print_exc()
        raise
