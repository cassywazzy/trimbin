# Trimbin

Media cleanup tool for the *arr ecosystem. Monitors your watched lists across multiple sources and reports which media is still on disk — then lets you trim it with one click.

Named after the **trim bin** — the wire basket next to a flatbed film editor where the discarded strips of celluloid collect after each cut.

## What it does

Trimbin pulls watched movies and shows from Letterboxd, Simkl, and Jellystat, then cross-references your Radarr and Sonarr libraries to find watched content still consuming storage. It serves a web UI where you can review, trim (delete + unmonitor), or ignore them.

## Features

- **Multi-source watch detection** — merges watched lists from Letterboxd, Simkl, and Jellystat
- **Movie + show support** — cross-references Radarr (movies) and Sonarr (shows)
- **Multi-user watch counts** — Jellystat integration shows "3/4 users watched" badges
- **One-click trim** — delete files and unmonitor in Radarr/Sonarr from the web UI (with confirmation dialog)
- **Ignore list** — push movies to a greyed-out secondary list; restore them anytime
- **Tabbed UI** — separate Movies and Shows tabs with progress bars for episode completion
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
| POST | `/api/trim/<tmdb_id>` | Delete movie files + remove from Radarr |
| POST | `/api/trim-show/<sonarr_id>` | Delete show files + remove from Sonarr |
| POST | `/api/ignore/<tmdb_id>` | Add movie to ignore list |
| POST | `/api/unignore/<tmdb_id>` | Remove movie from ignore list |
| GET/POST | `/api/settings` | View/save integration settings |

## Setup

### Docker (recommended)

```bash
git clone https://github.com/your-user/trimbin.git
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

- [ ] Show ignore list (same pattern as movies)
- [ ] Multi-user awareness in show progress ("X of Y users have watched this")
- [ ] Anime-specific tracking via AniList/MAL integration

## License

MIT
