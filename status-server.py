#!/usr/bin/env python3
"""Trimbin — web UI + API for managing watched media still on disk."""
import json
import html
import os
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from string import Template
from urllib.parse import parse_qs

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
STATUS_FILE = DATA_DIR / "cleanup_status.json"
WATCHED_LIST_FILE = DATA_DIR / "cleanup_watched_list.json"
IGNORED_FILE = DATA_DIR / "trimbin_ignored.json"
RADARR_URL = os.environ.get("RADARR_URL", "").rstrip("/")
RADARR_API_KEY = os.environ.get("RADARR_API_KEY", "")
PORT = int(os.environ.get("PORT", "5380"))

PAGE_TEMPLATE = Template("""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Trimbin</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#1a1a2e;color:#e0e0e0;padding:20px;max-width:960px;margin:0 auto}
h1{color:#00d474;margin-bottom:4px;font-size:1.5em}
h2{color:#888;margin:32px 0 12px;font-size:1.1em;font-weight:600}
.subtitle{color:#888;margin-bottom:20px;font-size:.9em}
.summary{display:flex;gap:16px;margin-bottom:24px;flex-wrap:wrap}
.stat{background:#16213e;border:1px solid #0f3460;border-radius:8px;padding:16px 20px;flex:1;min-width:120px;text-align:center}
.stat .value{font-size:1.8em;font-weight:700;color:#00d474}
.stat .label{font-size:.8em;color:#888;margin-top:4px}
.stat.new .value{color:#f39c12}
.stat.reclaimed .value{color:#e94560}
table{width:100%;border-collapse:collapse;background:#16213e;border-radius:8px;overflow:hidden;margin-bottom:8px}
th{background:#0f3460;padding:10px 12px;text-align:left;font-size:.85em;color:#888;font-weight:600;text-transform:uppercase;letter-spacing:.5px}
td{padding:10px 12px;border-bottom:1px solid #0f3460;font-size:.9em}
tr:last-child td{border-bottom:none}
tr:hover{background:#1e2d4a}
.size{font-weight:600;color:#e94560;white-space:nowrap}
.year{color:#888}
.badge{display:inline-block;padding:2px 8px;border-radius:10px;font-size:.75em;font-weight:600;background:#f39c12;color:#1a1a2e;margin-left:8px}
a.movie-link{color:#00d474;text-decoration:none}
a.movie-link:hover{text-decoration:underline}
.actions{white-space:nowrap;display:flex;gap:6px}
a.radarr{color:#ffc230;text-decoration:none;font-size:.8em;padding:4px 10px;border:1px solid #ffc230;border-radius:4px}
a.radarr:hover{background:#ffc230;color:#1a1a2e}
button{cursor:pointer;font-size:.8em;padding:4px 10px;border-radius:4px;border:1px solid;background:transparent}
button.trim{color:#e94560;border-color:#e94560}
button.trim:hover{background:#e94560;color:#fff}
button.ignore{color:#666;border-color:#666}
button.ignore:hover{background:#333;color:#aaa}
button.restore{color:#00d474;border-color:#00d474}
button.restore:hover{background:#00d474;color:#1a1a2e}
.ignored-section table{opacity:.5}
.ignored-section:hover table{opacity:.8}
.empty{text-align:center;padding:60px 20px;color:#666}
.confirm-overlay{display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.7);z-index:100;justify-content:center;align-items:center}
.confirm-overlay.active{display:flex}
.confirm-box{background:#16213e;border:1px solid #e94560;border-radius:12px;padding:32px;max-width:400px;text-align:center}
.confirm-box h3{color:#e94560;margin-bottom:12px}
.confirm-box p{margin-bottom:20px;color:#ccc}
.confirm-box .btn-row{display:flex;gap:12px;justify-content:center}
.confirm-box button.yes{background:#e94560;color:#fff;border:none;padding:8px 24px;font-size:.9em}
.confirm-box button.no{background:transparent;color:#888;border:1px solid #888;padding:8px 24px;font-size:.9em}
.toast{display:none;position:fixed;bottom:24px;left:50%;transform:translateX(-50%);background:#00d474;color:#1a1a2e;padding:12px 24px;border-radius:8px;font-weight:600;z-index:200}
@media(max-width:600px){
  .summary{gap:8px}
  .stat{padding:12px 8px}
  .stat .value{font-size:1.4em}
  td,th{padding:8px 6px;font-size:.8em}
  .actions{flex-direction:column;gap:4px}
  button,a.radarr{font-size:.75em;padding:3px 6px}
}
</style>
</head>
<body>
<h1>Trimbin</h1>
<p class="subtitle">Watched movies still on disk &mdash; last scan: $last_run</p>
<div class="summary">
<div class="stat"><div class="value">$watched_on_disk</div><div class="label">Watched on disk</div></div>
<div class="stat"><div class="value">$total_gb GB</div><div class="label">Reclaimable</div></div>
<div class="stat new"><div class="value">$new_since_last</div><div class="label">New since last</div></div>
<div class="stat reclaimed"><div class="value">$trimmed_gb GB</div><div class="label">Trimmed</div></div>
</div>
$table
$ignored_section

<div class="confirm-overlay" id="confirmOverlay">
<div class="confirm-box">
<h3>Trim this movie?</h3>
<p id="confirmText">Delete files and unmonitor in Radarr?</p>
<div class="btn-row">
<button class="yes" onclick="executeTrim()">Trim it</button>
<button class="no" onclick="closeConfirm()">Cancel</button>
</div>
</div>
</div>
<div class="toast" id="toast"></div>

<script>
let pendingTmdb = null;
let pendingTitle = '';

function confirmTrim(tmdbId, title) {
  pendingTmdb = tmdbId;
  pendingTitle = title;
  document.getElementById('confirmText').textContent = 'Delete files for "' + title + '" and unmonitor in Radarr?';
  document.getElementById('confirmOverlay').classList.add('active');
}

function closeConfirm() {
  document.getElementById('confirmOverlay').classList.remove('active');
  pendingTmdb = null;
}

function executeTrim() {
  if (!pendingTmdb) return;
  closeConfirm();
  fetch('/api/trim/' + pendingTmdb, {method: 'POST'})
    .then(r => r.json())
    .then(d => { showToast(d.ok ? 'Trimmed: ' + pendingTitle : 'Error: ' + (d.error || 'unknown')); setTimeout(() => location.reload(), 1200); })
    .catch(e => showToast('Error: ' + e));
}

function doIgnore(tmdbId) {
  fetch('/api/ignore/' + tmdbId, {method: 'POST'})
    .then(r => r.json())
    .then(d => { if(d.ok) location.reload(); else showToast('Error: ' + d.error); });
}

function doRestore(tmdbId) {
  fetch('/api/unignore/' + tmdbId, {method: 'POST'})
    .then(r => r.json())
    .then(d => { if(d.ok) location.reload(); else showToast('Error: ' + d.error); });
}

function showToast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.style.display = 'block';
  setTimeout(() => t.style.display = 'none', 3000);
}
</script>
</body></html>""")


def slug_from_title(title, year):
    s = title.lower()
    s = "".join(c if c.isalnum() or c == " " else "" for c in s)
    return "-".join(s.split())


def load_json(path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    tmp.replace(path)


def build_row(m, ignored=False):
    badge = '<span class="badge">NEW</span>' if m.get("new") and not ignored else ""
    slug = slug_from_title(m["title"], m.get("year", ""))
    title = html.escape(m["title"])
    year = m.get("year", "?")
    size = m["size_gb"]
    rid = m.get("radarr_id", "")
    tmdb = m.get("tmdb_id", "")
    title_js = html.escape(m["title"].replace("'", "\\'").replace('"', '\\"'))

    if ignored:
        actions = (
            f'<button class="restore" onclick="doRestore({tmdb})">Restore</button>'
        )
    else:
        actions = (
            f'<button class="trim" onclick="confirmTrim({tmdb}, \'{title_js}\')">Trim</button>'
            f'<button class="ignore" onclick="doIgnore({tmdb})">Ignore</button>'
            f'<a class="radarr" href="{RADARR_URL}/movie/{rid}" target="_blank">Radarr</a>'
        )

    return (
        f'<tr><td><a class="movie-link" href="https://letterboxd.com/film/{slug}/" '
        f'target="_blank">{title}</a> <span class="year">({year})</span>{badge}</td>'
        f'<td class="size">{size} GB</td>'
        f'<td class="actions">{actions}</td></tr>'
    )


def radarr_api(method, path, body=None):
    url = f"{RADARR_URL}/api/v3{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("X-Api-Key", RADARR_API_KEY)
    req.add_header("Content-Type", "application/json")
    resp = urllib.request.urlopen(req, timeout=30)
    if resp.status == 200:
        return json.loads(resp.read())
    return None


def trim_movie(tmdb_id):
    """Delete movie files and unmonitor in Radarr."""
    movies = radarr_api("GET", "/movie")
    target = None
    for m in movies:
        if m.get("tmdbId") == tmdb_id:
            target = m
            break
    if not target:
        return False, "Movie not found in Radarr"

    radarr_api("DELETE", f"/movie/{target['id']}?deleteFiles=true&addImportExclusion=false")

    movies_list = load_json(WATCHED_LIST_FILE, [])
    movies_list = [m for m in movies_list if m.get("tmdb_id") != tmdb_id]
    save_json(WATCHED_LIST_FILE, movies_list)

    trimmed = load_json(DATA_DIR / "trimbin_trimmed.json", {"count": 0, "gb": 0})
    trimmed["count"] += 1
    trimmed["gb"] = round(trimmed["gb"] + target.get("sizeOnDisk", 0) / (1024**3), 1)
    save_json(DATA_DIR / "trimbin_trimmed.json", trimmed)

    return True, "ok"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/status":
            data = load_json(STATUS_FILE, {
                "watched_on_disk": 0, "total_gb": 0, "new_since_last": 0,
                "new_gb": 0, "last_run": "never"})
            self._json_response(data)

        elif self.path in ("/", "/status", "/ui"):
            self._serve_ui()

        elif self.path == "/ping":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"pong")
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path.startswith("/api/trim/"):
            tmdb_id = int(self.path.split("/")[-1])
            ok, msg = trim_movie(tmdb_id)
            self._json_response({"ok": ok, "error": msg if not ok else None})

        elif self.path.startswith("/api/ignore/"):
            tmdb_id = int(self.path.split("/")[-1])
            ignored = set(load_json(IGNORED_FILE, []))
            ignored.add(tmdb_id)
            save_json(IGNORED_FILE, sorted(ignored))
            self._json_response({"ok": True})

        elif self.path.startswith("/api/unignore/"):
            tmdb_id = int(self.path.split("/")[-1])
            ignored = set(load_json(IGNORED_FILE, []))
            ignored.discard(tmdb_id)
            save_json(IGNORED_FILE, sorted(ignored))
            self._json_response({"ok": True})

        else:
            self.send_response(404)
            self.end_headers()

    def _json_response(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def _serve_ui(self):
        status = load_json(STATUS_FILE, {
            "watched_on_disk": 0, "total_gb": 0, "new_since_last": 0,
            "new_gb": 0, "last_run": "never"})
        movies = load_json(WATCHED_LIST_FILE, [])
        ignored_ids = set(load_json(IGNORED_FILE, []))
        trimmed = load_json(DATA_DIR / "trimbin_trimmed.json", {"count": 0, "gb": 0})

        active = [m for m in movies if m.get("tmdb_id") not in ignored_ids]
        ignored = [m for m in movies if m.get("tmdb_id") in ignored_ids]

        active_gb = sum(m["size_gb"] for m in active)

        if active:
            rows = "\n".join(build_row(m) for m in active)
            table_html = (
                "<table><thead><tr><th>Movie</th><th>Size</th><th>Actions</th></tr></thead>"
                f"<tbody>{rows}</tbody></table>"
            )
        else:
            table_html = '<div class="empty">No watched movies on disk (or all ignored). The scan runs weekly.</div>'

        if ignored:
            ig_rows = "\n".join(build_row(m, ignored=True) for m in ignored)
            ignored_html = (
                '<div class="ignored-section">'
                f'<h2>Ignored ({len(ignored)} movies, {sum(m["size_gb"] for m in ignored):.0f} GB)</h2>'
                "<table><thead><tr><th>Movie</th><th>Size</th><th>Actions</th></tr></thead>"
                f"<tbody>{ig_rows}</tbody></table></div>"
            )
        else:
            ignored_html = ""

        page = PAGE_TEMPLATE.substitute(
            last_run=html.escape(status.get("last_run", "never")),
            watched_on_disk=len(active),
            total_gb=round(active_gb),
            new_since_last=status.get("new_since_last", 0),
            trimmed_gb=trimmed.get("gb", 0),
            table=table_html,
            ignored_section=ignored_html,
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(page.encode())

    def log_message(self, format, *args):
        pass


def serve():
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    serve()
