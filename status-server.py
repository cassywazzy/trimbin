#!/usr/bin/env python3
"""Letterboxd cleanup status server — JSON API + web UI."""
import json
import html
import os
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from string import Template

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
STATUS_FILE = DATA_DIR / "cleanup_status.json"
WATCHED_LIST_FILE = DATA_DIR / "cleanup_watched_list.json"
RADARR_URL = os.environ.get("RADARR_URL", "")
PORT = int(os.environ.get("PORT", "5380"))

PAGE_TEMPLATE = Template("""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Letterboxd Cleanup</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#1a1a2e;color:#e0e0e0;padding:20px;max-width:900px;margin:0 auto}
h1{color:#00d474;margin-bottom:4px;font-size:1.5em}
.subtitle{color:#888;margin-bottom:20px;font-size:.9em}
.summary{display:flex;gap:16px;margin-bottom:24px;flex-wrap:wrap}
.stat{background:#16213e;border:1px solid #0f3460;border-radius:8px;padding:16px 20px;flex:1;min-width:120px;text-align:center}
.stat .value{font-size:1.8em;font-weight:700;color:#00d474}
.stat .label{font-size:.8em;color:#888;margin-top:4px}
.stat.new .value{color:#f39c12}
table{width:100%;border-collapse:collapse;background:#16213e;border-radius:8px;overflow:hidden}
th{background:#0f3460;padding:10px 12px;text-align:left;font-size:.85em;color:#888;font-weight:600;text-transform:uppercase;letter-spacing:.5px}
td{padding:10px 12px;border-bottom:1px solid #0f3460;font-size:.9em}
tr:last-child td{border-bottom:none}
tr:hover{background:#1e2d4a}
.size{font-weight:600;color:#e94560;white-space:nowrap}
.year{color:#888}
.badge{display:inline-block;padding:2px 8px;border-radius:10px;font-size:.75em;font-weight:600;background:#f39c12;color:#1a1a2e;margin-left:8px}
a.radarr{color:#ffc230;text-decoration:none;font-size:.8em;padding:4px 10px;border:1px solid #ffc230;border-radius:4px;white-space:nowrap}
a.radarr:hover{background:#ffc230;color:#1a1a2e}
.empty{text-align:center;padding:60px 20px;color:#666}
.letterboxd{color:#00d474;text-decoration:none}
.letterboxd:hover{text-decoration:underline}
@media(max-width:600px){
  .summary{gap:8px}
  .stat{padding:12px 8px}
  .stat .value{font-size:1.4em}
  td,th{padding:8px 6px;font-size:.8em}
  a.radarr{padding:3px 6px;font-size:.75em}
}
</style>
</head>
<body>
<h1>Letterboxd Cleanup</h1>
<p class="subtitle">Watched movies still on disk &mdash; last scan: $last_run</p>
<div class="summary">
<div class="stat"><div class="value">$watched_on_disk</div><div class="label">Watched on disk</div></div>
<div class="stat"><div class="value">$total_gb GB</div><div class="label">Total size</div></div>
<div class="stat new"><div class="value">$new_since_last</div><div class="label">New since last</div></div>
</div>
$table
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


def build_row(m):
    badge = '<span class="badge">NEW</span>' if m.get("new") else ""
    slug = slug_from_title(m["title"], m.get("year", ""))
    title = html.escape(m["title"])
    year = m.get("year", "?")
    size = m["size_gb"]
    rid = m.get("radarr_id", "")
    return (
        f'<tr><td><a class="letterboxd" href="https://letterboxd.com/film/{slug}/" '
        f'target="_blank">{title}</a> <span class="year">({year})</span>{badge}</td>'
        f'<td class="size">{size} GB</td>'
        f'<td><a class="radarr" href="{RADARR_URL}/movie/{rid}" target="_blank">Radarr</a></td></tr>'
    )


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/status":
            data = load_json(STATUS_FILE, {
                "watched_on_disk": 0, "total_gb": 0, "new_since_last": 0,
                "new_gb": 0, "last_run": "never"})
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(data).encode())

        elif self.path in ("/", "/status", "/ui"):
            status = load_json(STATUS_FILE, {
                "watched_on_disk": 0, "total_gb": 0, "new_since_last": 0,
                "new_gb": 0, "last_run": "never"})
            movies = load_json(WATCHED_LIST_FILE, [])

            if movies:
                rows = "\n".join(build_row(m) for m in movies)
                table_html = (
                    "<table><thead><tr><th>Movie</th><th>Size</th><th>Action</th></tr></thead>"
                    f"<tbody>{rows}</tbody></table>"
                )
            else:
                table_html = '<div class="empty">No scan data yet. The cleanup script runs weekly.</div>'

            page = PAGE_TEMPLATE.substitute(
                last_run=html.escape(status.get("last_run", "never")),
                watched_on_disk=status.get("watched_on_disk", 0),
                total_gb=status.get("total_gb", 0),
                new_since_last=status.get("new_since_last", 0),
                table=table_html,
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(page.encode())

        elif self.path == "/ping":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"pong")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass


def serve():
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    serve()
