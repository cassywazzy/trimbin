<p align="center">
  <img src="logo.png" alt="Trimbin" width="128">
</p>

# Trimbin

Media cleanup tool for the *arr ecosystem. Monitors your watched lists across multiple sources and reports which media is still on disk — then lets you trim it with one click.

## Features

- **Multi-source watch detection** — merges watched lists from Letterboxd, Simkl, and Jellystat
- **Movie + show support** — cross-references Radarr (movies) and Sonarr (shows)
- **Multi-user watch counts** — Jellystat integration shows "3/4 users watched" badges
- **One-click trim** — delete files, unmonitor in Radarr/Sonarr, and purge the qBittorrent listing from the web UI (with confirmation dialog)
- **Ignore list** — push movies to a greyed-out secondary list; restore them anytime
- **Tabbed UI** — separate Movies and Shows tabs with progress bars for episode completion
- **Storage Explorer** — zoomable D3 treemap + sortable tree list of your libraries, with By Size / Age / Type / AI views and optional local-LLM (Ollama) keep/delete recommendations
- **Duplicate finder** — flags the same movie or series stored more than once (including the same show across `/tv` and `/anime`), with quality / codec / audio / size shown per copy
- **Orphan & junk cleanup** — finds OS junk, empty dirs, scene/release leftovers, samples, and orphan sidecar files with a 3-tier safety model
- **Trickplay scan** — flags oversized or video-containing `.trickplay` directories
- **Tdarr stats** — a Tdarr tab with cumulative transcode savings, counts, and a recent-transcodes list (cached, so it still shows numbers when Tdarr is offline)
- **Storage pool health** — an always-visible header gauge showing how full the media pool is (used / free / %, color-coded by safety level)
- **Update checker** — a header badge that flags when a newer version is published on the GitHub project
- **Discord digest** — weekly notification of newly watched media still on disk
- **Status API** — JSON endpoint for dashboard widgets (Homepage, etc.)
- **Trimmed counter** — tracks cumulative disk space reclaimed

## Architecture

Two components in one container:

- **cleanup-notify.py** — one-shot scanner that runs on a schedule (cron/timer). Scrapes Letterboxd, queries Simkl/Jellystat/Jellyfin APIs, cross-references Radarr + Sonarr, writes status JSON, and posts to Discord.
- **status-server.py** — always-running HTTP server. Reads the JSON files and serves the web UI + API. Handles trim/ignore actions via Radarr/Sonarr APIs.

## Watch sources

| Source | What it provides | Auth |
|--------|-----------------|------|
| Letterboxd | Watched movies (scraped from profile) | Public profile, no key needed |
| Simkl | Watched movies + shows + anime with episode-level progress | OAuth token (never expires) |
| Trakt | Watched movies + shows (optional, activates only when configured) | API client ID + username |
| Jellystat | Per-user watch counts ("3 of 4 users watched") | API token |
| Jellyfin | TMDB-to-Jellyfin ID mapping (needed for Jellystat) | API key |

All sources are optional. Trimbin works with just Letterboxd + Radarr, and gains features as you add more integrations.

## Web UI

The status server provides a dark-themed dashboard at port 5380:

- Summary stats: movies on disk, reclaimable GB, shows on disk, new since last scan, total trimmed
- **Movies tab**: movie table with Letterboxd links, source badges, watch count badges, file sizes, and action buttons (Trim / Ignore / Radarr)
- **Shows tab**: show table with episode progress bars, watched percentage, file sizes, and action buttons (Trim / Sonarr)
- Confirmation dialog before any deletion
- Ignored movies section (greyed out, with Restore button)

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Web UI |
| GET | `/api/status` | JSON status for dashboard widgets |
| GET | `/ping` | Health check |
| POST | `/api/trim/<tmdb_id>` | Delete movie files + remove from Radarr + purge qBit torrent |
| POST | `/api/trim-show/<sonarr_id>` | Delete show files + remove from Sonarr + purge qBit torrent |
| POST | `/api/ignore/<tmdb_id>` | Add movie to ignore list |
| POST | `/api/unignore/<tmdb_id>` | Remove movie from ignore list |
| GET/POST | `/api/settings` | View/save integration settings |
| POST | `/api/scan` | Run the watch-list scanner now |
| POST | `/api/scan-dedup` · `/api/scan-trickplay` · `/api/scan-cleanup` · `/api/scan-tree` | Run a storage scan |
| GET | `/api/tree` | Cached tree-scan data for the Explorer treemap |
| GET | `/api/explorer/lookup?path=` · `/api/explorer/quality-profiles` | Resolve a path to Radarr/Sonarr · list quality profiles |
| POST | `/api/explorer/set-quality` | Change a movie/show quality profile (optionally search) |
| POST | `/api/delete-path/<hash>` | Delete a scanned file/dir (validated against `MEDIA_LIBRARIES`) |
| POST | `/api/explorer/delete` | Delete an arbitrary media path from disk (same realpath-containment guard) |
| GET | `/api/storage-health` | Media-pool capacity via `statvfs` (+ optional Netdata enrichment) |
| GET | `/api/tdarr/stats` | Tdarr transcode stats (cached; live via Tdarr's cruddb API) |
| GET | `/api/version` | Running version + latest on GitHub (update check) |
| POST | `/api/ai/analyze` | Ollama keep/delete recommendations for the current view |

## Security

Trimbin has **no built-in authentication**, and it can **delete files** (trim) and remove items from Radarr / Sonarr / qBittorrent. Treat it as an admin tool:

- **Don't expose it directly to the internet.** Put it behind a reverse proxy with auth (Authentik/Authelia, basic auth, a VPN/overlay like Tailscale or NetBird), or keep it on a trusted LAN.
- Deletions are guarded — a path is only removed if its resolved real path (symlinks and `..` resolved) is **inside** a configured `MEDIA_LIBRARIES` root, and symlinks are unlinked rather than followed — but still mount only the media you want Trimbin to manage.
- Secrets live in `.env` / the in-volume `trimbin_config.json`, never in the image. The provided `.gitignore` keeps `.env` and state JSON out of version control.

## Setup

### Docker (recommended)

```bash
git clone https://github.com/cassywazzy/trimbin.git
cd trimbin
cp .env.example .env
# Edit .env with your values
docker compose up -d
```

The status server starts automatically. To run the scan manually:

```bash
docker compose exec trimbin python cleanup-notify.py
```

For weekly scans, set up a cron job or systemd timer on the host:

```bash
docker compose exec trimbin python cleanup-notify.py
```

### Standalone

```bash
cp .env.example .env
# Edit .env
python status-server.py &    # Start the web UI
python cleanup-notify.py     # Run a scan
```

## Configuration

### Core (required)

| Variable | Description |
|----------|-------------|
| `LETTERBOXD_USER` | Your Letterboxd username |
| `RADARR_URL` | Radarr base URL (e.g. `http://radarr:7878`) |
| `RADARR_API_KEY` | Radarr API key (Settings > General) |

### Simkl integration

| Variable | Description |
|----------|-------------|
| `SIMKL_CLIENT_ID` | Simkl API client ID ([create app](https://simkl.com/settings/developer/new/)) |
| `SIMKL_ACCESS_TOKEN` | OAuth access token (use PIN auth flow, token never expires) |

### qBittorrent integration

| Variable | Description |
|----------|-------------|
| `QBIT_URL` | qBittorrent Web UI URL (e.g. `http://qbittorrent:8080`) |
| `QBIT_USERNAME` | qBittorrent username (default: `admin`) |
| `QBIT_PASSWORD` | qBittorrent password |

When configured, trimming a movie/show also removes the associated torrent(s) from qBittorrent (torrent entry only — files are already deleted by Radarr/Sonarr). If you run multiple qBit instances, point this at your main one (not a dedicated seeding instance).

### Sonarr integration

| Variable | Description |
|----------|-------------|
| `SONARR_URL` | Sonarr base URL (e.g. `http://sonarr:8989`) |
| `SONARR_API_KEY` | Sonarr API key (Settings > General) |

### Jellystat integration

| Variable | Description |
|----------|-------------|
| `JELLYSTAT_URL` | Jellystat base URL (e.g. `http://jellystat:3000`) |
| `JELLYSTAT_API_KEY` | Jellystat API token (from Jellystat settings) |
| `JELLYFIN_URL` | Jellyfin base URL (e.g. `http://jellyfin:8096`) |
| `JELLYFIN_API_KEY` | Jellyfin API key (Dashboard > API Keys) |

### Monitoring

| Variable | Default | Description |
|----------|---------|-------------|
| `DISCORD_WEBHOOK_URL` | -- | Discord webhook for digest notifications |
| `DATA_DIR` | `/data` | Directory for JSON state files |
| `HC_PING_URL` | -- | Healthchecks.io ping URL for dead-man's switch |
| `PORT` | `5380` | Web UI port |

## Dashboard widget

For [Homepage](https://gethomepage.dev/) (gethomepage):

```yaml
- Trimbin:
    icon: mdi-filmstrip-off
    href: http://trimbin:5380
    widget:
      type: customapi
      url: http://trimbin:5380/api/status
      mappings:
        - field: watched_on_disk
          label: On Disk
        - field: total_gb
          label: GB Reclaimable
          format: suffix
          suffix: " GB"
        - field: new_since_last
          label: New
        - field: shows_on_disk
          label: Shows
```

## Roadmap

- [x] Show ignore list
- [x] Storage Explorer with treemap + AI recommendations
- [x] Cross-library duplicate detection (movies + TV/anime)
- [ ] Multi-user awareness in show progress ("X of Y users have watched this")
- [ ] MyAnimeList / AniList anime tracking (Fribb ID mapping for Sonarr cross-reference)
- [ ] Plex support (Tautulli watch history)
- [ ] Configurable show watch threshold (currently 75%)
- [ ] Built-in scheduled scans (no external timer needed)

## License

MIT
