#!/usr/bin/env python3
"""Trimbin — web UI + API for managing watched media still on disk."""
import json
import html
import os
import subprocess
import threading
import urllib.request
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from string import Template

APP_DIR = Path(__file__).parent
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
STATUS_FILE = DATA_DIR / "cleanup_status.json"
WATCHED_LIST_FILE = DATA_DIR / "cleanup_watched_list.json"
SHOWS_LIST_FILE = DATA_DIR / "trimbin_shows.json"
IGNORED_FILE = DATA_DIR / "trimbin_ignored.json"
CONFIG_FILE = DATA_DIR / "trimbin_config.json"
PORT = int(os.environ.get("PORT", "5380"))

CONFIG_KEYS = [
    ("LETTERBOXD_USER", "Letterboxd username", "letterboxd"),
    ("RADARR_URL", "Radarr URL", "radarr"),
    ("RADARR_API_KEY", "Radarr API key", "radarr"),
    ("SONARR_URL", "Sonarr URL", "sonarr"),
    ("SONARR_API_KEY", "Sonarr API key", "sonarr"),
    ("SIMKL_CLIENT_ID", "Simkl client ID", "simkl"),
    ("SIMKL_ACCESS_TOKEN", "Simkl access token", "simkl"),
    ("JELLYSTAT_URL", "Jellystat URL", "jellystat"),
    ("JELLYSTAT_API_KEY", "Jellystat API key", "jellystat"),
    ("JELLYFIN_URL", "Jellyfin URL", "jellystat"),
    ("JELLYFIN_API_KEY", "Jellyfin API key", "jellystat"),
    ("DISCORD_WEBHOOK_URL", "Discord webhook URL", "notifications"),
    ("HC_PING_URL", "Healthchecks ping URL", "notifications"),
    ("DIGEST_TIME", "Daily trim digest time (HH:MM, 24h)", "notifications"),
]

SENSITIVE_KEYS = {"RADARR_API_KEY", "SONARR_API_KEY", "SIMKL_CLIENT_ID",
                  "SIMKL_ACCESS_TOKEN", "JELLYSTAT_API_KEY", "JELLYFIN_API_KEY",
                  "DISCORD_WEBHOOK_URL", "HC_PING_URL"}


def load_json(path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    tmp.replace(path)


def get_config(key):
    config = load_json(CONFIG_FILE, {})
    return config.get(key) or os.environ.get(key, "")


def get_radarr_url():
    return get_config("RADARR_URL").rstrip("/")


def get_sonarr_url():
    return get_config("SONARR_URL").rstrip("/")


TRIM_LOG_FILE = DATA_DIR / "trimbin_trim_log.json"


def log_trim(title, size_gb, media_type="movie"):
    log = load_json(TRIM_LOG_FILE, [])
    log.append({
        "title": title,
        "size_gb": size_gb,
        "type": media_type,
        "trimmed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })
    save_json(TRIM_LOG_FILE, log)


def post_daily_digest():
    webhook = get_config("DISCORD_WEBHOOK_URL")
    if not webhook:
        return
    log = load_json(TRIM_LOG_FILE, [])
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    todays_trims = [t for t in log if t["trimmed_at"].startswith(today)]
    if not todays_trims:
        return
    total_gb = sum(t["size_gb"] for t in todays_trims)
    trimmed = load_json(DATA_DIR / "trimbin_trimmed.json", {"count": 0, "gb": 0})
    lines = [f"**Trimbin Daily Digest** — {len(todays_trims)} item{'s' if len(todays_trims) != 1 else ''} "
             f"trimmed today ({total_gb:.1f} GB)\n"]
    for t in todays_trims:
        lines.append(f"- **{t['title']}** — {t['size_gb']} GB ({t['type']})")
    lines.append(f"\n_Lifetime: {trimmed.get('count', 0)} items, {trimmed.get('gb', 0)} GB reclaimed_")
    msg = "\n".join(lines)
    try:
        data = json.dumps({"content": msg[:2000]}).encode()
        req = urllib.request.Request(webhook, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        urllib.request.urlopen(req, timeout=15)
    except Exception:
        pass


def digest_scheduler():
    """Background thread that posts daily trim digest at the configured time."""
    last_posted_date = None
    while True:
        try:
            digest_time = get_config("DIGEST_TIME") or "21:00"
            h, m = (int(x) for x in digest_time.split(":"))
            now = datetime.now()
            today_str = now.strftime("%Y-%m-%d")
            if now.hour == h and now.minute == m and last_posted_date != today_str:
                post_daily_digest()
                last_posted_date = today_str
        except Exception:
            pass
        threading.Event().wait(30)


PAGE_TEMPLATE = Template("""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Trimbin</title>
<link rel="icon" type="image/png" href="/logo.png">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#1a1a2e;color:#e0e0e0;padding:20px;max-width:1000px;margin:0 auto}
h1{color:#00d474;margin-bottom:4px;font-size:1.5em}
h2{color:#aaa;margin:28px 0 12px;font-size:1.1em;font-weight:600;border-bottom:1px solid #0f3460;padding-bottom:6px}
.subtitle{color:#888;margin-bottom:20px;font-size:.9em}
.summary{display:flex;gap:16px;margin-bottom:24px;flex-wrap:wrap}
.stat{background:#16213e;border:1px solid #0f3460;border-radius:8px;padding:16px 20px;flex:1;min-width:100px;text-align:center}
.stat .value{font-size:1.8em;font-weight:700;color:#00d474}
.stat .label{font-size:.75em;color:#888;margin-top:4px}
.stat.warn .value{color:#f39c12}
.stat.danger .value{color:#e94560}
.tabs{display:flex;gap:0;margin-bottom:0}
.tab{padding:10px 24px;cursor:pointer;background:#0f3460;color:#888;border:1px solid #0f3460;border-bottom:none;border-radius:8px 8px 0 0;font-size:.9em;font-weight:600}
.tab.active{background:#16213e;color:#00d474;border-color:#0f3460}
.tab-content{display:none}
.tab-content.active{display:block}
table{width:100%;border-collapse:collapse;background:#16213e;border-radius:0 8px 8px 8px;overflow:hidden;margin-bottom:8px}
th{background:#0f3460;padding:10px 12px;text-align:left;font-size:.8em;color:#888;font-weight:600;text-transform:uppercase;letter-spacing:.5px}
td{padding:10px 12px;border-bottom:1px solid #0f3460;font-size:.9em}
tr:last-child td{border-bottom:none}
tr:hover{background:#1e2d4a}
.size{font-weight:600;color:#e94560;white-space:nowrap}
.year{color:#888}
.badge{display:inline-block;padding:2px 8px;border-radius:10px;font-size:.7em;font-weight:600;margin-left:6px}
.badge.new{background:#f39c12;color:#1a1a2e}
.badge.src{background:#0f3460;color:#888;font-weight:400}
.badge.watched{background:#00d474;color:#1a1a2e}
.pct-bar{display:inline-block;width:60px;height:8px;background:#0f3460;border-radius:4px;overflow:hidden;vertical-align:middle;margin-right:6px}
.pct-fill{height:100%;border-radius:4px}
.pct-100 .pct-fill{background:#00d474}
.pct-high .pct-fill{background:#f39c12}
.pct-low .pct-fill{background:#e94560}
a.movie-link{color:#00d474;text-decoration:none}
a.movie-link:hover{text-decoration:underline}
.actions{white-space:nowrap;display:flex;gap:6px;flex-wrap:wrap}
a.arr-link{color:#ffc230;text-decoration:none;font-size:.8em;padding:4px 10px;border:1px solid #ffc230;border-radius:4px}
a.arr-link:hover{background:#ffc230;color:#1a1a2e}
button{cursor:pointer;font-size:.8em;padding:4px 10px;border-radius:4px;border:1px solid;background:transparent}
button.trim{color:#e94560;border-color:#e94560}
button.trim:hover{background:#e94560;color:#fff}
button.ignore{color:#666;border-color:#666}
button.ignore:hover{background:#333;color:#aaa}
button.restore{color:#00d474;border-color:#00d474}
button.restore:hover{background:#00d474;color:#1a1a2e}
.ignored-section table{opacity:.5}
.ignored-section:hover table{opacity:.8}
.empty{text-align:center;padding:40px 20px;color:#666}
.confirm-overlay{display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.7);z-index:100;justify-content:center;align-items:center}
.confirm-overlay.active{display:flex}
.confirm-box{background:#16213e;border:1px solid #e94560;border-radius:12px;padding:32px;max-width:420px;text-align:center}
.confirm-box h3{color:#e94560;margin-bottom:12px}
.confirm-box p{margin-bottom:20px;color:#ccc;font-size:.95em}
.confirm-box .btn-row{display:flex;gap:12px;justify-content:center}
.confirm-box button.yes{background:#e94560;color:#fff;border:none;padding:8px 24px;font-size:.9em;border-radius:4px}
.confirm-box button.no{background:transparent;color:#888;border:1px solid #888;padding:8px 24px;font-size:.9em;border-radius:4px}
.toast{display:none;position:fixed;bottom:24px;left:50%;transform:translateX(-50%);background:#00d474;color:#1a1a2e;padding:12px 24px;border-radius:8px;font-weight:600;z-index:200}
.settings-form{background:#16213e;border-radius:8px;padding:24px;margin-top:8px}
.settings-group{margin-bottom:24px}
.settings-group h3{color:#00d474;font-size:.95em;margin-bottom:12px;text-transform:uppercase;letter-spacing:.5px}
.field{margin-bottom:12px;display:flex;align-items:center;gap:12px}
.field label{min-width:180px;font-size:.85em;color:#aaa}
.field input{flex:1;background:#0f3460;border:1px solid #1e2d4a;border-radius:4px;padding:8px 12px;color:#e0e0e0;font-size:.85em;font-family:monospace}
.field input:focus{outline:none;border-color:#00d474}
.field .status-dot{width:8px;height:8px;border-radius:50%;flex-shrink:0}
.field .status-dot.set{background:#00d474}
.field .status-dot.unset{background:#444}
button.save-btn{background:#00d474;color:#1a1a2e;border:none;padding:10px 32px;font-size:.9em;font-weight:600;border-radius:4px;cursor:pointer;margin-top:8px}
button.save-btn:hover{background:#00b863}
.settings-note{color:#666;font-size:.8em;margin-top:8px}
.header-row{display:flex;align-items:center;gap:12px;margin-bottom:4px}
button.refresh-btn{background:transparent;color:#00d474;border:1px solid #00d474;padding:6px 14px;font-size:.8em;border-radius:4px;cursor:pointer}
button.refresh-btn:hover{background:#00d474;color:#1a1a2e}
button.refresh-btn:disabled{opacity:.4;cursor:wait}
button.refresh-btn .spin{display:inline-block;animation:spin 1s linear infinite}
@keyframes spin{from{transform:rotate(0deg)}to{transform:rotate(360deg)}}
@media(max-width:600px){
  .summary{gap:8px}
  .stat{padding:10px 6px;min-width:70px}
  .stat .value{font-size:1.3em}
  td,th{padding:8px 6px;font-size:.8em}
  .actions{flex-direction:column;gap:3px}
  button,a.arr-link{font-size:.75em;padding:3px 6px}
  .tab{padding:8px 12px;font-size:.8em}
  .pct-bar{width:40px}
  .field{flex-direction:column;align-items:stretch;gap:4px}
  .field label{min-width:0}
}
</style>
</head>
<body>
<div class="header-row">
<img src="/logo.png" alt="Trimbin" style="height:40px;border-radius:6px">
<h1>Trimbin</h1>
<button class="refresh-btn" onclick="runScan(this)" title="Run scanner now">Scan</button>
</div>
<p class="subtitle">Watched media still on disk &mdash; last scan: $last_run</p>
<div class="summary">
<div class="stat"><div class="value">$movies_count</div><div class="label">Movies</div></div>
<div class="stat"><div class="value">$movies_gb GB</div><div class="label">Movies reclaimable</div></div>
<div class="stat"><div class="value">$shows_count</div><div class="label">Shows</div></div>
<div class="stat"><div class="value">$shows_gb GB</div><div class="label">Shows reclaimable</div></div>
<div class="stat warn"><div class="value">$new_count</div><div class="label">New</div></div>
<div class="stat danger"><div class="value">$trimmed_gb GB</div><div class="label">Trimmed</div></div>
</div>

<div class="tabs">
<div class="tab active" onclick="switchTab('movies')">Movies</div>
<div class="tab" onclick="switchTab('shows')">Shows</div>
<div class="tab" onclick="switchTab('settings')">Settings</div>
</div>

<div id="tab-movies" class="tab-content active">
$movies_table
$ignored_section
</div>

<div id="tab-shows" class="tab-content">
$shows_table
</div>

<div id="tab-settings" class="tab-content">
$settings_html
</div>

<div class="confirm-overlay" id="confirmOverlay">
<div class="confirm-box">
<h3 id="confirmTitle">Trim this?</h3>
<p id="confirmText"></p>
<div class="btn-row">
<button class="yes" onclick="executeTrim()">Trim it</button>
<button class="no" onclick="closeConfirm()">Cancel</button>
</div>
</div>
</div>
<div class="toast" id="toast"></div>

<script>
let pendingUrl = null;
let pendingTitle = '';

function switchTab(name) {
  document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  event.target.classList.add('active');
}

function confirmTrimMovie(tmdbId, title) {
  pendingUrl = '/api/trim/' + tmdbId;
  pendingTitle = title;
  document.getElementById('confirmTitle').textContent = 'Trim movie?';
  document.getElementById('confirmText').textContent = 'Delete files for "' + title + '" and remove from Radarr?';
  document.getElementById('confirmOverlay').classList.add('active');
}

function confirmTrimShow(sonarrId, title) {
  pendingUrl = '/api/trim-show/' + sonarrId;
  pendingTitle = title;
  document.getElementById('confirmTitle').textContent = 'Trim show?';
  document.getElementById('confirmText').textContent = 'Delete all files for "' + title + '" and remove from Sonarr?';
  document.getElementById('confirmOverlay').classList.add('active');
}

function closeConfirm() {
  document.getElementById('confirmOverlay').classList.remove('active');
  pendingUrl = null;
}

function executeTrim() {
  if (!pendingUrl) return;
  var url = pendingUrl;
  closeConfirm();
  fetch(url, {method: 'POST'})
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

function saveSettings() {
  var form = document.getElementById('settingsForm');
  var data = {};
  form.querySelectorAll('input[data-key]').forEach(function(input) {
    var val = input.value.trim();
    if (val) data[input.dataset.key] = val;
  });
  fetch('/api/settings', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)})
    .then(r => r.json())
    .then(d => { showToast(d.ok ? 'Settings saved' : 'Error: ' + d.error); if(d.ok) setTimeout(() => location.reload(), 800); })
    .catch(e => showToast('Error: ' + e));
}

function runScan(btn) {
  btn.disabled = true;
  btn.innerHTML = '<span class="spin">&#x21bb;</span> Scanning...';
  fetch('/api/scan', {method: 'POST'})
    .then(r => r.json())
    .then(d => {
      if (d.ok) { showToast('Scan complete'); setTimeout(() => location.reload(), 800); }
      else { showToast('Scan error: ' + (d.error || 'unknown')); btn.disabled = false; btn.textContent = 'Scan'; }
    })
    .catch(e => { showToast('Error: ' + e); btn.disabled = false; btn.textContent = 'Scan'; });
}

function showToast(msg) {
  var t = document.getElementById('toast');
  t.textContent = msg;
  t.style.display = 'block';
  setTimeout(function(){ t.style.display = 'none'; }, 3000);
}
</script>
</body></html>""")


def slug_from_title(title, year):
    s = title.lower()
    s = "".join(c if c.isalnum() or c == " " else "" for c in s)
    return "-".join(s.split())


def build_movie_row(m, ignored=False):
    radarr_url = get_radarr_url()
    badge_new = '<span class="badge new">NEW</span>' if m.get("new") and not ignored else ""
    sources = m.get("sources", [])
    badge_src = " ".join(f'<span class="badge src">{html.escape(s)}</span>' for s in sources)
    wc = m.get("watch_count", 0)
    tu = m.get("total_users", 0)
    badge_watch = ""
    if wc > 0:
        badge_watch = f'<span class="badge watched">{wc}/{tu} watched</span>' if tu > 0 else f'<span class="badge watched">{wc} watched</span>'

    slug = slug_from_title(m["title"], m.get("year", ""))
    title = html.escape(m["title"])
    year = m.get("year", "?")
    size = m["size_gb"]
    rid = m.get("radarr_id", "")
    tmdb = m.get("tmdb_id", "")
    title_js = html.escape(m["title"].replace("'", "\\'").replace('"', '\\"'))

    if ignored:
        actions = f'<button class="restore" onclick="doRestore({tmdb})">Restore</button>'
    else:
        arr_link = f'<a class="arr-link" href="{radarr_url}/movie/{rid}" target="_blank">Radarr</a>' if radarr_url else ''
        actions = (
            f'<button class="trim" onclick="confirmTrimMovie({tmdb}, \'{title_js}\')">Trim</button>'
            f'<button class="ignore" onclick="doIgnore({tmdb})">Ignore</button>'
            f'{arr_link}'
        )

    return (
        f'<tr><td><a class="movie-link" href="https://letterboxd.com/film/{slug}/" '
        f'target="_blank">{title}</a> <span class="year">({year})</span>'
        f'{badge_new}{badge_src}{badge_watch}</td>'
        f'<td class="size">{size} GB</td>'
        f'<td class="actions">{actions}</td></tr>'
    )


def build_show_row(s):
    sonarr_url = get_sonarr_url()
    title = html.escape(s["title"])
    year = s.get("year", "?")
    size = s["size_gb"]
    sid = s.get("sonarr_id", "")
    pct = s.get("watched_pct", 0)
    w_eps = s.get("watched_episodes", 0)
    t_eps = s.get("total_episodes", 0)
    title_js = html.escape(s["title"].replace("'", "\\'").replace('"', '\\"'))

    pct_class = "pct-100" if pct >= 100 else ("pct-high" if pct >= 50 else "pct-low")

    pct_bar = (
        f'<span class="pct-bar {pct_class}"><span class="pct-fill" style="width:{min(pct,100)}%"></span></span>'
        f'{w_eps}/{t_eps} eps ({pct}%)'
    )

    arr_link = f'<a class="arr-link" href="{sonarr_url}/series/{sid}" target="_blank">Sonarr</a>' if sonarr_url else ''
    actions = (
        f'<button class="trim" onclick="confirmTrimShow({sid}, \'{title_js}\')">Trim</button>'
        f'{arr_link}'
    )

    return (
        f'<tr><td>{title} <span class="year">({year})</span></td>'
        f'<td>{pct_bar}</td>'
        f'<td class="size">{size} GB</td>'
        f'<td class="actions">{actions}</td></tr>'
    )


def build_settings_html():
    config = load_json(CONFIG_FILE, {})
    groups = {}
    for key, label, group in CONFIG_KEYS:
        groups.setdefault(group, []).append((key, label))

    group_titles = {
        "letterboxd": "Letterboxd",
        "radarr": "Radarr",
        "sonarr": "Sonarr",
        "simkl": "Simkl",
        "jellystat": "Jellystat / Jellyfin",
        "notifications": "Notifications",
    }

    parts = ['<form id="settingsForm" class="settings-form" onsubmit="event.preventDefault();saveSettings()">']
    for group_key in ["letterboxd", "radarr", "sonarr", "simkl", "jellystat", "notifications"]:
        if group_key not in groups:
            continue
        parts.append(f'<div class="settings-group"><h3>{group_titles.get(group_key, group_key)}</h3>')
        for key, label in groups[group_key]:
            val = config.get(key, "")
            env_val = os.environ.get(key, "")
            has_value = bool(val or env_val)
            dot_class = "set" if has_value else "unset"
            is_sensitive = key in SENSITIVE_KEYS
            input_type = "password" if is_sensitive else "text"
            display_val = html.escape(val) if val else ""
            placeholder = "(from environment)" if env_val and not val else ""
            parts.append(
                f'<div class="field">'
                f'<span class="status-dot {dot_class}"></span>'
                f'<label>{html.escape(label)}</label>'
                f'<input type="{input_type}" data-key="{key}" value="{display_val}" placeholder="{placeholder}">'
                f'</div>'
            )
        parts.append('</div>')
    parts.append('<button type="submit" class="save-btn">Save settings</button>')
    parts.append('<p class="settings-note">Settings are saved to the data directory. Environment variables are used as fallback when a field is empty.</p>')
    parts.append('</form>')
    return "\n".join(parts)


def arr_api(base_url, api_key, method, path, body=None):
    url = f"{base_url}/api/v3{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("X-Api-Key", api_key)
    req.add_header("Content-Type", "application/json")
    resp = urllib.request.urlopen(req, timeout=30)
    if resp.status == 200 and method == "GET":
        return json.loads(resp.read())
    return None


def trim_movie(tmdb_id):
    radarr_url = get_radarr_url()
    radarr_key = get_config("RADARR_API_KEY")
    movies = arr_api(radarr_url, radarr_key, "GET", "/movie")
    target = None
    for m in movies:
        if m.get("tmdbId") == tmdb_id:
            target = m
            break
    if not target:
        return False, "Movie not found in Radarr"

    title = target.get("title", "Unknown")
    size_gb = round(target.get("sizeOnDisk", 0) / (1024**3), 1)
    arr_api(radarr_url, radarr_key, "DELETE",
            f"/movie/{target['id']}?deleteFiles=true&addImportExclusion=false")

    movies_list = load_json(WATCHED_LIST_FILE, [])
    movies_list = [m for m in movies_list if m.get("tmdb_id") != tmdb_id]
    save_json(WATCHED_LIST_FILE, movies_list)

    trimmed = load_json(DATA_DIR / "trimbin_trimmed.json", {"count": 0, "gb": 0})
    trimmed["count"] += 1
    trimmed["gb"] = round(trimmed["gb"] + size_gb, 1)
    save_json(DATA_DIR / "trimbin_trimmed.json", trimmed)
    log_trim(title, size_gb, "movie")

    return True, "ok"


def trim_show(sonarr_id):
    sonarr_url = get_sonarr_url()
    sonarr_key = get_config("SONARR_API_KEY")
    series = arr_api(sonarr_url, sonarr_key, "GET", "/series")
    target = None
    for s in series:
        if s.get("id") == sonarr_id:
            target = s
            break
    if not target:
        return False, "Show not found in Sonarr"

    title = target.get("title", "Unknown")
    size_gb = round(target.get("statistics", {}).get("sizeOnDisk", 0) / (1024**3), 1)
    arr_api(sonarr_url, sonarr_key, "DELETE",
            f"/series/{target['id']}?deleteFiles=true&addImportExclusion=false")

    shows_list = load_json(SHOWS_LIST_FILE, [])
    shows_list = [s for s in shows_list if s.get("sonarr_id") != sonarr_id]
    save_json(SHOWS_LIST_FILE, shows_list)

    trimmed = load_json(DATA_DIR / "trimbin_trimmed.json", {"count": 0, "gb": 0})
    trimmed["count"] += 1
    trimmed["gb"] = round(trimmed["gb"] + size_gb, 1)
    save_json(DATA_DIR / "trimbin_trimmed.json", trimmed)
    log_trim(title, size_gb, "show")

    return True, "ok"


def run_scan():
    try:
        result = subprocess.run(
            ["python3", "/app/cleanup-notify.py"],
            capture_output=True, text=True, timeout=600,
        )
        return result.returncode == 0, result.stderr[-500:] if result.returncode != 0 else "ok"
    except subprocess.TimeoutExpired:
        return False, "scan timed out"
    except Exception as e:
        return False, str(e)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/status":
            data = load_json(STATUS_FILE, {
                "watched_on_disk": 0, "total_gb": 0, "new_since_last": 0,
                "new_gb": 0, "shows_on_disk": 0, "shows_gb": 0, "last_run": "never"})
            self._json_response(data)
        elif self.path in ("/", "/status", "/ui"):
            self._serve_ui()
        elif self.path == "/logo.png":
            logo = APP_DIR / "logo.png"
            if logo.exists():
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Cache-Control", "public, max-age=86400")
                self.end_headers()
                self.wfile.write(logo.read_bytes())
            else:
                self.send_response(404)
                self.end_headers()
        elif self.path == "/ping":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"pong")
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/api/scan":
            ok, msg = run_scan()
            self._json_response({"ok": ok, "error": msg if not ok else None})
        elif self.path == "/api/settings":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            save_json(CONFIG_FILE, body)
            self._json_response({"ok": True})
        elif self.path.startswith("/api/trim-show/"):
            sonarr_id = int(self.path.split("/")[-1])
            ok, msg = trim_show(sonarr_id)
            self._json_response({"ok": ok, "error": msg if not ok else None})
        elif self.path.startswith("/api/trim/"):
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
            "shows_on_disk": 0, "shows_gb": 0, "last_run": "never"})
        movies = load_json(WATCHED_LIST_FILE, [])
        shows = load_json(SHOWS_LIST_FILE, [])
        ignored_ids = set(load_json(IGNORED_FILE, []))
        trimmed = load_json(DATA_DIR / "trimbin_trimmed.json", {"count": 0, "gb": 0})

        active_movies = [m for m in movies if m.get("tmdb_id") not in ignored_ids]
        ignored_movies = [m for m in movies if m.get("tmdb_id") in ignored_ids]
        active_gb = sum(m["size_gb"] for m in active_movies)
        shows_gb = sum(s["size_gb"] for s in shows)

        if active_movies:
            rows = "\n".join(build_movie_row(m) for m in active_movies)
            movies_table = (
                "<table><thead><tr><th>Movie</th><th>Size</th><th>Actions</th></tr></thead>"
                f"<tbody>{rows}</tbody></table>"
            )
        else:
            movies_table = '<div class="empty">No watched movies on disk. Next scan will populate this.</div>'

        if ignored_movies:
            ig_rows = "\n".join(build_movie_row(m, ignored=True) for m in ignored_movies)
            ignored_html = (
                '<div class="ignored-section">'
                f'<h2>Ignored ({len(ignored_movies)} movies, {sum(m["size_gb"] for m in ignored_movies):.0f} GB)</h2>'
                "<table><thead><tr><th>Movie</th><th>Size</th><th>Actions</th></tr></thead>"
                f"<tbody>{ig_rows}</tbody></table></div>"
            )
        else:
            ignored_html = ""

        if shows:
            s_rows = "\n".join(build_show_row(s) for s in shows)
            shows_table = (
                "<table><thead><tr><th>Show</th><th>Progress</th><th>Size</th><th>Actions</th></tr></thead>"
                f"<tbody>{s_rows}</tbody></table>"
            )
        else:
            shows_table = '<div class="empty">No watched shows on disk. Enable Simkl + Sonarr to track shows.</div>'

        page = PAGE_TEMPLATE.substitute(
            last_run=html.escape(status.get("last_run", "never")),
            movies_count=len(active_movies),
            movies_gb=round(active_gb),
            shows_count=len(shows),
            shows_gb=round(shows_gb),
            new_count=status.get("new_since_last", 0),
            trimmed_gb=trimmed.get("gb", 0),
            movies_table=movies_table,
            ignored_section=ignored_html,
            shows_table=shows_table,
            settings_html=build_settings_html(),
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(page.encode())

    def log_message(self, format, *args):
        pass


def serve():
    t = threading.Thread(target=digest_scheduler, daemon=True)
    t.start()
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    serve()
