# Changelog

All notable changes to Trimbin are documented here. Newest entries on top.

## 2026-05-21

- **Cleanup scanner** — New `cleanup-scan.py` scans media libraries for orphan subtitles, orphan images (metadata in empty movie dirs), OS junk (Thumbs.db, .DS_Store), sample files/dirs, and empty directories. Smart detection avoids flagging files that are part of media releases (anime extras, booklets, soundtrack scans).

- **Storage tab redesign** — Per-copy rows with quality/codec/source/HDR labels, individual Delete buttons (path-hash validated against media library paths), per-group Ignore for dedup, library grouping. Season directory filtering prevents TV season folders from appearing as duplicates.

## 2026-05-20

- **Storage scans tab** — New "Storage" tab with duplicate movie detection and trickplay/BIF directory scanning. Each has its own Scan button. Requires `MEDIA_LIBRARIES` setting (comma-separated paths). Dedup scan identifies same-title-year movies across libraries; trickplay scan flags oversized or video-containing trickplay dirs.

- **Ignored items preserved across scans** — Scanner now preserves ignored movies and shows in the data files even if they're no longer returned by watch sources or arr APIs. Previously, ignored items could silently vanish after a rescan.

- **75% watch threshold for shows** — Shows below 75% watched are filtered out of the main list. Prevents partial-watch shows from cluttering the UI.

- **Tab persistence** — Ignore/Restore actions on shows no longer bounce back to the Movies tab. URL hash tracks the active tab across reloads.

- **Min 0.1 GB display** — Movies with very small files (< 50 MB) now show "0.1 GB" instead of "0.0 GB".

- **Show ignore feature** — Same pattern as movie ignore: Ignore/Restore buttons on each show row, greyed-out "Ignored" section at bottom of Shows tab, state persisted to `trimbin_ignored_shows.json`. API: `POST /api/ignore-show/<tvdb_id>`, `POST /api/unignore-show/<tvdb_id>`.

- **Simkl "completed" filter** — Phase 1 endpoints changed to `/sync/all-items/{type}/completed` to only pull finished content. Previously pulled all statuses including "watching" at 0%.

- **Episode count fix** — `total_episodes` now uses Sonarr's `episodeCount` (full series) instead of `episodeFileCount` (files on disk). Fixes shows like Attack on Titan showing "25/12 eps".

- **Episode count fallback** — When Simkl returns `watched_episodes_count` at the top level but no `seasons` array (e.g., Malcolm in the Middle), scanner now falls back to the top-level count instead of showing 0%.

- **Simkl integration** — Replaced Trakt with Simkl as watch tracking source (movies + shows + anime). Trakt code retained as unused functions for future use. Simkl uses Phase 1/Phase 2 sync protocol with local caching.

- **Settings page** — All integration keys configurable through the web UI. Saves to `trimbin_config.json`, env vars as fallback.

- **Scan button** — "Scan" button in the header triggers the scanner from the UI.

- **Daily trim digest** — If anything was trimmed during the day, posts a summary to Discord at the configured time (default 21:00). Configurable via `DIGEST_TIME` in Settings.

- **Logo** — Trimbin logo in header, favicon, and README.

## 2026-05-19

- **Trimbin v2: multi-source release** — Renamed from "Letterboxd Cleanup" / "Media Cleanup" to Trimbin. Scanner now merges watched lists from Letterboxd + Simkl (movies, shows, anime with episode progress), enriches with Jellystat per-user watch counts (via Jellyfin TMDB→ID mapping), cross-references both Radarr and Sonarr. Tabbed UI (Movies/Shows), source badges, watch count badges ("3/4 watched"), episode progress bars, movie ignore list, trim with confirmation dialog, Discord notifications, Homepage widget API.

- **Duplicates tab and System tab** — Dedup scanner found 12 groups (37.2 GB potential savings). System tab scans all LXCs for apt caches and journal logs with Clean/Vacuum buttons.

## 2026-05-18

- **Radarr→Jellyseerr delete sync** — Webhook fires on movie delete, triggers Jellyseerr's radarr-scan job for instant availability update.

- **Trickplay scanner** — New tab scanning media libraries for broken trickplay dirs (oversized >50 MB or containing video files). First scan: 8766 dirs, 49.54 GB total. Renamed UI to "Media Cleanup" with nav tabs.

## 2026-05-17

- **Cleanup notifier** — Weekly one-shot scanner scrapes watched films from Letterboxd, cross-references Radarr library, posts Discord digest of watched movies still on disk. First test: 288 films scraped, 21 watched movies on disk (181 GB).

## 2026-05-07

- **Quality profile upgrade** — Changed from `HD-1080p` (id 4) to `Remux + WEB 1080p` (id 9, TRaSH-managed via Recyclarr) for new adds.

## 2026-05-06

- **Healthchecks monitoring** — Dead-man's-switch via systemd timer on the host. Checks docker logs for cycle evidence, pings HC success/fail.

## 2026-05-04

- **Security hardening** — Per-cycle add cap (`MAX_ADDS_PER_CYCLE=10`), large-delta circuit breaker (`DELTA_ALERT_THRESHOLD=40`), Discord webhook alerts for both conditions.

## 2026-05-01

- **Initial deploy** — Letterboxd watchlist HTML scraping (CF blocks RSS). Forward-only baseline sync, two-layer dedup (Radarr library + added cache), slug→TMDB resolution with persistent cache. ~1316 slugs resolved on first run.
