# letterboxd-cleanup

Monitors your Letterboxd watched list and reports which movies are still on disk in Radarr.

## What it does

letterboxd-cleanup scrapes your Letterboxd profile for watched films, resolves each to a TMDB ID, then cross-references your Radarr library to find watched movies still taking up storage. It posts a Discord digest of the results and serves a web UI so you can review them at a glance.

## Architecture

The project has two components:

- **cleanup-notify.py** -- A one-shot script meant to run on a weekly schedule (cron, systemd timer, etc.). It performs the Letterboxd scrape, Radarr lookup, and Discord notification, then writes status and movie-list JSON files to disk.
- **status-server.py** -- A lightweight HTTP server that reads the JSON files written by the cleanup script and serves both a JSON API (for dashboard widgets) and an HTML web UI.

### Web UI

The status server provides a dark-themed page listing each watched-on-disk movie with its file size, a link to the movie on Letterboxd, and a direct link into Radarr. Summary stats at the top show total count, total size, and how many are new since the last scan.

### Discord notifications

On the first run, the cleanup script posts the full list of watched movies on disk (top 20 by size). On subsequent runs, it only reports newly watched movies that are still on disk. This keeps the notifications useful without being noisy.

### Homepage integration

The `/api/status` endpoint returns JSON suitable for a [Homepage](https://gethomepage.dev/) `customapi` widget:

```yaml
- Letterboxd Cleanup:
    widget:
      type: customapi
      url: http://letterboxd-cleanup:5380/api/status
      mappings:
        - field: watched_on_disk
          label: Watched on disk
        - field: total_gb
          label: Total GB
        - field: new_since_last
          label: New since last
```

## Prerequisites

- Python 3.12+
- A Radarr instance with API access
- A Letterboxd account (public profile)
- (Optional) A Discord webhook URL for notifications
- (Optional) A Healthchecks ping URL for dead-man's-switch monitoring

## Setup

1. Clone the repository:

   ```bash
   git clone https://github.com/your-user/letterboxd-cleanup.git
   cd letterboxd-cleanup
   ```

2. Copy the example environment file and fill in your values:

   ```bash
   cp .env.example .env
   ```

3. Run with Docker Compose:

   ```bash
   docker compose up -d
   ```

   This starts the status server on port 5380. To run the cleanup scan, exec into the container:

   ```bash
   docker compose exec letterboxd-cleanup-status python cleanup-notify.py
   ```

   Or run it directly if you prefer not to use Docker:

   ```bash
   export $(grep -v '^#' .env | xargs)
   python status-server.py &
   python cleanup-notify.py
   ```

## Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `LETTERBOXD_USER` | Yes | -- | Your Letterboxd username |
| `RADARR_URL` | Yes | -- | Base URL of your Radarr instance (e.g. `http://radarr:7878`) |
| `RADARR_API_KEY` | Yes | -- | Radarr API key |
| `DISCORD_WEBHOOK_URL` | No | -- | Discord webhook URL for digest notifications |
| `DATA_DIR` | No | `/data` | Directory for JSON state files |
| `HC_PING_URL` | No | -- | Healthchecks ping URL for dead-man's-switch monitoring |
| `PORT` | No | `5380` | Port for the status server |

## Running the cleanup script manually

```bash
python cleanup-notify.py
```

The script reads environment variables (or an optional env file at the path in `CLEANUP_ENV_FILE`), scrapes Letterboxd, queries Radarr, writes status JSON, and posts to Discord if there are new results. It pings the Healthchecks URL at start and finish if configured.

## Shared TMDB slug cache

The cleanup script maintains a `slug_to_tmdb.json` cache file in `DATA_DIR` that maps Letterboxd slugs to TMDB IDs. If you also run [letterboxd-sync](https://github.com/your-user/letterboxd-sync) and point both projects at the same data directory, they will share this cache, reducing redundant lookups against Letterboxd.

## License

[MIT](LICENSE)
