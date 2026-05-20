#!/usr/bin/env python3
"""Trimbin scanner — multi-source watch cross-reference.

Pulls watched movies/shows from Letterboxd, Trakt, and Jellystat, then
cross-references against Radarr/Sonarr libraries to find watched content
still consuming storage. Writes status JSON for the Trimbin web UI and
posts a Discord digest.
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

LB_USER         = os.environ.get("LETTERBOXD_USER", "")
RADARR_URL      = os.environ.get("RADARR_URL", "").rstrip("/")
RADARR_API_KEY  = os.environ.get("RADARR_API_KEY", "")
SONARR_URL      = os.environ.get("SONARR_URL", "").rstrip("/")
SONARR_API_KEY  = os.environ.get("SONARR_API_KEY", "")
TRAKT_CLIENT_ID = os.environ.get("TRAKT_CLIENT_ID", "")
TRAKT_USERNAME  = os.environ.get("TRAKT_USERNAME", "")
JELLYSTAT_URL   = os.environ.get("JELLYSTAT_URL", "").rstrip("/")
JELLYSTAT_KEY   = os.environ.get("JELLYSTAT_API_KEY", "")
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "")
DATA_DIR        = Path(os.environ.get("DATA_DIR", "/data"))
HC_PING_URL     = os.environ.get("HC_PING_URL", "")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s",
                    stream=sys.stdout)
log = logging.getLogger("trimbin")

DATA_DIR.mkdir(parents=True, exist_ok=True)
SLUG_CACHE      = DATA_DIR / "slug_to_tmdb.json"
NOTIFIED_FILE   = DATA_DIR / "cleanup_notified_tmdb_ids.json"
STATUS_FILE     = DATA_DIR / "cleanup_status.json"
WATCHED_LIST_FILE = DATA_DIR / "cleanup_watched_list.json"
SHOWS_LIST_FILE = DATA_DIR / "trimbin_shows.json"
DISCORD_MSG_FILE = DATA_DIR / "cleanup_discord_msg.json"


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


def http_get(url, timeout=30, referer=None, headers=None):
    hdrs = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml"}
    if referer:
        hdrs["Referer"] = referer
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs)
    return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", errors="replace")


def api_json(url, timeout=30, headers=None, method="GET", body=None):
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    resp = urllib.request.urlopen(req, timeout=timeout)
    return json.loads(resp.read())


def ping_hc(suffix=""):
    if not HC_PING_URL:
        return
    url = f"{HC_PING_URL}/{suffix}" if suffix else HC_PING_URL
    try:
        urllib.request.urlopen(url, timeout=10)
    except Exception as e:
        log.warning("HC ping failed: %s", e)


# ---------------------------------------------------------------------------
# Letterboxd scraper
# ---------------------------------------------------------------------------

def scrape_letterboxd_movies():
    if not LB_USER:
        log.info("LETTERBOXD_USER not set, skipping Letterboxd")
        return {}
    slugs, seen = [], set()
    base_url = f"https://letterboxd.com/{LB_USER}/films/"
    page = 1
    while True:
        url = f"{base_url}page/{page}/"
        try:
            html = http_get(url, referer=base_url)
        except urllib.error.HTTPError as e:
            if e.code in (404, 403):
                break
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
        log.info("letterboxd page %d: %d films (total %d)", page, len(new), len(slugs))
        page += 1
        time.sleep(2)

    slug_cache = load_json(SLUG_CACHE, {})
    watched = {}
    for slug in slugs:
        tmdb = _resolve_slug(slug, slug_cache)
        if tmdb is not None:
            watched[tmdb] = slug
    save_json(SLUG_CACHE, slug_cache)
    log.info("letterboxd: %d/%d resolved to TMDB IDs", len(watched), len(slugs))
    return watched


def _resolve_slug(slug, cache):
    if slug in cache:
        return cache[slug]
    url = f"https://letterboxd.com/film/{slug}/"
    try:
        html = http_get(url)
    except Exception:
        return None
    m = re.search(r'data-tmdb-id="(\d+)"', html)
    if not m:
        cache[slug] = None
        return None
    tmdb = int(m.group(1))
    cache[slug] = tmdb
    return tmdb


# ---------------------------------------------------------------------------
# Trakt API
# ---------------------------------------------------------------------------

def trakt_headers():
    return {
        "Content-Type": "application/json",
        "trakt-api-version": "2",
        "trakt-api-key": TRAKT_CLIENT_ID,
    }


def fetch_trakt_movies():
    if not TRAKT_CLIENT_ID or not TRAKT_USERNAME:
        log.info("TRAKT_CLIENT_ID/TRAKT_USERNAME not set, skipping Trakt movies")
        return {}
    url = f"https://api.trakt.tv/users/{TRAKT_USERNAME}/watched/movies"
    data = api_json(url, headers=trakt_headers())
    watched = {}
    for item in data:
        tmdb = item.get("movie", {}).get("ids", {}).get("tmdb")
        if tmdb:
            watched[tmdb] = item["movie"]["title"]
    log.info("trakt: %d watched movies", len(watched))
    return watched


def fetch_trakt_shows():
    if not TRAKT_CLIENT_ID or not TRAKT_USERNAME:
        log.info("TRAKT_CLIENT_ID/TRAKT_USERNAME not set, skipping Trakt shows")
        return {}
    url = f"https://api.trakt.tv/users/{TRAKT_USERNAME}/watched/shows"
    data = api_json(url, headers=trakt_headers())
    shows = {}
    for item in data:
        ids = item.get("show", {}).get("ids", {})
        tvdb = ids.get("tvdb")
        if not tvdb:
            continue
        watched_eps = {}
        for season in item.get("seasons", []):
            snum = season["number"]
            watched_eps[snum] = len(season.get("episodes", []))
        shows[tvdb] = {
            "title": item["show"]["title"],
            "year": item["show"].get("year"),
            "tmdb": ids.get("tmdb"),
            "watched_seasons": watched_eps,
            "total_watched_eps": sum(watched_eps.values()),
        }
    log.info("trakt: %d watched shows", len(shows))
    return shows


# ---------------------------------------------------------------------------
# Jellystat API
# ---------------------------------------------------------------------------

def jellystat_headers():
    return {"x-api-token": JELLYSTAT_KEY, "Content-Type": "application/json"}


def fetch_jellystat_movie_watches(jellyfin_id):
    """Get distinct user count who watched a Jellyfin item."""
    if not JELLYSTAT_URL or not JELLYSTAT_KEY:
        return 0, []
    try:
        data = api_json(
            f"{JELLYSTAT_URL}/api/getItemHistory?size=200&page=1",
            headers=jellystat_headers(),
            method="POST",
            body={"itemid": jellyfin_id},
        )
        results = data.get("results", [])
        users = {}
        for r in results:
            uid = r.get("UserId")
            if uid and uid not in users:
                users[uid] = r.get("UserName", "Unknown")
        return len(users), list(users.values())
    except Exception as e:
        log.warning("jellystat query failed for %s: %s", jellyfin_id, e)
        return 0, []


def fetch_jellystat_total_users():
    """Get total Jellyfin user count for 'X of Y watched' display."""
    if not JELLYSTAT_URL or not JELLYSTAT_KEY:
        return 0
    try:
        data = api_json(
            f"{JELLYSTAT_URL}/stats/getAllUserActivity",
            headers=jellystat_headers(),
        )
        return len(data) if isinstance(data, list) else 0
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Jellyfin API (for ID mapping)
# ---------------------------------------------------------------------------

JELLYFIN_URL = os.environ.get("JELLYFIN_URL", "").rstrip("/")
JELLYFIN_API_KEY = os.environ.get("JELLYFIN_API_KEY", "")


def jellyfin_lookup_by_tmdb(tmdb_id):
    """Find a Jellyfin item ID by TMDB provider ID."""
    if not JELLYFIN_URL or not JELLYFIN_API_KEY:
        return None
    try:
        url = (f"{JELLYFIN_URL}/Items?hasTmdbId=true"
               f"&fields=ProviderIds&recursive=true&includeItemTypes=Movie"
               f"&api_key={JELLYFIN_API_KEY}")
        data = api_json(url)
        for item in data.get("Items", []):
            pids = item.get("ProviderIds", {})
            if str(pids.get("Tmdb", "")) == str(tmdb_id):
                return item["Id"]
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Radarr library
# ---------------------------------------------------------------------------

def get_radarr_library():
    if not RADARR_URL or not RADARR_API_KEY:
        log.info("RADARR_URL/RADARR_API_KEY not set, skipping Radarr")
        return {}
    data = api_json(
        f"{RADARR_URL}/api/v3/movie",
        headers={"X-Api-Key": RADARR_API_KEY},
        timeout=60,
    )
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


# ---------------------------------------------------------------------------
# Sonarr library
# ---------------------------------------------------------------------------

def get_sonarr_library():
    if not SONARR_URL or not SONARR_API_KEY:
        log.info("SONARR_URL/SONARR_API_KEY not set, skipping Sonarr")
        return {}
    data = api_json(
        f"{SONARR_URL}/api/v3/series",
        headers={"X-Api-Key": SONARR_API_KEY},
        timeout=60,
    )
    shows = {}
    for s in data:
        stats = s.get("statistics", {})
        if stats.get("episodeFileCount", 0) > 0:
            shows[s.get("tvdbId", 0)] = {
                "title": s["title"],
                "year": s.get("year", ""),
                "size_gb": round(stats.get("sizeOnDisk", 0) / (1024 ** 3), 1),
                "sonarr_id": s["id"],
                "episode_file_count": stats.get("episodeFileCount", 0),
                "episode_count": stats.get("episodeCount", 0),
                "season_count": stats.get("seasonCount", 0),
            }
    return shows


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------

def post_discord(content):
    save_json(DISCORD_MSG_FILE, {"content": content})
    log.info("wrote Discord message (%d chars)", len(content))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ping_hc("start")

    # ---- Movies ----
    lb_watched = scrape_letterboxd_movies()
    trakt_watched = fetch_trakt_movies()

    all_watched_tmdb = set(lb_watched.keys()) | set(trakt_watched.keys())
    log.info("total unique watched movies: %d (LB=%d, Trakt=%d, overlap=%d)",
             len(all_watched_tmdb), len(lb_watched), len(trakt_watched),
             len(set(lb_watched) & set(trakt_watched)))

    if not all_watched_tmdb and LB_USER:
        log.error("no watched films found from any source, aborting")
        ping_hc("fail")
        return

    radarr = get_radarr_library()
    log.info("radarr: %d movies with files on disk", len(radarr))

    notified = set(load_json(NOTIFIED_FILE, []))
    first_run = not NOTIFIED_FILE.exists()

    total_users = fetch_jellystat_total_users()

    watched_on_disk = []
    for tmdb_id in all_watched_tmdb:
        if tmdb_id in radarr:
            movie = radarr[tmdb_id]
            sources = []
            if tmdb_id in lb_watched:
                sources.append("letterboxd")
            if tmdb_id in trakt_watched:
                sources.append("trakt")

            watch_count, watched_by = 0, []
            if JELLYSTAT_URL:
                jf_id = jellyfin_lookup_by_tmdb(tmdb_id)
                if jf_id:
                    watch_count, watched_by = fetch_jellystat_movie_watches(jf_id)

            watched_on_disk.append({
                "tmdb_id": tmdb_id,
                "title": movie["title"],
                "year": movie["year"],
                "size_gb": movie["size_gb"],
                "radarr_id": movie["radarr_id"],
                "new": tmdb_id not in notified,
                "sources": sources,
                "watch_count": watch_count,
                "total_users": total_users,
                "watched_by": watched_by,
            })
    watched_on_disk.sort(key=lambda x: x["size_gb"], reverse=True)

    total_count = len(watched_on_disk)
    total_gb = sum(m["size_gb"] for m in watched_on_disk)
    new_watches = [m for m in watched_on_disk if m["new"]]
    new_gb = sum(m["size_gb"] for m in new_watches)

    log.info("movies on disk: %d (%.0f GB), %d new", total_count, total_gb, len(new_watches))

    # ---- Shows ----
    trakt_shows = fetch_trakt_shows()
    sonarr = get_sonarr_library()
    log.info("sonarr: %d shows with files on disk", len(sonarr))

    shows_on_disk = []
    for tvdb_id, trakt_info in trakt_shows.items():
        if tvdb_id in sonarr:
            show = sonarr[tvdb_id]
            watched_eps = trakt_info["total_watched_eps"]
            total_eps = show["episode_file_count"]
            pct = round(100 * watched_eps / total_eps) if total_eps > 0 else 0
            shows_on_disk.append({
                "tvdb_id": tvdb_id,
                "title": show["title"],
                "year": show["year"],
                "size_gb": show["size_gb"],
                "sonarr_id": show["sonarr_id"],
                "watched_episodes": watched_eps,
                "total_episodes": total_eps,
                "season_count": show["season_count"],
                "watched_pct": pct,
                "watched_seasons": trakt_info["watched_seasons"],
            })
    shows_on_disk.sort(key=lambda x: x["size_gb"], reverse=True)

    shows_count = len(shows_on_disk)
    shows_gb = sum(s["size_gb"] for s in shows_on_disk)
    log.info("shows on disk: %d (%.0f GB)", shows_count, shows_gb)

    # ---- Write data ----
    write_status(total_count, total_gb, len(new_watches), new_gb, shows_count, shows_gb)
    save_json(WATCHED_LIST_FILE, watched_on_disk)
    save_json(SHOWS_LIST_FILE, shows_on_disk)

    # ---- Discord ----
    if new_watches:
        if first_run:
            header = (f"**Trimbin Digest** — "
                      f"{total_count} watched movies still on disk ({total_gb:.0f} GB)\n\n"
                      f"First scan — showing top items by size:\n")
            show_list = watched_on_disk[:20]
            footer_extra = f"\n_...and {total_count - 20} more_\n" if total_count > 20 else ""
        else:
            header = (f"**Trimbin Digest** — "
                      f"{len(new_watches)} newly watched movie{'s' if len(new_watches) != 1 else ''} "
                      f"still on disk ({new_gb:.0f} GB)\n")
            show_list = new_watches[:25]
            footer_extra = f"\n_...and {len(new_watches) - 25} more_\n" if len(new_watches) > 25 else ""

        lines = [header]
        for m in show_list:
            src = "/".join(m["sources"]) if m["sources"] else "?"
            lines.append(f"- **{m['title']}** ({m['year']}) — {m['size_gb']} GB [{src}]")
        if footer_extra:
            lines.append(footer_extra)
        lines.append(f"\n**Total on disk: {total_count} movies ({total_gb:.0f} GB)"
                     f" + {shows_count} shows ({shows_gb:.0f} GB)**")

        msg = "\n".join(lines)
        if len(msg) > 1990:
            msg = msg[:1987] + "..."
        post_discord(msg)
    else:
        log.info("no new watched movies — skipping Discord")

    # ---- Update notified set ----
    all_notified = (notified | {m["tmdb_id"] for m in watched_on_disk}) & set(radarr.keys())
    pruned = len(notified) - len(all_notified & notified)
    if pruned > 0:
        log.info("pruned %d stale IDs from notified set", pruned)
    save_json(NOTIFIED_FILE, sorted(all_notified))

    ping_hc()
    log.info("done")


def write_status(movie_count, movie_gb, new_count, new_gb, show_count=0, show_gb=0):
    save_json(STATUS_FILE, {
        "watched_on_disk": movie_count,
        "total_gb": round(movie_gb),
        "new_since_last": new_count,
        "new_gb": round(new_gb),
        "shows_on_disk": show_count,
        "shows_gb": round(show_gb),
        "last_run": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    })


if __name__ == "__main__":
    main()
