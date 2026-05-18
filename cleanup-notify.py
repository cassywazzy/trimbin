#!/usr/bin/env python3
"""Letterboxd watched-movie cleanup notifier.

Weekly one-shot: scrapes the user's Letterboxd "films" page (all watched),
cross-references against Radarr's on-disk library, and posts a Discord digest
listing watched movies still consuming storage — sorted largest first.

First run: posts the full list. Subsequent runs: only highlights NEW watches
since the last notification, plus a summary total line.

Shares the slug→TMDB cache with letterboxd-sync (same /data volume).
"""
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ENV_FILE = Path(os.environ.get("CLEANUP_ENV_FILE", "/app/cleanup.env"))
if ENV_FILE.exists():
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

LB_USER        = os.environ["LETTERBOXD_USER"]
RADARR_URL     = os.environ["RADARR_URL"].rstrip("/")
RADARR_API_KEY = os.environ["RADARR_API_KEY"]
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "")
DATA_DIR       = Path(os.environ.get("DATA_DIR", "/data"))
HC_PING_URL    = os.environ.get("HC_PING_URL", "")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s",
                    stream=sys.stdout)
log = logging.getLogger("lb-cleanup")

DATA_DIR.mkdir(parents=True, exist_ok=True)
SLUG_CACHE     = DATA_DIR / "slug_to_tmdb.json"
NOTIFIED_FILE  = DATA_DIR / "cleanup_notified_tmdb_ids.json"


def load_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception as e:
            log.warning("load %s failed: %s", path, e)
    return default


def save_json(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    tmp.replace(path)


def http_get(url, timeout=30, referer=None):
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml",
    }
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", errors="replace")


def ping_hc(suffix=""):
    if not HC_PING_URL:
        return
    url = f"{HC_PING_URL}/{suffix}" if suffix else HC_PING_URL
    try:
        urllib.request.urlopen(url, timeout=10)
    except Exception as e:
        log.warning("HC ping failed: %s", e)


def scrape_watched_slugs():
    """Scrape all watched film slugs from letterboxd.com/<user>/films/."""
    slugs, seen = [], set()
    base_url = f"https://letterboxd.com/{LB_USER}/films/"
    page = 1
    while True:
        url = f"{base_url}page/{page}/"
        try:
            html = http_get(url, referer=base_url)
        except urllib.error.HTTPError as e:
            if e.code in (404, 403):
                if e.code == 403:
                    log.info("page %d: CF 403 — stopping pagination (got %d films)", page, len(slugs))
                break
            log.error("page %d: HTTP %d", page, e.code)
            raise
        page_slugs = re.findall(r'data-film-slug="([^"]+)"', html)
        if not page_slugs:
            page_slugs = re.findall(r'data-target-link="/film/([^"/]+)/"', html)
        new = [s for s in page_slugs if s not in seen]
        if not new:
            break
        for s in new:
            slugs.append(s)
            seen.add(s)
        log.info("watched page %d: %d films (running total %d)", page, len(new), len(slugs))
        page += 1
        time.sleep(2)
    return slugs


def resolve_slug(slug, cache):
    """Resolve a Letterboxd slug to a TMDB ID, using/updating the shared cache."""
    if slug in cache:
        return cache[slug]
    url = f"https://letterboxd.com/film/{slug}/"
    try:
        html = http_get(url)
    except Exception as e:
        log.warning("fetch %s failed: %s", url, e)
        return None
    m = re.search(r'data-tmdb-id="(\d+)"', html)
    if not m:
        log.debug("no tmdb-id for %s", slug)
        cache[slug] = None
        return None
    tmdb = int(m.group(1))
    cache[slug] = tmdb
    return tmdb


def get_radarr_library():
    """Return {tmdb_id: {title, year, size_gb, radarr_id}} for movies with files on disk."""
    req = urllib.request.Request(
        f"{RADARR_URL}/api/v3/movie",
        headers={"X-Api-Key": RADARR_API_KEY},
    )
    data = json.loads(urllib.request.urlopen(req, timeout=60).read())
    movies = {}
    for m in data:
        if m.get("hasFile") and m.get("sizeOnDisk", 0) > 0:
            movies[m["tmdbId"]] = {
                "title": m["title"],
                "year": m.get("year", ""),
                "size_gb": round(m["sizeOnDisk"] / (1024 ** 3), 1),
                "radarr_id": m["id"],
            }
    return movies


DISCORD_MSG_FILE = DATA_DIR / "cleanup_discord_msg.json"


def post_discord(content):
    """Write Discord payload to a file for the host-side wrapper to POST.

    Python urllib inside this container gets CF-blocked by Discord.
    The wrapper script reads the file and POSTs with curl from the LXC host.
    """
    payload = {"content": content}
    save_json(DISCORD_MSG_FILE, payload)
    log.info("wrote Discord message to %s (%d chars)", DISCORD_MSG_FILE, len(content))


def main():
    ping_hc("start")

    log.info("scraping watched films for %s", LB_USER)
    watched_slugs = scrape_watched_slugs()
    log.info("found %d watched films on Letterboxd", len(watched_slugs))

    if not watched_slugs:
        log.error("no watched films found — likely scrape failure, aborting")
        ping_hc("fail")
        return

    slug_cache = load_json(SLUG_CACHE, {})
    notified = set(load_json(NOTIFIED_FILE, []))
    first_run = not NOTIFIED_FILE.exists()

    log.info("resolving slugs to TMDB IDs (cache has %d entries)", len(slug_cache))
    watched_tmdb = {}
    uncached = 0
    for slug in watched_slugs:
        was_cached = slug in slug_cache
        tmdb_id = resolve_slug(slug, slug_cache)
        if tmdb_id is not None:
            watched_tmdb[tmdb_id] = slug
        if not was_cached:
            uncached += 1
            time.sleep(0.5)
    save_json(SLUG_CACHE, slug_cache)
    log.info("resolved %d/%d to TMDB IDs (%d new lookups)",
             len(watched_tmdb), len(watched_slugs), uncached)

    log.info("fetching Radarr library")
    radarr = get_radarr_library()
    log.info("Radarr has %d movies with files on disk", len(radarr))

    watched_on_disk = []
    for tmdb_id in watched_tmdb:
        if tmdb_id in radarr:
            movie = radarr[tmdb_id]
            watched_on_disk.append({
                "tmdb_id": tmdb_id,
                "title": movie["title"],
                "year": movie["year"],
                "size_gb": movie["size_gb"],
                "radarr_id": movie["radarr_id"],
                "new": tmdb_id not in notified,
            })
    watched_on_disk.sort(key=lambda x: x["size_gb"], reverse=True)

    total_count = len(watched_on_disk)
    total_gb = sum(m["size_gb"] for m in watched_on_disk)
    new_watches = [m for m in watched_on_disk if m["new"]]
    new_gb = sum(m["size_gb"] for m in new_watches)

    log.info("watched on disk: %d movies (%.0f GB), %d new since last run (%.0f GB)",
             total_count, total_gb, len(new_watches), new_gb)

    if not new_watches:
        log.info("no new watched movies on disk — skipping Discord")
        pruned = notified & set(radarr.keys())
        if len(pruned) != len(notified):
            log.info("pruned %d stale IDs from notified set", len(notified) - len(pruned))
            save_json(NOTIFIED_FILE, sorted(pruned))
        write_status(total_count, total_gb, 0, 0)
        save_json(WATCHED_LIST_FILE, watched_on_disk)
        ping_hc()
        return

    if first_run:
        header = (f"**Letterboxd Cleanup Digest** — "
                  f"{total_count} watched movies still on disk ({total_gb:.0f} GB)\n\n"
                  f"First scan — showing top items by size:\n")
        show_list = watched_on_disk[:20]
        footer_extra = ""
        if total_count > 20:
            footer_extra = f"\n_...and {total_count - 20} more (full list: {total_count} movies)_\n"
    else:
        header = (f"**Letterboxd Cleanup Digest** — "
                  f"{len(new_watches)} newly watched movie{'s' if len(new_watches) != 1 else ''} "
                  f"still on disk ({new_gb:.0f} GB)\n")
        show_list = new_watches[:25]
        footer_extra = ""
        if len(new_watches) > 25:
            footer_extra = f"\n_...and {len(new_watches) - 25} more_\n"

    lines = [header]
    for m in show_list:
        lines.append(f"- **{m['title']}** ({m['year']}) — {m['size_gb']} GB")
    if footer_extra:
        lines.append(footer_extra)
    lines.append(f"\n**Total watched on disk: {total_count} movies, {total_gb:.0f} GB**")
    lines.append("_Delete via Radarr if no longer needed._")

    msg = "\n".join(lines)
    if len(msg) > 1990:
        msg = msg[:1987] + "..."

    log.info("posting to Discord")
    post_discord(msg)

    all_notified = (notified | {m["tmdb_id"] for m in watched_on_disk}) & set(radarr.keys())
    save_json(NOTIFIED_FILE, sorted(all_notified))
    log.info("updated notified set: %d IDs", len(all_notified))

    write_status(total_count, total_gb, len(new_watches), new_gb)
    save_json(WATCHED_LIST_FILE, watched_on_disk)

    ping_hc()
    log.info("done")


STATUS_FILE = DATA_DIR / "cleanup_status.json"
WATCHED_LIST_FILE = DATA_DIR / "cleanup_watched_list.json"


def write_status(watched_count, total_gb, new_count, new_gb):
    save_json(STATUS_FILE, {
        "watched_on_disk": watched_count,
        "total_gb": round(total_gb),
        "new_since_last": new_count,
        "new_gb": round(new_gb),
        "last_run": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    })


if __name__ == "__main__":
    main()
