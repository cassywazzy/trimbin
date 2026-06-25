#!/usr/bin/env python3
"""Trimbin scanner — multi-source watch cross-reference.

Pulls watched movies/shows from Letterboxd, Simkl, and Jellystat, then
cross-references against Radarr/Sonarr libraries to find watched content
still consuming storage. Writes status JSON for the Trimbin web UI and
posts a Discord digest.
"""
import json
import logging
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
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

DATA_DIR        = Path(os.environ.get("DATA_DIR", "/data"))
CONFIG_FILE     = DATA_DIR / "trimbin_config.json"


def _cfg(key):
    """Read from UI config file first, then environment."""
    try:
        config = json.loads(CONFIG_FILE.read_text())
        val = config.get(key, "")
        if val:
            return val
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return os.environ.get(key, "")


def _cfg_int(key, default):
    try:
        return int(_cfg(key))
    except (TypeError, ValueError):
        return default


def _cfg_bool(key, default):
    v = _cfg(key).strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return default


LB_USER         = _cfg("LETTERBOXD_USER")
RADARR_URL      = _cfg("RADARR_URL").rstrip("/")
RADARR_API_KEY  = _cfg("RADARR_API_KEY")
SONARR_URL      = _cfg("SONARR_URL").rstrip("/")
SONARR_API_KEY  = _cfg("SONARR_API_KEY")
SIMKL_CLIENT_ID = _cfg("SIMKL_CLIENT_ID")
SIMKL_TOKEN     = _cfg("SIMKL_ACCESS_TOKEN")
JELLYSTAT_URL   = _cfg("JELLYSTAT_URL").rstrip("/")
JELLYSTAT_KEY   = _cfg("JELLYSTAT_API_KEY")
DISCORD_WEBHOOK = _cfg("DISCORD_WEBHOOK_URL")
HC_PING_URL     = _cfg("HC_PING_URL")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15")

# ---- Letterboxd scrape tuning (rate-limit resilience + incremental sync) ----
# Letterboxd 403s aggressively once it decides you're scraping; once tripped the
# block outlasts a short retry. So we (a) walk fewer pages by default via an
# incremental diary scrape that stops at the last-seen film, (b) back off long
# enough to outlast a block, and (c) never let a truncated scrape shrink the
# persisted watched set. See [[Trimbin]] History 2026-06-25.
LB_PAGE_SLEEP            = _cfg_int("LB_PAGE_SLEEP", 6)          # base seconds between page fetches
LB_403_BACKOFF          = [20, 60, 120]                          # retry sleeps (s) after a 403
LB_INCREMENTAL          = _cfg_bool("LB_INCREMENTAL", True)      # diary-based incremental sync
LB_INCREMENTAL_MAX_PAGES = _cfg_int("LB_INCREMENTAL_MAX_PAGES", 5)  # diary pages to walk before stopping
LB_FULL_RESCAN_DAYS     = _cfg_int("LB_FULL_RESCAN_DAYS", 14)    # force a full reconcile this often

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s",
                    stream=sys.stdout)
log = logging.getLogger("trimbin")

DATA_DIR.mkdir(parents=True, exist_ok=True)
SLUG_CACHE      = DATA_DIR / "slug_to_tmdb.json"
LB_WATCHED_CACHE = DATA_DIR / "lb_watched_cache.json"  # accumulator: {slug: rating|null} + full_scrape_ts
NOTIFIED_FILE   = DATA_DIR / "cleanup_notified_tmdb_ids.json"
STATUS_FILE     = DATA_DIR / "cleanup_status.json"
WATCHED_LIST_FILE = DATA_DIR / "cleanup_watched_list.json"
SHOWS_LIST_FILE = DATA_DIR / "trimbin_shows.json"
DISCORD_MSG_FILE = DATA_DIR / "cleanup_discord_msg.json"
IGNORED_FILE    = DATA_DIR / "trimbin_ignored.json"
IGNORED_SHOWS_FILE = DATA_DIR / "trimbin_ignored_shows.json"
AUTO_IGNORED_FILE = DATA_DIR / "trimbin_auto_ignored.json"


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


def api_json(url, timeout=30, headers=None, method="GET", body=None, retries=2):
    """JSON API call with retry/backoff on transient failures (5xx, timeouts,
    connection errors). 4xx and other HTTP errors are raised immediately."""
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    data = json.dumps(body).encode() if body else None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
            resp = urllib.request.urlopen(req, timeout=timeout)
            return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code >= 500 and attempt < retries:
                time.sleep(2 * (attempt + 1))
                continue
            raise
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            if attempt < retries:
                log.warning("api_json %s failed (%s), retrying", url, e)
                time.sleep(2 * (attempt + 1))
                continue
            raise


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

def _lb_get(url, referer=None):
    """Fetch a Letterboxd page with long, jittered 403 backoff.

    Returns (html|None, status) where status is one of:
      "ok"   — html returned
      "404"  — natural end of pagination
      "403"  — blocked, gave up after LB_403_BACKOFF retries (caller should
               treat the scrape as *truncated*, not complete)
      "error"— transport/other error
    """
    for back in [0] + LB_403_BACKOFF:
        if back:
            log.warning("letterboxd 403 on %s — backing off %ds", url, back)
            time.sleep(back + random.uniform(0, back * 0.3))
        try:
            return http_get(url, referer=referer), "ok"
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None, "404"
            if e.code == 403:
                continue
            log.error("letterboxd %s: HTTP %d", url, e.code)
            return None, "error"
        except Exception as e:
            log.warning("letterboxd %s: %s", url, e)
            return None, "error"
    log.error("letterboxd %s: 403 after %d retries, aborting (partial)", url, len(LB_403_BACKOFF))
    return None, "403"


def _parse_grid(html, acc):
    """Parse a /films/ grid page; add NEW {slug: rating|None} to acc. Returns count added."""
    added = 0
    for item in re.findall(r'<li class="griditem">(.*?)</li>', html, re.DOTALL):
        sm = re.search(r'data-item-slug="([^"]+)"', item)
        if not sm:
            continue
        slug = sm.group(1)
        if slug in acc:
            continue
        rm = re.search(r'rated-(\d+)', item)
        acc[slug] = (int(rm.group(1)) / 2.0) if rm else None
        added += 1
    if added == 0:  # fallback for markup changes
        for slug in re.findall(r'data-target-link="/film/([^"/]+)/"', html):
            if slug not in acc:
                acc[slug] = None
                added += 1
    return added


def _parse_diary(html):
    """Return diary slugs in document order (newest-watched first)."""
    out, seen = [], set()
    for m in re.finditer(r'data-(?:film|item)-slug="([^"]+)"|data-target-link="/film/([^"/]+)/"', html):
        slug = m.group(1) or m.group(2)
        if slug and slug not in seen:
            seen.add(slug)
            out.append(slug)
    return out


def _scrape_films_full():
    """Walk every page of /{user}/films/. Returns (slug_to_rating, aborted)."""
    acc = {}
    base = f"https://letterboxd.com/{LB_USER}/films/"
    page = 1
    while True:
        html, status = _lb_get(f"{base}page/{page}/", referer=base)
        if status == "403":
            return acc, True               # truncated
        if html is None:
            break                          # 404/error → natural end
        added = _parse_grid(html, acc)
        if added == 0:
            break
        log.info("letterboxd films page %d: +%d (total %d)", page, added, len(acc))
        page += 1
        time.sleep(LB_PAGE_SLEEP + random.uniform(0, LB_PAGE_SLEEP * 0.5))
    return acc, False


def _scrape_recent(prev_slugs):
    """Incremental: collect newly-watched slugs up to the last-seen point.

    Walks the diary (newest-first) and stops as soon as a page contributes no
    slugs we haven't already seen. Falls back to the first /films/ pages only if
    the diary is unreachable. Returns (new_slug_to_rating, aborted)."""
    new = {}
    base = f"https://letterboxd.com/{LB_USER}/films/diary/"
    diary_ok = False
    for page in range(1, LB_INCREMENTAL_MAX_PAGES + 1):
        url = base if page == 1 else f"{base}page/{page}/"
        html, status = _lb_get(url, referer=base)
        if status == "403":
            return new, True               # truncated — keep what we got
        if html is None:
            diary_ok = True
            break
        diary_ok = True
        page_slugs = _parse_diary(html)
        if not page_slugs:
            break
        fresh = [s for s in page_slugs if s not in prev_slugs and s not in new]
        for s in fresh:
            new[s] = None                  # rating backfilled by the periodic full reconcile
        log.info("letterboxd diary page %d: %d entries, +%d new", page, len(page_slugs), len(fresh))
        if not fresh:                      # reached previously-seen territory
            break
        time.sleep(LB_PAGE_SLEEP + random.uniform(0, LB_PAGE_SLEEP * 0.5))

    if diary_ok:
        return new, False

    log.warning("letterboxd diary unreachable — falling back to /films/ front pages")
    base2 = f"https://letterboxd.com/{LB_USER}/films/"
    for page in range(1, 3):
        html, status = _lb_get(f"{base2}page/{page}/", referer=base2)
        if status == "403":
            return new, True
        if html is None:
            break
        tmp = {}
        _parse_grid(html, tmp)
        fresh = {s: r for s, r in tmp.items() if s not in prev_slugs and s not in new}
        if not fresh and page > 1:
            break
        new.update(fresh)
        time.sleep(LB_PAGE_SLEEP + random.uniform(0, LB_PAGE_SLEEP * 0.5))
    return new, False


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _full_rescan_due(ts):
    if not ts:
        return True
    try:
        dt = datetime.strptime(ts, "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc)
    except Exception:
        return True
    return (datetime.now(timezone.utc) - dt).days >= LB_FULL_RESCAN_DAYS


def scrape_letterboxd_movies():
    """Return (watched, ratings, complete).

      watched:  {tmdb_id: slug}   ratings: {tmdb_id: float}
      complete: False if Letterboxd 403-truncated this run (caller should treat
                the result as possibly missing brand-new films, not authoritative)

    Watched slugs accumulate in LB_WATCHED_CACHE and are only ever *unioned* in,
    so a rate-limited run can never shrink the set. The set is rebuilt from
    scratch (allowing removals) only on a successful, complete periodic full walk.
    """
    if not LB_USER:
        log.info("LETTERBOXD_USER not set, skipping Letterboxd")
        return {}, {}, True

    cache = load_json(LB_WATCHED_CACHE, {})
    if not isinstance(cache, dict):
        cache = {}
    prev = cache.get("slugs", {}) if isinstance(cache.get("slugs"), dict) else {}
    last_full = cache.get("full_scrape_ts")

    force_full = (not prev) or (not LB_INCREMENTAL) \
        or _full_rescan_due(last_full) or _cfg_bool("LB_FORCE_FULL", False)

    complete = True
    if force_full:
        why = ("first run" if not prev else
               "incremental disabled" if not LB_INCREMENTAL else "periodic reconcile")
        log.info("letterboxd: full scrape (%s)", why)
        full_sr, aborted = _scrape_films_full()
        if aborted:
            merged = dict(prev)            # union — don't lose prior watches
            merged.update(full_sr)
            complete = False               # leave full_scrape_ts so we retry full next run
        else:
            merged = full_sr               # authoritative — allows pruning un-logged films
            last_full = _now_iso()
    else:
        log.info("letterboxd: incremental scrape (%d known)", len(prev))
        new_sr, aborted = _scrape_recent(prev)
        merged = dict(prev)
        merged.update(new_sr)
        complete = not aborted
        log.info("letterboxd incremental: +%d new (total known %d)", len(new_sr), len(merged))

    save_json(LB_WATCHED_CACHE, {"slugs": merged, "full_scrape_ts": last_full})

    slug_cache = load_json(SLUG_CACHE, {})
    watched, ratings = {}, {}
    for slug, rating in merged.items():
        tmdb = _resolve_slug(slug, slug_cache)
        if tmdb is not None:
            watched[tmdb] = slug
            if rating is not None:
                ratings[tmdb] = rating
    save_json(SLUG_CACHE, slug_cache)
    log.info("letterboxd: %d/%d watched slugs resolved, %d rated, complete=%s",
             len(watched), len(merged), len(ratings), complete)
    return watched, ratings, complete


def scrape_letterboxd_likes():
    """Scrape liked/favorited films from Letterboxd profile."""
    if not LB_USER:
        return set()
    liked_slugs = set()
    base_url = f"https://letterboxd.com/{LB_USER}/likes/films/"
    page = 1
    while True:
        html_text, status = _lb_get(f"{base_url}page/{page}/", referer=base_url)
        if html_text is None:
            break
        page_slugs = re.findall(r'data-item-slug="([^"]+)"', html_text)
        if not page_slugs:
            page_slugs = re.findall(r'data-target-link="/film/([^"/]+)/"', html_text)
        new = [s for s in page_slugs if s not in liked_slugs]
        if not new:
            break
        liked_slugs.update(new)
        log.info("letterboxd likes page %d: %d films (total %d)",
                 page, len(new), len(liked_slugs))
        page += 1
        time.sleep(LB_PAGE_SLEEP + random.uniform(0, LB_PAGE_SLEEP * 0.5))

    slug_cache = load_json(SLUG_CACHE, {})
    liked_tmdb = set()
    for slug in liked_slugs:
        tmdb = _resolve_slug(slug, slug_cache)
        if tmdb is not None:
            liked_tmdb.add(tmdb)
    save_json(SLUG_CACHE, slug_cache)
    log.info("letterboxd likes: %d/%d resolved to TMDB IDs",
             len(liked_tmdb), len(liked_slugs))
    return liked_tmdb


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
# Simkl API — follows official sync protocol (Phase 1 + Phase 2)
# ---------------------------------------------------------------------------

SIMKL_BASE = "https://api.simkl.com"
SIMKL_ACTIVITY_FILE = DATA_DIR / "simkl_activity.json"
SIMKL_CACHE_FILE = DATA_DIR / "simkl_cache.json"
APP_NAME = "trimbin"
APP_VERSION = "1.0"


def simkl_headers():
    return {
        "Content-Type": "application/json",
        "simkl-api-key": SIMKL_CLIENT_ID,
        "Authorization": f"Bearer {SIMKL_TOKEN}",
        "User-Agent": f"Trimbin/{APP_VERSION}",
    }


def simkl_url(path):
    sep = "&" if "?" in path else "?"
    return (f"{SIMKL_BASE}{path}{sep}"
            f"client_id={SIMKL_CLIENT_ID}&app-name={APP_NAME}&app-version={APP_VERSION}")


def simkl_get(path, timeout=60):
    return api_json(simkl_url(path), headers=simkl_headers(), timeout=timeout)


def _simkl_fetch_activities():
    return api_json(simkl_url("/sync/activities"),
                    headers=simkl_headers(), method="POST")


def _simkl_needs_sync(activity_data, saved):
    """Check if any watched timestamps changed since last sync."""
    for category in ("movies", "shows", "anime"):
        cat_data = activity_data.get(category, {})
        new_ts = cat_data.get("watched_at") or cat_data.get("all")
        old_ts = saved.get(f"{category}_watched_at", "")
        if new_ts and new_ts != old_ts:
            return True
    return False


def _parse_simkl_movies(data):
    watched = {}
    for item in data.get("movies", []):
        ids = item.get("movie", {}).get("ids", {})
        tmdb = ids.get("tmdb")
        if tmdb:
            watched[int(tmdb)] = item["movie"].get("title", "")
    return watched


def _parse_simkl_shows(data, item_type="shows"):
    shows = {}
    for item in data.get(item_type, []):
        if item.get("status") not in ("completed", None):
            continue
        show_obj = item.get("show", item.get("anime", {}))
        ids = show_obj.get("ids", {})
        tvdb = ids.get("tvdb")
        if not tvdb:
            continue
        tvdb = int(tvdb) if isinstance(tvdb, str) else tvdb
        watched_eps = {}
        for season in item.get("seasons", []):
            snum = season["number"]
            watched_eps[snum] = len(season.get("episodes", []))
        total = sum(watched_eps.values())
        if total == 0:
            total = item.get("watched_episodes_count", 0) or 0
        if tvdb in shows and shows[tvdb]["total_watched_eps"] >= total:
            continue
        shows[tvdb] = {
            "title": show_obj.get("title", ""),
            "year": show_obj.get("year"),
            "tmdb": ids.get("tmdb"),
            "watched_seasons": watched_eps,
            "total_watched_eps": total,
        }
    return shows


def _merge_shows(base, extra):
    """Merge `extra` shows into `base`, keeping whichever record has more watched episodes."""
    for tvdb, rec in extra.items():
        cur = base.get(tvdb)
        if not cur or rec.get("total_watched_eps", 0) > cur.get("total_watched_eps", 0):
            base[tvdb] = rec
    return base


def _simkl_initial_sync():
    """Phase 1 full sync: fetch each type, write cache + activity timestamps, return movies."""
    log.info("simkl: initial sync (Phase 1)")
    movies = _parse_simkl_movies(simkl_get("/sync/all-items/movies/completed") or {})
    time.sleep(1)
    shows = _parse_simkl_shows(simkl_get("/sync/all-items/shows/completed?extended=full") or {}, "shows")
    time.sleep(1)
    _merge_shows(shows, _parse_simkl_shows(simkl_get("/sync/all-items/anime/completed?extended=full") or {}, "anime"))

    save_json(SIMKL_CACHE_FILE, {"movies": {str(k): v for k, v in movies.items()},
                                 "shows": {str(k): v for k, v in shows.items()}})

    activity = _simkl_fetch_activities()
    saved = {"initialized": True}
    for cat in ("movies", "shows", "anime"):
        cat_data = activity.get(cat, {})
        saved[f"{cat}_watched_at"] = cat_data.get("watched_at") or cat_data.get("all") or ""
    save_json(SIMKL_ACTIVITY_FILE, saved)

    log.info("simkl initial: %d movies, %d shows", len(movies), len(shows))
    return movies


def fetch_simkl_movies():
    if not SIMKL_CLIENT_ID or not SIMKL_TOKEN:
        log.info("SIMKL_CLIENT_ID/SIMKL_ACCESS_TOKEN not set, skipping Simkl")
        return {}

    saved = load_json(SIMKL_ACTIVITY_FILE, {})
    cache = load_json(SIMKL_CACHE_FILE, {"movies": {}, "shows": {}})

    if not saved.get("initialized"):
        return _simkl_initial_sync()

    # Phase 2: check activity, delta sync if changed
    activity = _simkl_fetch_activities()
    if not _simkl_needs_sync(activity, saved):
        log.info("simkl: no changes since last sync, using cache")
        return {int(k): v for k, v in cache.get("movies", {}).items()}

    date_from = saved.get("movies_watched_at") or saved.get("shows_watched_at") or ""
    if not date_from:
        log.info("simkl: no saved timestamp, falling back to initial sync")
        return _simkl_initial_sync()

    log.info("simkl: delta sync from %s", date_from)
    data = simkl_get(f"/sync/all-items/?date_from={urllib.parse.quote(date_from)}&extended=full")

    delta_movies = _parse_simkl_movies(data or {})
    delta_shows = _parse_simkl_shows(data or {}, "shows")
    delta_anime = _parse_simkl_shows(data or {}, "anime")
    _merge_shows(delta_shows, delta_anime)

    # Merge into cache
    for k, v in delta_movies.items():
        cache.setdefault("movies", {})[str(k)] = v
    for k, v in delta_shows.items():
        cache.setdefault("shows", {})[str(k)] = v
    save_json(SIMKL_CACHE_FILE, cache)

    for cat in ("movies", "shows", "anime"):
        cat_data = activity.get(cat, {})
        ts = cat_data.get("watched_at") or cat_data.get("all") or ""
        if ts:
            saved[f"{cat}_watched_at"] = ts
    save_json(SIMKL_ACTIVITY_FILE, saved)

    movies = {int(k): v for k, v in cache.get("movies", {}).items()}
    log.info("simkl: %d total movies (delta: %d new), %d delta shows",
             len(movies), len(delta_movies), len(delta_shows))
    return movies


def fetch_simkl_shows():
    """Returns shows from Simkl cache (populated by fetch_simkl_movies)."""
    if not SIMKL_CLIENT_ID or not SIMKL_TOKEN:
        return {}
    cache = load_json(SIMKL_CACHE_FILE, {"movies": {}, "shows": {}})
    shows = {int(k): v for k, v in cache.get("shows", {}).items()}
    log.info("simkl: %d cached shows", len(shows))
    return shows


# ---------------------------------------------------------------------------
# Trakt API (kept for future use — not called by default)
# ---------------------------------------------------------------------------

def _trakt_headers():
    client_id = os.environ.get("TRAKT_CLIENT_ID", "")
    return {
        "Content-Type": "application/json",
        "trakt-api-version": "2",
        "trakt-api-key": client_id,
    }


def _fetch_trakt_movies():
    """Fetch watched movies from Trakt. Not used — enable by calling in main()."""
    client_id = os.environ.get("TRAKT_CLIENT_ID", "")
    username = os.environ.get("TRAKT_USERNAME", "")
    if not client_id or not username:
        return {}
    url = f"https://api.trakt.tv/users/{username}/watched/movies"
    data = api_json(url, headers=_trakt_headers())
    watched = {}
    for item in data:
        tmdb = item.get("movie", {}).get("ids", {}).get("tmdb")
        if tmdb:
            watched[tmdb] = item["movie"]["title"]
    return watched


def _fetch_trakt_shows():
    """Fetch watched shows from Trakt. Not used — enable by calling in main()."""
    client_id = os.environ.get("TRAKT_CLIENT_ID", "")
    username = os.environ.get("TRAKT_USERNAME", "")
    if not client_id or not username:
        return {}
    url = f"https://api.trakt.tv/users/{username}/watched/shows"
    data = api_json(url, headers=_trakt_headers())
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
            "title": item.get("show", {}).get("title", ""),
            "year": item.get("show", {}).get("year"),
            "tmdb": ids.get("tmdb"),
            "watched_seasons": watched_eps,
            "total_watched_eps": sum(watched_eps.values()),
        }
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

JELLYFIN_URL = _cfg("JELLYFIN_URL").rstrip("/")
JELLYFIN_API_KEY = _cfg("JELLYFIN_API_KEY")
JELLYFIN_USER_ID = _cfg("JELLYFIN_USER_ID")  # account whose Jellyfin watches count as "watched"


def fetch_jellyfin_watched_movies():
    """Movies marked Played on the configured Jellyfin user account, as {tmdb_id: title}.

    Makes a Jellyfin watch a first-class 'watched' source so films watched only in
    Jellyfin (never logged to Letterboxd/Simkl) are still detected. Without this,
    Jellyfin/Jellystat are badge-only and such watches are invisible to Trimbin.
    """
    out = {}
    if not (JELLYFIN_URL and JELLYFIN_API_KEY and JELLYFIN_USER_ID):
        return out
    try:
        url = (f"{JELLYFIN_URL}/Users/{JELLYFIN_USER_ID}/Items?isPlayed=true"
               f"&includeItemTypes=Movie&recursive=true&fields=ProviderIds")
        data = api_json(url, headers={"X-Emby-Token": JELLYFIN_API_KEY})
        for item in data.get("Items", []):
            tmdb = item.get("ProviderIds", {}).get("Tmdb")
            if tmdb:
                try:
                    out[int(tmdb)] = item.get("Name", "")
                except (TypeError, ValueError):
                    pass
    except Exception as e:
        log.warning("jellyfin watched-movies query failed: %s", e)
    return out


def jellyfin_lookup_by_tmdb(tmdb_id):
    """Find a Jellyfin item ID by TMDB provider ID."""
    if not JELLYFIN_URL or not JELLYFIN_API_KEY:
        return None
    try:
        url = (f"{JELLYFIN_URL}/Items?hasTmdbId=true"
               f"&fields=ProviderIds&recursive=true&includeItemTypes=Movie")
        data = api_json(url, headers={"X-Emby-Token": JELLYFIN_API_KEY})
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
                "size_gb": max(0.1, round(m["sizeOnDisk"] / (1024 ** 3), 1)),
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
        tvdb = s.get("tvdbId")
        if not tvdb:
            continue  # unmatched series — don't collapse them all under key 0
        stats = s.get("statistics", {})
        if stats.get("episodeFileCount", 0) > 0:
            shows[tvdb] = {
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

def _scan():
    # Read auto-ignore settings early so we only scrape likes if needed
    auto_ignore_liked = _cfg("LB_AUTO_IGNORE_LIKED").lower() in ("true", "1", "yes", "on")
    min_rating_str = _cfg("LB_MIN_RATING_IGNORE")
    min_rating = None
    if min_rating_str:
        try:
            min_rating = float(min_rating_str)
            if not (0.5 <= min_rating <= 5.0):
                min_rating = None
        except ValueError:
            min_rating = None

    # ---- Movies ----
    lb_watched, lb_ratings, lb_complete = scrape_letterboxd_movies()
    if not lb_complete:
        log.warning("letterboxd scrape was TRUNCATED (rate-limited) — watched set "
                    "preserved from cache, but brand-new films may be missing this run")
    lb_likes = scrape_letterboxd_likes() if auto_ignore_liked else set()
    simkl_watched = fetch_simkl_movies()
    trakt_watched = _fetch_trakt_movies()  # no-op unless TRAKT_CLIENT_ID + TRAKT_USERNAME are set
    jf_watched = fetch_jellyfin_watched_movies()  # no-op unless JELLYFIN_USER_ID is set

    all_watched_tmdb = (set(lb_watched.keys()) | set(simkl_watched.keys())
                        | set(trakt_watched.keys()) | set(jf_watched.keys()))
    log.info("total unique watched movies: %d (LB=%d, Simkl=%d, Trakt=%d, Jellyfin=%d)",
             len(all_watched_tmdb), len(lb_watched), len(simkl_watched), len(trakt_watched), len(jf_watched))

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
            if tmdb_id in simkl_watched:
                sources.append("simkl")
            if tmdb_id in trakt_watched:
                sources.append("trakt")
            if tmdb_id in jf_watched:
                sources.append("jellyfin")

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
                "new": (not first_run) and (tmdb_id not in notified),
                "sources": sources,
                "watch_count": watch_count,
                "total_users": total_users,
                "watched_by": watched_by,
                "rating": lb_ratings.get(tmdb_id),
            })
    watched_on_disk.sort(key=lambda x: x["size_gb"], reverse=True)

    # ---- Auto-ignore liked/highly-rated films ----
    if auto_ignore_liked or min_rating is not None:
        existing_ignored = set(load_json(IGNORED_FILE, []))
        auto_ignored = load_json(AUTO_IGNORED_FILE, {})
        auto_count = 0
        for m in watched_on_disk:
            tmdb = m["tmdb_id"]
            if tmdb in existing_ignored:
                continue
            prev = auto_ignored.get(str(tmdb))
            if prev and prev.get("restored"):
                continue
            reason = None
            if auto_ignore_liked and tmdb in lb_likes:
                reason = "liked"
            if min_rating is not None and tmdb in lb_ratings and lb_ratings[tmdb] >= min_rating:
                reason = f"rated {lb_ratings[tmdb]:g}"
            if reason:
                existing_ignored.add(tmdb)
                auto_ignored[str(tmdb)] = {"reason": reason}
                auto_count += 1
                log.info("auto-ignored: %s (%s)", m["title"], reason)
        if auto_count:
            save_json(IGNORED_FILE, sorted(existing_ignored))
            save_json(AUTO_IGNORED_FILE, auto_ignored)
            log.info("auto-ignored %d movies this scan", auto_count)

    # Preserve ignored movies — they may no longer be in Radarr or watch sources
    # but must stay in the list so the UI can render them in the ignored section
    ignored_ids = set(load_json(IGNORED_FILE, []))
    active_tmdb_ids = {m["tmdb_id"] for m in watched_on_disk}
    if ignored_ids - active_tmdb_ids:
        prev_list = load_json(WATCHED_LIST_FILE, [])
        for m in prev_list:
            if m.get("tmdb_id") in ignored_ids and m["tmdb_id"] not in active_tmdb_ids:
                watched_on_disk.append(m)

    total_count = len(watched_on_disk)
    total_gb = sum(m["size_gb"] for m in watched_on_disk)
    new_watches = [m for m in watched_on_disk if m["new"]]
    new_gb = sum(m["size_gb"] for m in new_watches)

    log.info("movies on disk: %d (%.0f GB), %d new", total_count, total_gb, len(new_watches))

    # ---- Shows ----
    simkl_shows = fetch_simkl_shows()
    _merge_shows(simkl_shows, _fetch_trakt_shows())  # Trakt shows — no-op without TRAKT creds
    sonarr = get_sonarr_library()
    log.info("sonarr: %d shows with files on disk", len(sonarr))

    shows_on_disk = []
    for tvdb_id, simkl_info in simkl_shows.items():
        if tvdb_id in sonarr:
            show = sonarr[tvdb_id]
            watched_eps = simkl_info["total_watched_eps"]
            total_eps = show["episode_count"]
            pct = round(100 * watched_eps / total_eps) if total_eps > 0 else 0
            if pct < 75:
                continue
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
                "watched_seasons": simkl_info["watched_seasons"],
            })
    shows_on_disk.sort(key=lambda x: x["size_gb"], reverse=True)

    # Preserve ignored shows
    ignored_show_ids = set(load_json(IGNORED_SHOWS_FILE, []))
    active_tvdb_ids = {s["tvdb_id"] for s in shows_on_disk}
    if ignored_show_ids - active_tvdb_ids:
        prev_shows = load_json(SHOWS_LIST_FILE, [])
        for s in prev_shows:
            if s.get("tvdb_id") in ignored_show_ids and s["tvdb_id"] not in active_tvdb_ids:
                shows_on_disk.append(s)

    shows_count = len(shows_on_disk)
    shows_gb = sum(s["size_gb"] for s in shows_on_disk)
    log.info("shows on disk: %d (%.0f GB)", shows_count, shows_gb)

    # ---- Write data ----
    write_status(total_count, total_gb, len(new_watches), new_gb, shows_count, shows_gb,
                 lb_ok=lb_complete)
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


def write_status(movie_count, movie_gb, new_count, new_gb, show_count=0, show_gb=0, lb_ok=True):
    save_json(STATUS_FILE, {
        "watched_on_disk": movie_count,
        "total_gb": round(movie_gb),
        "new_since_last": new_count,
        "new_gb": round(new_gb),
        "shows_on_disk": show_count,
        "shows_gb": round(show_gb),
        "lb_scrape": "ok" if lb_ok else "degraded",
        "last_run": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    })


def main():
    ping_hc("start")
    try:
        _scan()
    except Exception:
        log.exception("trimbin scan failed")
        ping_hc("fail")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
