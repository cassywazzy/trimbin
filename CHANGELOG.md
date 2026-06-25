# Changelog

All notable changes to Trimbin are documented here. Newest entries on top.

## 2026-06-25 — v2.10

- **Letterboxd scrape: incremental diary sync + rate-limit resilience** — Letterboxd rate-limits (HTTP 403) profile scraping once a scan walks too many pages of your films grid. The old scraper retried only briefly, then aborted mid-pagination, and the watch-list scan overwrote its results with the truncated set — so a recently-watched film could silently drop off the list until a later scan happened to catch it (or until another watch source picked it up). Three changes fix this:
  - **Watched films now accumulate** in `lb_watched_cache.json` and are only ever merged in, never replaced wholesale — so a rate-limited (partial) scrape can no longer shrink your watched list. A full rebuild that can prune films you've un-logged happens only when a complete walk succeeds, and at most every `LB_FULL_RESCAN_DAYS` (default 14).
  - **Incremental diary sync** — routine scans read your diary (newest first) and stop at the last film already seen, instead of walking every page of the films grid. This sharply cuts the request volume that triggers the rate-limit. Controlled by `LB_INCREMENTAL` (default on) and `LB_INCREMENTAL_MAX_PAGES` (default 5); falls back to the films grid if the diary is unreachable.
  - **Longer, jittered backoff** — `LB_PAGE_SLEEP` (default 6 s) between pages, and a 20/60/120 s backoff on a 403, so a transient block is ridden out rather than aborting the scrape.
- **Truncated scrapes are now visible** — when a run is cut short by rate-limiting, the scanner logs a warning and sets `lb_scrape: "degraded"` in the status JSON instead of silently reporting success.
- New config: `LB_INCREMENTAL`, `LB_INCREMENTAL_MAX_PAGES`, `LB_FULL_RESCAN_DAYS`, `LB_PAGE_SLEEP`, `LB_FORCE_FULL`.
- **Correct Letterboxd links** — movie titles in the UI now link via `https://letterboxd.com/tmdb/<id>/` (which redirects to the exact film) instead of a slug guessed from the title. Films whose title collides with another (Letterboxd disambiguates the slug, e.g. with a `-<year>` suffix) or contains punctuation previously opened the wrong listing.

## 2026-06-09 — v2.9

- **New "Torrent Orphans" tab** — Finds files in the qBittorrent download root that no torrent points at anymore (failed imports, removed-but-kept torrents, manual leftovers) by diffing the download dir against every torrent's `content_path`. Crucially, it records **both the real on-disk size (`st_blocks`) and the apparent/allocated size (`st_size`)** — qBittorrent pre-allocates the full file size, so an abandoned download can report as 30 GB while occupying ~18 MB on disk. The list shows real size (with the allocated size noted when it's materially larger), sorted and totalled by real usage. Configure with `QBIT_DOWNLOAD_ROOT` (the download root as the container sees it) and a bind mount of that directory.
- **Two safety tiers** — **SAFE** (failed/sparse partials + sub-1 MB stubs) and **CAUTION** (complete untracked content — the only download copy, which may already be imported to your library). Per-item and per-tier delete.
- **Guarded, re-verified deletes** — Every orphan delete is realpath-contained to the download root **and** re-checked against qBittorrent's live torrent list at delete time: it refuses to delete anything a torrent now claims, and refuses entirely if qBittorrent can't be reached. New endpoints `POST /api/scan-orphans`, `POST /api/orphan-delete/<hash>`, `POST /api/orphan-delete-tier/<tier>`.

## 2026-06-04 — v2.8

- **qBittorrent integration (trim also removes the torrent)** — Trimming a movie or show now also deletes the torrent your *arr app grabbed for it from qBittorrent, reclaiming the seeding copy in the download directory (not just the imported library copy). The torrent is matched by **hash**: the download ID Radarr/Sonarr records in its history *is* the qBittorrent torrent hash, so only torrents the *arr app actually grabbed for the trimmed item are touched — manual torrents (private trackers, etc.) are never matched. Best-effort and non-fatal: a qBittorrent hiccup never undoes the *arr-side trim, and the result is surfaced in the success toast (e.g. "qBit: removed 1 torrent (+65.0 GB)").
- **Configurable in Settings** — New **qBittorrent** settings group: `QBIT_URL` (WebUI URL, container-reachable), `QBIT_USERNAME` / `QBIT_PASSWORD` (leave the username blank if qBittorrent trusts the host via `bypass_auth_subnet_whitelist`), and `QBIT_DELETE_FILES` (delete the downloaded files too — default — or remove the torrent listing only). When `QBIT_URL` is unset the integration stays completely dormant, so existing installs are unaffected.
- **Stacked scan progress** — Running more than one scan at once no longer makes the progress bar jump erratically between them. Each scan now writes its own progress record and the panel **stacks a bar per running scan**, each with its own label, count, and current directory. Progress is server-authoritative (per-type files aggregated by `/api/scan-progress`, with a 20 s stale-filter), so a scan finishing no longer clears another's bar, and an in-flight scan re-appears after the page reloads.
- **Transcode tab: Tdarr → Unmanic** — The Tdarr tab is now an **Unmanic** tab (`UNMANIC_URL` / `UNMANIC_BROWSER_URL`), pulling live from Unmanic's API (`history/tasks`, `pending/tasks`, `workers/status`), cached to disk and offline-tolerant. Cards: Transcodes, Failed, Queued, Workers busy — plus a **recent-transcodes list with real filenames** (`task_label`), which Tdarr couldn't provide. Note: Unmanic's history stores no before/after file sizes, so there's no "space saved" figure (the inverse trade from Tdarr, which had the number but no filenames).

## 2026-06-01 — v2.7

- **Live scan progress** — Long scans (especially the Storage Explorer tree scan) now show a live progress panel — a bar, a running count, and the directory currently being scanned — instead of a dead spinner. The scanners write a throttled `scan_progress.json`; the UI polls `GET /api/scan-progress`. The tree scan estimates a percentage from the previous scan's directory count; the others show an indeterminate bar with a live counter.
- **Optional nightly auto-scan** — Set `AUTO_SCAN_TIME` (HH:MM) to run the watch-list scan automatically each day; enable `AUTO_SCAN_STORAGE` to also run the dedup/trickplay/cleanup/tree scans. Off by default (the storage scans are I/O-heavy).
- **Context-aware Explorer action menu** — The treemap action menu now looks an item up in Radarr/Sonarr first and offers only what fits: a managed movie/show gets **Change quality** + **Trim** (which removes it from *arr so it won't re-download); unmanaged media gets **Delete from disk**. This removes the footgun of disk-deleting a managed item (which would just re-download).
- **Honest Tdarr tab** — Replaced the per-event "recent transcodes" list (which had no filenames and was dominated by setup/test runs) with a per-library savings breakdown and a clear note: Tdarr's savings log records space saved per transcode but **not** the filename, and its per-file database is empty until Tdarr transcodes in production. The aggregate cards now read "Transcode events" rather than "Transcodes."

## 2026-06-01 — v2.6

- **Tdarr tab** — New tab surfacing your Tdarr transcode activity: total space saved, transcode count, files tracked, health-check count, and a recent-transcodes list (per-event savings + library). Pulls live from Tdarr's HTTP API (`/api/v2/cruddb`) and caches the last sync, so it keeps showing numbers even when Tdarr is offline (it degrades to a clear "offline — last sync" badge). Configure with `TDARR_URL` (+ optional `TDARR_BROWSER_URL` for the "Open Tdarr" link).
- **Storage pool health widget** — An always-visible capacity gauge in the header: how full the media pool is (used / free / %), color-coded green < 80 % / amber 80–90 % / red > 90 %, refreshed on load and on demand. Reads exact figures via `statvfs` on each media library (deduped by filesystem); optional best-effort Netdata enrichment via `NETDATA_URL`.
- **Explorer action menu** — Clicking a media cell in the treemap now opens an action menu (Change quality profile / Trim / Delete from disk) instead of jumping straight to the quality dropdown.
- **Entry-level Explorer actions + delete-from-disk** — Trim/Quality actions now appear only on actual movie/show *entries* (one level below a library root), not on season folders, disc folders, or `.trickplay` subdirectories that were never *arr-managed. Media that isn't tracked by Radarr/Sonarr (e.g. a manually-added remux) can now be removed directly from disk via the same realpath-containment guard (`POST /api/explorer/delete`, with a refusal to ever delete a library root).
- **Update checker** — A version badge in the header checks the GitHub project (`/releases/latest`, falling back to tags) and shows "update available" when a newer version exists. Cached ~6 h; points only at the public repo (configurable via `GITHUB_REPO`), never at any private server — so it works for anyone self-hosting.
- **Settings: Ollama fields now visible** — The AI (Ollama) settings group was defined but never rendered in the Settings form; it now appears alongside new Tdarr and Updates groups.

## 2026-05-29

- **Maintenance / robustness pass** — Shared `trimbin_common.py` (single source of truth for the file-type extension sets, config loading, recursive dir sizing, and atomic JSON writes — the scanners' sets had drifted out of sync). Scans are now crash-safe: a failed Radarr/Sonarr/Simkl call fires the Healthchecks alert instead of aborting silently, and `api_json` retries transient 5xx/timeout/connection errors. Simkl show de-dup now keeps the richer record across movie/show/anime categories, skips Sonarr series with no TVDB id, drops a fragile recursive re-sync, and URL-encodes the delta timestamp. Jellyfin lookups send the API key as a header (`X-Emby-Token`) rather than a query param. Trakt wired in as an optional, env-gated watch source. All scanners write their JSON atomically.
- **Storage Explorer + AI recommendations (v2.4)** — New "Explorer" tab: a D3 zoomable treemap + sortable tree list over your media libraries with four view modes (By Size / Age / Type / AI), plus optional local-LLM (Ollama) keep/delete recommendations driven by a taste profile built from your Radarr/Sonarr genres and trim history. Inline Trim and quality-profile controls per item. Requires `MEDIA_LIBRARIES`.
- **Cross-library duplicate detection for TV/anime** — `dedup-scan.py` now finds the same series duplicated across libraries (e.g. a show in both `/tv` and `/anime`) via Sonarr TVDB IDs, alongside the existing Radarr/TMDB movie dedup. Adds audio-format detection (FLAC/AAC/TrueHD/DTS) to tell otherwise-identical copies apart.
- **Explorer UX fixes** — trimming an item updates the view immediately (no stale "not found" on re-click); treemap leaf cells open the quality dropdown on click; view-mode buttons now re-sort the list (not just recolor); muted the color palette.
- **Hardened delete safety** — the path-deletion guard resolves symlinks/`..` and requires the real path to be *inside* a configured library (a plain prefix check could authorize a sibling like `/media/movies-4k`); symlinks are unlinked, never followed.
- **Portability** — `tree-scan.py` now honors `DATA_DIR` and tolerates malformed config like the other scanners; Explorer media-action detection derives from `MEDIA_LIBRARIES` instead of hardcoded paths; unreadable directories are no longer mistaken for empty/deletable; the Dockerfile now ships `tree-scan.py`; `.env.example` documents `MEDIA_LIBRARIES`, Ollama, browser URLs, digest time, and auto-ignore. First-run scans no longer report the whole library as "new."
- **Split API vs browser URLs** — `RADARR_BROWSER_URL` / `SONARR_BROWSER_URL` allow the in-container API URL to differ from the clickable links in the UI.
- **qBittorrent purge-on-trim** — trimming a movie/show also removes the matching torrent from qBittorrent.
- **Letterboxd auto-ignore** — optionally auto-ignore films you liked or rated at/above a threshold.

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
