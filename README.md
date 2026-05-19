# Trimbin

Media cleanup tool for the *arr ecosystem. Monitors your Letterboxd watched list and reports which movies are still on disk in Radarr — then lets you trim them with one click.

Named after the **trim bin** — the wire basket next to a flatbed film editor where the discarded strips of celluloid collect after each cut.

## What it does

Trimbin scrapes your Letterboxd profile for watched films, resolves each to a TMDB ID, then cross-references your Radarr library to find watched movies still taking up storage. It serves a web UI where you can review, trim (delete + unmonitor), or ignore them.

## Features

- **Watched-on-disk detection** — weekly scan cross-references Letterboxd watched list against Radarr library
- **One-click trim** — delete movie files and unmonitor in Radarr from the web UI (with confirmation dialog)
- **Ignore list** — push movies to a greyed-out secondary list to exclude them from reclaimable totals; restore them anytime
- **Discord digest** — weekly notification of newly watched movies still on disk
- **Status API** — JSON endpoint for dashboard widgets (Homepage, etc.)
- **Trimmed counter** — tracks cumulative disk space reclaimed

## Architecture

Two components in one container:

- **cleanup-notify.py** — one-shot scanner that runs on a schedule (cron/timer). Scrapes Letterboxd, queries Radarr, writes status JSON, and posts to Discord.
- **status-server.py** — always-running HTTP server. Reads the JSON files and serves the web UI + API. Handles trim/ignore actions via Radarr API.

## Web UI

The status server provides a dark-themed dashboard at port 5380:

- Summary stats: watched on disk, reclaimable GB, new since last scan, total trimmed
- Movie table with Letterboxd links, file sizes, and action buttons (Trim / Ignore / Radarr)
- Confirmation dialog before any deletion
- Ignored movies section (greyed out, with Restore button)

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Web UI |
| GET | `/api/status` | JSON status for dashboard widgets |
| GET | `/ping` | Health check |
| POST | `/api/trim/<tmdb_id>` | Delete files + unmonitor in Radarr |
| POST | `/api/ignore/<tmdb_id>` | Add movie to ignore list |
| POST | `/api/unignore/<tmdb_id>` | Remove movie from ignore list |

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

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `LETTERBOXD_USER` | Yes | -- | Your Letterboxd username |
| `RADARR_URL` | Yes | -- | Radarr base URL (e.g. `http://radarr:7878`) |
| `RADARR_API_KEY` | Yes | -- | Radarr API key (Settings > General) |
| `DISCORD_WEBHOOK_URL` | No | -- | Discord webhook for digest notifications |
| `DATA_DIR` | No | `/data` | Directory for JSON state files |
| `HC_PING_URL` | No | -- | Healthchecks.io ping URL for dead-man's switch |
| `PORT` | No | `5380` | Web UI port |

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
```

## Roadmap

- [ ] Jellyfin watch tracking (movies, shows, anime) via Jellystat integration
- [ ] Sonarr support — same trim workflow for watched shows
- [ ] Trakt integration as an alternative watch source
- [ ] Multi-user awareness — "X of Y users have watched this"

## License

MIT
