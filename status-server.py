#!/usr/bin/env python3
"""Trimbin — web UI + API for managing watched media still on disk."""
import json
import html
import hashlib
import os
import shutil
import subprocess
import threading
import time
import urllib.request
import urllib.error
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
AUTO_IGNORED_FILE = DATA_DIR / "trimbin_auto_ignored.json"
CONFIG_FILE = DATA_DIR / "trimbin_config.json"
TREE_FILE = DATA_DIR / "tree_scan.json"
TASTE_FILE = DATA_DIR / "taste_profile.json"
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
    ("MEDIA_LIBRARIES", "Media library paths (comma-separated)", "scans"),
    ("LB_AUTO_IGNORE_LIKED", "Auto-ignore liked films on Letterboxd", "auto_ignore"),
    ("LB_MIN_RATING_IGNORE", "Auto-ignore films rated at or above", "auto_ignore"),
    ("OLLAMA_URL", "Ollama server URL (e.g. http://ollama-host:11434)", "ai"),
    ("OLLAMA_MODEL", "Ollama model name (e.g. llama3.1:8b)", "ai"),
    ("OLLAMA_TEMPERATURE", "AI temperature (0.0-1.0, lower=more focused)", "ai"),
    ("OLLAMA_TIMEOUT", "AI request timeout in seconds", "ai"),
]

SELECT_KEYS = {"LB_AUTO_IGNORE_LIKED", "LB_MIN_RATING_IGNORE"}

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
<script src="https://d3js.org/d3.v7.min.js"></script>
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
.badge.auto-liked{background:#e94560;color:#fff}
.badge.auto-rated{background:#f39c12;color:#1a1a2e}
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
details[open] .collapse-arrow{transform:rotate(90deg)}
details summary::-webkit-details-marker{display:none}
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
.explorer-wrap{display:flex;flex-direction:column;gap:16px}
.treemap-container{width:100%;height:420px;background:#0d1b2a;border-radius:8px;overflow:hidden;position:relative}
.treemap-container svg{width:100%;height:100%}
.breadcrumbs{display:flex;gap:4px;align-items:center;flex-wrap:wrap;font-size:.8em;color:#888;margin-bottom:8px}
.breadcrumbs span{cursor:pointer;color:#00d474;padding:2px 6px;border-radius:3px}
.breadcrumbs span:hover{background:#0f3460}
.breadcrumbs span.current{color:#e0e0e0;cursor:default}
.breadcrumbs span.current:hover{background:transparent}
.color-modes{display:flex;gap:6px;margin-bottom:12px;flex-wrap:wrap}
.color-modes button{font-size:.75em;padding:4px 12px;border-radius:12px;border:1px solid #0f3460;background:transparent;color:#888;cursor:pointer}
.color-modes button.active{border-color:#00d474;color:#00d474;background:#0f346044}
.tree-list{max-height:400px;overflow-y:auto;background:#16213e;border-radius:8px;font-size:.85em}
.tree-row{display:flex;align-items:center;gap:8px;padding:6px 12px;border-bottom:1px solid #0f3460;cursor:pointer}
.tree-row:hover{background:#1e2d4a}
.tree-row .name{flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tree-row .pct-bar-wrap{width:80px;display:flex;align-items:center;gap:4px}
.tree-row .pct-bar-wrap .bar{flex:1;height:6px;background:#0f3460;border-radius:3px;overflow:hidden}
.tree-row .pct-bar-wrap .bar .fill{height:100%;border-radius:3px}
.tree-row .sz{color:#e94560;font-weight:600;min-width:70px;text-align:right;font-size:.9em}
.tree-row .age{color:#888;font-size:.8em;min-width:60px;text-align:right}
.tree-row .indent{display:inline-block}
.tree-row .toggle{width:16px;color:#666;text-align:center;flex-shrink:0}
.ai-panel{background:#16213e;border:1px solid #0f3460;border-radius:8px;padding:16px;margin-top:12px}
.ai-panel h3{color:#00d474;font-size:.9em;margin-bottom:10px;display:flex;align-items:center;gap:8px}
.ai-panel .ai-status{font-size:.75em;padding:2px 8px;border-radius:8px}
.ai-panel .ai-status.connected{background:#00d47433;color:#00d474;border:1px solid #00d47444}
.ai-panel .ai-status.disconnected{background:#e9456033;color:#e94560;border:1px solid #e9456044}
.ai-rec{padding:8px 12px;margin-bottom:6px;border-radius:6px;border-left:3px solid;font-size:.85em}
.ai-rec.keep{border-color:#00d474;background:#00d47411}
.ai-rec.consider{border-color:#f39c12;background:#f39c1211}
.ai-rec.delete{border-color:#e94560;background:#e9456011}
.ai-rec .title{font-weight:600;margin-bottom:2px}
.ai-rec .reason{color:#888;font-size:.9em}
.ai-rec .conf{float:right;font-size:.8em;color:#666}
.explorer-empty{text-align:center;padding:60px 20px;color:#666}
.explorer-empty button{margin-top:12px}
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
<div class="tab active" data-tab="movies" onclick="switchTab('movies')">Movies</div>
<div class="tab" data-tab="shows" onclick="switchTab('shows')">Shows</div>
<div class="tab" data-tab="duplicates" onclick="switchTab('duplicates')">Duplicates</div>
<div class="tab" data-tab="trickplay" onclick="switchTab('trickplay')">Trickplay</div>
<div class="tab" data-tab="cleanup" onclick="switchTab('cleanup')">Cleanup</div>
<div class="tab" data-tab="explorer" onclick="switchTab('explorer')">Explorer</div>
<div class="tab" data-tab="settings" onclick="switchTab('settings')">Settings</div>
</div>

<div id="tab-movies" class="tab-content active">
$movies_table
$ignored_section
</div>

<div id="tab-shows" class="tab-content">
$shows_table
$ignored_shows_section
</div>

<div id="tab-duplicates" class="tab-content">
$duplicates_html
</div>

<div id="tab-trickplay" class="tab-content">
$trickplay_html
</div>

<div id="tab-cleanup" class="tab-content">
$cleanup_html
</div>

<div id="tab-explorer" class="tab-content">
$explorer_html
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
let pendingRow = null;

function reloadToTab(tab) {
  window.location.hash = tab;
  location.reload();
}

function switchTab(name) {
  document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  document.querySelector('.tab[data-tab="' + name + '"]').classList.add('active');
  window.location.hash = name;
}

(function() {
  var h = window.location.hash.replace('#', '');
  if (h && document.getElementById('tab-' + h)) switchTab(h);
})();

function confirmTrimMovie(tmdbId, title, btn) {
  pendingUrl = '/api/trim/' + tmdbId;
  pendingTitle = title;
  pendingRow = btn ? btn.closest('tr') || btn.closest('.movie-row') : null;
  document.getElementById('confirmTitle').textContent = 'Trim movie?';
  document.getElementById('confirmText').textContent = 'Delete files for "' + title + '" and remove from Radarr?';
  document.getElementById('confirmOverlay').classList.add('active');
}

function confirmTrimShow(sonarrId, title, btn) {
  pendingUrl = '/api/trim-show/' + sonarrId;
  pendingTitle = title;
  pendingRow = btn ? btn.closest('tr') || btn.closest('.show-row') : null;
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
  var title = pendingTitle;
  var row = pendingRow;
  var isBulk = url.indexOf('/api/delete-category/') !== -1;
  closeConfirm();
  fetch(url, {method: 'POST'})
    .then(r => r.json())
    .then(d => {
      if (d.ok) {
        if (isBulk) {
          var msg = 'Deleted ' + (d.deleted || 0) + ' items';
          if (d.errors && d.errors.length) msg += ' (' + d.errors.length + ' errors)';
          showToast(msg);
          setTimeout(function(){ reloadToTab('cleanup'); }, 800);
        } else {
          showToast('Deleted: ' + title);
          if (row) {
            row.style.transition = 'opacity 0.3s'; row.style.opacity = '0';
            setTimeout(function(){
              var tbody = row.closest('tbody');
              row.remove();
              if (tbody && tbody.querySelectorAll('tr').length === 0) {
                var group = tbody.closest('div[style*="margin-bottom"]');
                if (group) { group.style.transition = 'opacity 0.3s'; group.style.opacity = '0'; setTimeout(function(){ group.remove(); }, 350); }
              }
            }, 350);
          }
        }
      } else {
        showToast('Error: ' + (d.error || 'unknown'));
      }
    })
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

function doIgnoreShow(tvdbId) {
  fetch('/api/ignore-show/' + tvdbId, {method: 'POST'})
    .then(r => r.json())
    .then(d => { if(d.ok) reloadToTab('shows'); else showToast('Error: ' + d.error); });
}

function doRestoreShow(tvdbId) {
  fetch('/api/unignore-show/' + tvdbId, {method: 'POST'})
    .then(r => r.json())
    .then(d => { if(d.ok) reloadToTab('shows'); else showToast('Error: ' + d.error); });
}

function saveSettings() {
  var form = document.getElementById('settingsForm');
  var data = {};
  form.querySelectorAll('input[data-key], select[data-key]').forEach(function(el) {
    var val = el.value.trim();
    if (val) data[el.dataset.key] = val;
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

function confirmDeletePath(pathHash, label, btn) {
  pendingUrl = '/api/delete-path/' + pathHash;
  pendingTitle = label;
  pendingRow = btn ? btn.closest('tr') || btn.closest('div') : null;
  document.getElementById('confirmTitle').textContent = 'Delete from disk?';
  document.getElementById('confirmText').textContent = 'Permanently delete "' + label + '"? This cannot be undone.';
  document.getElementById('confirmOverlay').classList.add('active');
}

function confirmDeleteAllCategory(category, count, label) {
  pendingUrl = '/api/delete-category/' + category;
  pendingTitle = count + ' ' + label;
  pendingRow = null;
  document.getElementById('confirmTitle').textContent = 'Delete all ' + label + '?';
  document.getElementById('confirmText').textContent = 'Permanently delete all ' + count + ' ' + label + '? This cannot be undone.';
  document.getElementById('confirmOverlay').classList.add('active');
}

function doIgnoreDedup(groupKey) {
  fetch('/api/ignore-dedup', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({key: groupKey})})
    .then(r => r.json())
    .then(d => { if(d.ok) reloadToTab('duplicates'); else showToast('Error: ' + d.error); });
}

function runStorageScan(type, btn) {
  btn.disabled = true;
  btn.innerHTML = '<span class="spin">&#x21bb;</span> Scanning...';
  var tabMap = {dedup: 'duplicates', trickplay: 'trickplay', cleanup: 'cleanup'};
  var tab = tabMap[type] || type;
  fetch('/api/scan-' + type, {method: 'POST'})
    .then(r => r.json())
    .then(d => {
      if (d.ok) { showToast('Scan complete'); reloadToTab(tab); }
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

/* === Explorer: D3 Treemap + Tree List + AI === */
var explorerData = null;
var explorerCurrent = null;
var explorerColorMode = 'size';
var explorerAiRecs = {};

function initExplorer() {
  var container = document.getElementById('explorer-treemap');
  if (!container) return;
  fetch('/api/tree').then(function(r){ return r.json(); }).then(function(data) {
    if (!data || !data.tree || !data.tree.children || data.tree.children.length === 0) return;
    explorerData = data.tree;
    explorerCurrent = explorerData;
    renderTreemap();
    renderTreeList();
    updateBreadcrumbs();
  }).catch(function(){});
}

function fmtSize(bytes) {
  if (bytes >= 1099511627776) return (bytes / 1099511627776).toFixed(1) + ' TB';
  if (bytes >= 1073741824) return (bytes / 1073741824).toFixed(1) + ' GB';
  if (bytes >= 1048576) return (bytes / 1048576).toFixed(0) + ' MB';
  return (bytes / 1024).toFixed(0) + ' KB';
}

function fmtAge(mtime) {
  if (!mtime) return '?';
  var days = Math.floor((Date.now() / 1000 - mtime) / 86400);
  if (days < 1) return 'today';
  if (days < 30) return days + 'd';
  if (days < 365) return Math.floor(days / 30) + 'mo';
  return Math.floor(days / 365) + 'y';
}

function getColor(node, mode) {
  if (mode === 'type') {
    var types = node.data.types || {};
    var max_type = '', max_val = 0;
    for (var t in types) { if (types[t] > max_val) { max_val = types[t]; max_type = t; } }
    var typeColors = {video:'#4a9eff', audio:'#00d474', subtitle:'#666', image:'#f39c12', metadata:'#888', junk:'#e94560', trickplay:'#9b59b6', other:'#555'};
    return typeColors[max_type] || '#444';
  }
  if (mode === 'age') {
    var mtime = node.data.mtime || 0;
    var days = (Date.now() / 1000 - mtime) / 86400;
    if (days < 30) return '#00d474';
    if (days < 180) return '#7ecb20';
    if (days < 365) return '#f39c12';
    if (days < 730) return '#e67e22';
    return '#e94560';
  }
  if (mode === 'ai') {
    var rec = explorerAiRecs[node.data.path];
    if (!rec) return '#333';
    if (rec.recommendation === 'keep') return '#00d474';
    if (rec.recommendation === 'consider_deleting') return '#f39c12';
    if (rec.recommendation === 'safe_to_delete') return '#e94560';
    return '#333';
  }
  // default: size gradient
  var root = explorerCurrent;
  var pct = root.size > 0 ? node.data.size / root.size : 0;
  var r = Math.floor(15 + pct * 200);
  var g = Math.floor(30 + (1 - pct) * 60);
  var b = Math.floor(80 - pct * 40);
  return 'rgb(' + r + ',' + g + ',' + b + ')';
}

function renderTreemap() {
  var container = document.getElementById('explorer-treemap');
  if (!container || !explorerCurrent) return;
  container.innerHTML = '';
  var w = container.clientWidth || 960;
  var h = container.clientHeight || 420;

  var root = d3.hierarchy(explorerCurrent)
    .sum(function(d) { return (!d.children || d.children.length === 0) ? d.size : 0; })
    .sort(function(a, b) { return b.value - a.value; });

  d3.treemap().size([w, h]).padding(2).round(true)(root);

  var svg = d3.select(container).append('svg').attr('width', w).attr('height', h);

  var leaves = root.leaves().filter(function(d) { return d.value > 0; });

  var cells = svg.selectAll('g').data(leaves).enter().append('g')
    .attr('transform', function(d) { return 'translate(' + d.x0 + ',' + d.y0 + ')'; });

  cells.append('rect')
    .attr('width', function(d) { return Math.max(0, d.x1 - d.x0); })
    .attr('height', function(d) { return Math.max(0, d.y1 - d.y0); })
    .attr('fill', function(d) { return getColor(d, explorerColorMode); })
    .attr('stroke', '#1a1a2e')
    .attr('stroke-width', 1)
    .style('cursor', 'pointer')
    .on('click', function(ev, d) {
      if (d.data.children && d.data.children.length > 0) {
        explorerCurrent = d.data;
        renderTreemap();
        renderTreeList();
        updateBreadcrumbs();
      }
    })
    .append('title')
    .text(function(d) {
      return d.data.name + '\n' + fmtSize(d.data.size) + ' | ' + (d.data.files || 0) + ' files | modified ' + fmtAge(d.data.mtime);
    });

  cells.append('text')
    .attr('x', 4).attr('y', 14)
    .attr('fill', '#e0e0e0').attr('font-size', '11px').attr('font-family', 'sans-serif')
    .text(function(d) {
      var rw = d.x1 - d.x0, rh = d.y1 - d.y0;
      if (rw < 50 || rh < 18) return '';
      var label = d.data.name;
      var maxChars = Math.floor(rw / 6.5);
      return label.length > maxChars ? label.substr(0, maxChars - 1) + '…' : label;
    });

  cells.append('text')
    .attr('x', 4).attr('y', 26)
    .attr('fill', '#888').attr('font-size', '9px').attr('font-family', 'sans-serif')
    .text(function(d) {
      var rw = d.x1 - d.x0, rh = d.y1 - d.y0;
      if (rw < 50 || rh < 30) return '';
      return fmtSize(d.data.size);
    });
}

function renderTreeList() {
  var container = document.getElementById('explorer-tree-list');
  if (!container || !explorerCurrent) return;
  var children = explorerCurrent.children || [];
  if (children.length === 0) { container.innerHTML = '<div style="padding:20px;color:#666">No subdirectories</div>'; return; }

  var html = '';
  var parentSize = explorerCurrent.size || 1;
  for (var i = 0; i < children.length && i < 100; i++) {
    var c = children[i];
    var pct = parentSize > 0 ? (c.size / parentSize * 100) : 0;
    var hasKids = c.children && c.children.length > 0;
    var ageColor = '#888';
    if (c.mtime) {
      var days = (Date.now() / 1000 - c.mtime) / 86400;
      if (days > 730) ageColor = '#e94560';
      else if (days > 365) ageColor = '#e67e22';
      else if (days > 180) ageColor = '#f39c12';
    }
    var fillColor = getColor({data: c}, explorerColorMode);
    html += '<div class="tree-row" onclick="explorerDrillInto(' + i + ')" title="' + (c.path || '').replace(/"/g, '&quot;') + '">';
    html += '<span class="toggle">' + (hasKids ? '&#9654;' : '&bull;') + '</span>';
    html += '<span class="name">' + escHtml(c.name) + '</span>';
    html += '<span class="pct-bar-wrap"><span class="bar"><span class="fill" style="width:' + Math.min(100, pct).toFixed(1) + '%;background:' + fillColor + '"></span></span><span style="font-size:.7em;color:#666">' + pct.toFixed(0) + '%</span></span>';
    html += '<span class="sz">' + fmtSize(c.size) + '</span>';
    html += '<span class="age" style="color:' + ageColor + '">' + fmtAge(c.mtime) + '</span>';
    html += '</div>';
  }
  container.innerHTML = html;
}

function escHtml(s) { var d = document.createElement('div'); d.textContent = s; return d.innerHTML; }

function updateBreadcrumbs() {
  var el = document.getElementById('explorer-breadcrumbs');
  if (!el) return;
  var path = [];
  var node = explorerCurrent;
  // walk up from current - we only have downward refs so rebuild from root
  path = buildPath(explorerData, explorerCurrent);
  var html = '';
  for (var i = 0; i < path.length; i++) {
    var isLast = (i === path.length - 1);
    if (isLast) {
      html += '<span class="current">' + escHtml(path[i].name) + '</span>';
    } else {
      html += '<span onclick="explorerNavigateTo(' + i + ')">' + escHtml(path[i].name) + '</span> / ';
    }
  }
  el.innerHTML = html;
}

function buildPath(root, target) {
  if (root === target) return [root];
  if (!root.children) return [];
  for (var i = 0; i < root.children.length; i++) {
    var p = buildPath(root.children[i], target);
    if (p.length > 0) { p.unshift(root); return p; }
  }
  return [];
}

function explorerNavigateTo(idx) {
  var path = buildPath(explorerData, explorerCurrent);
  if (idx < path.length) {
    explorerCurrent = path[idx];
    renderTreemap();
    renderTreeList();
    updateBreadcrumbs();
  }
}

function explorerDrillInto(childIdx) {
  var children = explorerCurrent.children || [];
  if (childIdx < children.length && children[childIdx].children && children[childIdx].children.length > 0) {
    explorerCurrent = children[childIdx];
    renderTreemap();
    renderTreeList();
    updateBreadcrumbs();
  }
}

function setColorMode(mode) {
  explorerColorMode = mode;
  document.querySelectorAll('.color-modes button').forEach(function(b) { b.classList.toggle('active', b.dataset.mode === mode); });
  renderTreemap();
  renderTreeList();
}

function runTreeScan(btn) {
  btn.disabled = true;
  btn.innerHTML = '<span class="spin">&#x21bb;</span> Scanning...';
  fetch('/api/scan-tree', {method: 'POST'})
    .then(function(r){ return r.json(); })
    .then(function(d) {
      if (d.ok) { showToast('Tree scan complete'); setTimeout(function(){ reloadToTab('explorer'); }, 800); }
      else { showToast('Error: ' + (d.error || 'unknown')); btn.disabled = false; btn.textContent = 'Scan'; }
    })
    .catch(function(e) { showToast('Error: ' + e); btn.disabled = false; btn.textContent = 'Scan'; });
}

function explorerAiAnalyze() {
  var statusEl = document.getElementById('ai-panel-status');
  var recsEl = document.getElementById('ai-recs-list');
  if (!explorerCurrent || !explorerCurrent.children) return;
  statusEl.textContent = 'Analyzing...';
  var items = explorerCurrent.children.slice(0, 20).map(function(c) {
    return {name: c.name, path: c.path, size: c.size, mtime: c.mtime, types: c.types, files: c.files};
  });
  fetch('/api/ai/analyze', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({items: items})})
    .then(function(r){ return r.json(); })
    .then(function(d) {
      if (d.error) { statusEl.textContent = 'Error: ' + d.error; return; }
      statusEl.textContent = d.model ? 'Model: ' + d.model : '';
      var html = '';
      (d.recommendations || []).forEach(function(rec) {
        var cls = rec.recommendation === 'keep' ? 'keep' : (rec.recommendation === 'safe_to_delete' ? 'delete' : 'consider');
        var emoji = rec.recommendation === 'keep' ? '&#9679;' : (rec.recommendation === 'safe_to_delete' ? '&#9679;' : '&#9679;');
        explorerAiRecs[rec.path] = rec;
        html += '<div class="ai-rec ' + cls + '">';
        html += '<span class="conf">' + Math.round((rec.confidence || 0) * 100) + '%</span>';
        html += '<div class="title">' + escHtml(rec.name) + ' (' + fmtSize(rec.size || 0) + ')</div>';
        html += '<div class="reason">' + escHtml(rec.reasoning || '') + '</div>';
        html += '</div>';
      });
      recsEl.innerHTML = html || '<div style="color:#666">No recommendations generated</div>';
      if (explorerColorMode === 'ai') { renderTreemap(); renderTreeList(); }
    })
    .catch(function(e) { statusEl.textContent = 'Error: ' + e; });
}

function checkAiStatus() {
  fetch('/api/ai/status').then(function(r){ return r.json(); }).then(function(d) {
    var badge = document.getElementById('ai-connection-badge');
    if (!badge) return;
    if (d.connected) {
      badge.className = 'ai-status connected';
      badge.textContent = d.models ? d.models.length + ' models' : 'connected';
    } else {
      badge.className = 'ai-status disconnected';
      badge.textContent = d.error || 'disconnected';
    }
  }).catch(function(){});
}

// Init explorer when tab is shown
(function() {
  var origSwitch = switchTab;
  switchTab = function(name) {
    origSwitch(name);
    if (name === 'explorer' && !explorerData) { initExplorer(); checkAiStatus(); }
  };
  // Re-check hash after override is in place
  var h = window.location.hash.replace('#', '');
  if (h === 'explorer') { initExplorer(); checkAiStatus(); }
})();
</script>
</body></html>""")


def slug_from_title(title, year):
    s = title.lower()
    s = "".join(c if c.isalnum() or c == " " else "" for c in s)
    return "-".join(s.split())


def _auto_ignored_cache():
    if not hasattr(_auto_ignored_cache, "_data"):
        _auto_ignored_cache._data = load_json(AUTO_IGNORED_FILE, {})
    return _auto_ignored_cache._data


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

    tmdb = m.get("tmdb_id", "")
    badge_auto = ""
    if ignored:
        auto_info = _auto_ignored_cache().get(str(tmdb), {})
        reason = auto_info.get("reason", "")
        if reason == "liked":
            badge_auto = '<span class="badge auto-liked">liked</span>'
        elif reason.startswith("rated"):
            badge_auto = f'<span class="badge auto-rated">{html.escape(reason)}</span>'

    slug = slug_from_title(m["title"], m.get("year", ""))
    title = html.escape(m["title"])
    year = m.get("year", "?")
    size = m["size_gb"]
    rid = m.get("radarr_id", "")
    title_js = html.escape(m["title"].replace("'", "\\'").replace('"', '\\"'))

    if ignored:
        actions = f'<button class="restore" onclick="doRestore({tmdb})">Restore</button>'
    else:
        arr_link = f'<a class="arr-link" href="{radarr_url}/movie/{rid}" target="_blank">Radarr</a>' if radarr_url else ''
        actions = (
            f'<button class="trim" onclick="confirmTrimMovie({tmdb}, \'{title_js}\', this)">Trim</button>'
            f'<button class="ignore" onclick="doIgnore({tmdb})">Ignore</button>'
            f'{arr_link}'
        )

    return (
        f'<tr><td><a class="movie-link" href="https://letterboxd.com/film/{slug}/" '
        f'target="_blank">{title}</a> <span class="year">({year})</span>'
        f'{badge_new}{badge_src}{badge_watch}{badge_auto}</td>'
        f'<td class="size">{size} GB</td>'
        f'<td class="actions">{actions}</td></tr>'
    )


IGNORED_SHOWS_FILE = DATA_DIR / "trimbin_ignored_shows.json"


def build_show_row(s, ignored=False):
    sonarr_url = get_sonarr_url()
    title = html.escape(s["title"])
    year = s.get("year", "?")
    size = s["size_gb"]
    sid = s.get("sonarr_id", "")
    tvdb = s.get("tvdb_id", "")
    pct = s.get("watched_pct", 0)
    w_eps = s.get("watched_episodes", 0)
    t_eps = s.get("total_episodes", 0)
    title_js = html.escape(s["title"].replace("'", "\\'").replace('"', '\\"'))

    pct_class = "pct-100" if pct >= 100 else ("pct-high" if pct >= 50 else "pct-low")

    pct_bar = (
        f'<span class="pct-bar {pct_class}"><span class="pct-fill" style="width:{min(pct,100)}%"></span></span>'
        f'{w_eps}/{t_eps} eps ({pct}%)'
    )

    if ignored:
        actions = f'<button class="restore" onclick="doRestoreShow({tvdb})">Restore</button>'
    else:
        arr_link = f'<a class="arr-link" href="{sonarr_url}/series/{sid}" target="_blank">Sonarr</a>' if sonarr_url else ''
        actions = (
            f'<button class="trim" onclick="confirmTrimShow({sid}, \'{title_js}\', this)">Trim</button>'
            f'<button class="ignore" onclick="doIgnoreShow({tvdb})">Ignore</button>'
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
        "scans": "Storage Scans",
        "auto_ignore": "Auto-Ignore",
    }

    parts = ['<form id="settingsForm" class="settings-form" onsubmit="event.preventDefault();saveSettings()">']
    for group_key in ["letterboxd", "radarr", "sonarr", "simkl", "jellystat", "notifications", "scans", "auto_ignore"]:
        if group_key not in groups:
            continue
        parts.append(f'<div class="settings-group"><h3>{group_titles.get(group_key, group_key)}</h3>')
        for key, label in groups[group_key]:
            val = config.get(key, "")
            env_val = os.environ.get(key, "")
            has_value = bool(val or env_val)
            dot_class = "set" if has_value else "unset"

            if key == "LB_AUTO_IGNORE_LIKED":
                sel_on = ' selected' if val == "true" else ''
                sel_off = ' selected' if val == "false" else ''
                sel_none = ' selected' if not val else ''
                parts.append(
                    f'<div class="field">'
                    f'<span class="status-dot {dot_class}"></span>'
                    f'<label>{html.escape(label)}</label>'
                    f'<select data-key="{key}" style="flex:1;background:#0f3460;border:1px solid #1e2d4a;'
                    f'border-radius:4px;padding:8px 12px;color:#e0e0e0;font-size:.85em">'
                    f'<option value=""{sel_none}>Disabled</option>'
                    f'<option value="true"{sel_on}>Enabled</option>'
                    f'<option value="false"{sel_off}>Disabled</option>'
                    f'</select></div>'
                )
            elif key == "LB_MIN_RATING_IGNORE":
                opts = ['<option value="">Disabled</option>']
                for r in [0.5, 1, 1.5, 2, 2.5, 3, 3.5, 4, 4.5, 5]:
                    rs = f"{r:g}"
                    sel = ' selected' if val == rs else ''
                    stars = int(r) * 2
                    half = r != int(r)
                    opts.append(f'<option value="{rs}"{sel}>{rs} stars</option>')
                parts.append(
                    f'<div class="field">'
                    f'<span class="status-dot {dot_class}"></span>'
                    f'<label>{html.escape(label)}</label>'
                    f'<select data-key="{key}" style="flex:1;background:#0f3460;border:1px solid #1e2d4a;'
                    f'border-radius:4px;padding:8px 12px;color:#e0e0e0;font-size:.85em">'
                    f'{"".join(opts)}'
                    f'</select></div>'
                )
            else:
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


DEDUP_FILE = DATA_DIR / "dedup_scan.json"
DEDUP_IGNORE_FILE = DATA_DIR / "dedup_ignore.json"
TRICKPLAY_FILE = DATA_DIR / "trickplay_scan.json"
CLEANUP_FILE = DATA_DIR / "cleanup_scan.json"


def path_hash(p):
    return hashlib.sha256(p.encode()).hexdigest()[:12]


def fmt_size(gb=None, size_bytes=None):
    if size_bytes is not None and (gb is None or gb == 0):
        if size_bytes < 1024:
            return f"{size_bytes} B"
        if size_bytes < 1024 * 1024:
            return f"{size_bytes / 1024:.1f} KB"
        if size_bytes < 1024 ** 3:
            return f"{size_bytes / (1024**2):.1f} MB"
        return f"{size_bytes / (1024**3):.1f} GB"
    if gb is not None:
        if gb < 0.01:
            if size_bytes is not None:
                return fmt_size(size_bytes=size_bytes)
            return "< 1 MB"
        if gb < 1.0:
            return f"{gb * 1024:.0f} MB"
        return f"{gb:.1f} GB"
    return "0 B"


def _no_libs_hint():
    libs = get_config("MEDIA_LIBRARIES")
    if not libs:
        return (
            '<div class="empty" style="margin-top:28px">'
            'Set <b>Media library paths</b> in Settings to enable storage scans. '
            'Comma-separated paths, e.g. <code>/media/movies,/media/tv,/media/anime</code>'
            '</div>'
        )
    return ""


def build_duplicates_html():
    parts = []
    dedup = load_json(DEDUP_FILE, {})
    dedup_scan_time = dedup.get("last_scan", "never")
    dupes = dedup.get("duplicates", [])
    parts.append(
        '<div style="display:flex;align-items:center;gap:12px;margin-bottom:12px;margin-top:12px">'
        '<h2 style="margin:0;border:none;padding:0">Duplicates</h2>'
        f'<button class="refresh-btn" onclick="runStorageScan(\'dedup\', this)">Scan</button>'
        f'<span style="color:#666;font-size:.8em">Last scan: {html.escape(dedup_scan_time)}</span>'
        '</div>'
    )
    if dupes:
        parts.append(
            f'<div class="summary" style="margin-bottom:16px">'
            f'<div class="stat"><div class="value">{len(dupes)}</div><div class="label">Duplicate groups</div></div>'
            f'<div class="stat danger"><div class="value">{fmt_size(gb=dedup.get("total_waste_gb", 0))}</div><div class="label">Reclaimable</div></div>'
            f'</div>'
        )
        by_lib = {}
        for d in dupes:
            libs_in_group = set(e.get("library", "other") for e in d.get("entries", []))
            lib_key = sorted(libs_in_group)[0] if libs_in_group else "other"
            by_lib.setdefault(lib_key, []).append(d)
        for lib_name in sorted(by_lib.keys()):
            parts.append(f'<h3 style="color:#888;margin:16px 0 8px;font-size:.9em;text-transform:capitalize">{html.escape(lib_name)}</h3>')
            for d in by_lib[lib_name][:50]:
                title = html.escape(d.get("title", "?"))
                year = d.get("year", "?")
                group_key = d.get("key", "")
                gk_js = html.escape(group_key.replace("'", "\\'").replace('"', '\\"'))
                parts.append(
                    f'<div style="margin-bottom:20px">'
                    f'<div style="display:flex;align-items:center;gap:10px;margin-bottom:6px">'
                    f'<strong>{title}</strong> <span class="year">({year})</span>'
                    f'<span style="color:#666;font-size:.8em">{d.get("copies", 0)} copies · {fmt_size(gb=d.get("total_gb", 0))} total</span>'
                    f'<button class="ignore" onclick="doIgnoreDedup(\'{gk_js}\')">Ignore</button>'
                    f'</div>'
                    f'<table style="margin-bottom:0"><thead><tr><th>Copy</th><th>Quality</th><th>Size</th><th></th></tr></thead><tbody>'
                )
                for e in d.get("entries", []):
                    dn = html.escape(e.get("dirname", "?"))
                    label = html.escape(e.get("label", e.get("quality", "?")))
                    sz = e.get("size_gb", 0)
                    lib = html.escape(e.get("library", ""))
                    ph = path_hash(e.get("path", ""))
                    dn_js = html.escape(dn.replace("'", "\\'").replace('"', '\\"'))
                    status = e.get("status", "complete")
                    video_count = e.get("video_files", 0)
                    total_count = e.get("total_files", 0)
                    partial_count = e.get("partial_files", 0)
                    status_badge = ''
                    if status == "partial":
                        status_badge = f' <span class="badge" style="background:#e6a817">INCOMPLETE ({partial_count} .part)</span>'
                    elif status == "no_media":
                        status_badge = ' <span class="badge new">NO VIDEO FILES</span>'
                    file_info = f'{video_count} video' if video_count == 1 else f'{video_count} videos'
                    file_info += f', {total_count} files total'
                    parts.append(
                        f'<tr><td style="font-size:.8em">{dn}{status_badge}'
                        f'<br><span style="color:#666;font-size:.85em">{lib} · {file_info}</span></td>'
                        f'<td>{label}</td>'
                        f'<td class="size">{fmt_size(gb=sz)}</td>'
                        f'<td class="actions"><button class="trim" onclick="confirmDeletePath(\'{ph}\', \'{dn_js}\', this)">Delete</button></td></tr>'
                    )
                parts.append('</tbody></table></div>')
    else:
        parts.append('<div class="empty">No duplicates found. Run a scan to check.</div>')
    hint = _no_libs_hint()
    if hint:
        parts.append(hint)
    return "\n".join(parts)


def build_trickplay_html():
    parts = []
    trick = load_json(TRICKPLAY_FILE, {})
    trick_scan_time = trick.get("last_scan", "never")
    flagged = trick.get("flagged", [])
    parts.append(
        '<div style="display:flex;align-items:center;gap:12px;margin-bottom:12px;margin-top:12px">'
        '<h2 style="margin:0;border:none;padding:0">Trickplay / BIF</h2>'
        f'<button class="refresh-btn" onclick="runStorageScan(\'trickplay\', this)">Scan</button>'
        f'<span style="color:#666;font-size:.8em">Last scan: {html.escape(trick_scan_time)}</span>'
        '</div>'
    )
    if flagged:
        parts.append(
            f'<div class="summary" style="margin-bottom:16px">'
            f'<div class="stat"><div class="value">{trick.get("total_trickplay_dirs", 0)}</div><div class="label">Trickplay dirs</div></div>'
            f'<div class="stat"><div class="value">{fmt_size(gb=trick.get("total_trickplay_gb", 0))}</div><div class="label">Total size</div></div>'
            f'<div class="stat danger"><div class="value">{trick.get("flagged_count", 0)}</div><div class="label">Flagged</div></div>'
            f'<div class="stat danger"><div class="value">{fmt_size(gb=trick.get("flagged_gb", 0))}</div><div class="label">Flagged size</div></div>'
            f'</div>'
        )
        parts.append('<table><thead><tr><th>Movie/Show</th><th>Issues</th><th>Size</th><th></th></tr></thead><tbody>')
        for f in flagged[:50]:
            movie = html.escape(f.get("movie", "?"))
            issue_badges = " ".join(
                f'<span class="badge {"new" if i == "contains_video" else "src"}">{html.escape(i)}</span>'
                for i in f.get("issues", [])
            )
            ph = path_hash(f.get("path", ""))
            movie_js = html.escape(movie.replace("'", "\\'").replace('"', '\\"'))
            parts.append(
                f'<tr><td>{movie}<br><span style="color:#666;font-size:.75em">{html.escape(f.get("library", ""))}</span></td>'
                f'<td>{issue_badges}</td>'
                f'<td class="size">{fmt_size(gb=f.get("size_gb", 0))}</td>'
                f'<td class="actions"><button class="trim" onclick="confirmDeletePath(\'{ph}\', \'{movie_js} trickplay\', this)">Delete</button></td></tr>'
            )
        parts.append('</tbody></table>')
    else:
        parts.append('<div class="empty">No trickplay issues found. Run a scan to check.</div>')
    hint = _no_libs_hint()
    if hint:
        parts.append(hint)
    return "\n".join(parts)


CLEANUP_CATEGORIES = {
    "executable": {
        "label": "Executables",
        "desc": "Windows executables (.exe/.bat/.scr/.msi) — security risk, never legitimate media. Full path shown for review.",
        "safe": True,
    },
    "os_junk": {
        "label": "OS Junk Files",
        "desc": "Thumbs.db, .DS_Store, desktop.ini — created by Windows/macOS, never part of media.",
        "safe": True,
    },
    "empty_dir": {
        "label": "Empty Directories",
        "desc": "Completely empty folders, usually left behind after a trim or failed download.",
        "safe": True,
    },
    "junk_dir": {
        "label": "Indexing Junk",
        "desc": "@eaDir, .@__thumb — created by Synology/NAS indexing software, not part of media.",
        "safe": True,
    },
    "scene_junk": {
        "label": "Scene / Release Junk",
        "desc": "RARBG.txt, .sfv, .srr, .torrent, .url — release-group promo files and checksums. Deleting will break seeding.",
        "safe": False,
    },
    "sample": {
        "label": "Sample Files",
        "desc": "Short preview clips bundled with releases. Deleting will break seeding for that torrent.",
        "safe": False,
    },
    "orphan_sub": {
        "label": "Orphan Subtitles",
        "desc": "Subtitle files (.srt/.ass/.sub) in directories with no video. May be part of a torrent.",
        "safe": False,
    },
    "orphan_nfo": {
        "label": "Orphan NFOs",
        "desc": "Release info files in directories with no video. Almost always part of the original torrent.",
        "safe": False,
    },
    "orphan_image": {
        "label": "Orphan Images",
        "desc": "Poster/backdrop images in directories with no video. Could be Jellyfin metadata or torrent extras.",
        "safe": False,
    },
}

DANGER_ORDER = ["executable"]
SAFE_ORDER = ["os_junk", "empty_dir", "junk_dir"]
RISKY_ORDER = ["scene_junk", "sample", "orphan_sub", "orphan_nfo", "orphan_image"]


def build_cleanup_html():
    parts = []
    cleanup = load_json(CLEANUP_FILE, {})
    cleanup_scan_time = cleanup.get("last_scan", "never")
    cleanup_items = cleanup.get("items", [])
    by_cat = cleanup.get("by_category", {})
    parts.append(
        '<div style="display:flex;align-items:center;gap:12px;margin-bottom:12px;margin-top:12px">'
        '<h2 style="margin:0;border:none;padding:0">Cleanup</h2>'
        f'<button class="refresh-btn" onclick="runStorageScan(\'cleanup\', this)">Scan</button>'
        f'<span style="color:#666;font-size:.8em">Last scan: {html.escape(cleanup_scan_time)}</span>'
        '</div>'
    )
    if cleanup_items:
        total_items = cleanup.get("total_items", 0)
        total_size = fmt_size(gb=cleanup.get("total_gb", 0))
        cat_chips = []
        for cat, info in by_cat.items():
            cat_info = CLEANUP_CATEGORIES.get(cat, {})
            cat_label = cat_info.get("label", cat)
            cat_chips.append(f'<span style="background:#0f3460;padding:3px 8px;border-radius:4px;font-size:.75em">'
                             f'{html.escape(cat_label)}: {info["count"]}</span>')
        parts.append(
            f'<div style="background:#16213e;border:1px solid #0f3460;border-radius:8px;padding:12px 16px;margin-bottom:16px">'
            f'<div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">'
            f'<span style="font-size:1.1em;font-weight:700;color:#e94560">{total_items} items · {total_size}</span>'
            f'<span style="color:#666">|</span>'
            f'{" ".join(cat_chips)}'
            f'</div></div>'
        )

        grouped = {}
        for item in cleanup_items:
            grouped.setdefault(item["category"], []).append(item)

        def render_section(title, cat_order, badge_color, badge_text):
            has_items = any(grouped.get(c) for c in cat_order)
            if not has_items:
                return
            parts.append(
                f'<h2 style="margin:20px 0 4px;border:none;padding:0;font-size:1em;color:#ccc">{title}'
                f' <span class="badge" style="background:{badge_color};font-size:.7em;vertical-align:middle;margin-left:6px">{badge_text}</span></h2>'
            )
            for cat in cat_order:
                cat_items = grouped.get(cat, [])
                if not cat_items:
                    continue
                cat_info = CLEANUP_CATEGORIES.get(cat, {})
                cat_label = cat_info.get("label", cat)
                cat_desc = cat_info.get("desc", "")
                cat_bytes = sum(i["size_bytes"] for i in cat_items)
                parts.append(
                    f'<details style="margin:8px 0">'
                    f'<summary style="cursor:pointer;list-style:none;display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:8px 0">'
                    f'<span style="color:#666;font-size:.85em;transition:transform .2s" class="collapse-arrow">&#9654;</span>'
                    f'<span style="color:#ccc;font-weight:600;font-size:.9em">'
                    f'{html.escape(cat_label)}</span>'
                    f'<span style="color:#666;font-size:.8em">{len(cat_items)} items · {fmt_size(size_bytes=cat_bytes)}</span>'
                    f'<button class="trim" style="font-size:.75em;padding:3px 10px" '
                    f'onclick="event.stopPropagation();confirmDeleteAllCategory(\'{cat}\', {len(cat_items)}, \'{html.escape(cat_label)}\')">'
                    f'Delete All</button>'
                    f'</summary>'
                    f'<p style="color:#666;font-size:.8em;margin:4px 0 8px 20px">{html.escape(cat_desc)}</p>'
                )
                parts.append('<table><thead><tr><th>Item</th><th>Size</th><th></th></tr></thead><tbody>')
                for item in cat_items[:100]:
                    label = html.escape(item.get("label", "?"))
                    lib = html.escape(item.get("library", ""))
                    sz_display = fmt_size(gb=item.get("size_gb", 0), size_bytes=item.get("size_bytes", 0))
                    ph = path_hash(item.get("path", ""))
                    label_js = html.escape(label.replace("'", "\\'").replace('"', '\\"'))
                    is_dir = item.get("is_dir", False)
                    icon = "&#128193;" if is_dir else "&#128196;"
                    parts.append(
                        f'<tr><td>{icon} {label}'
                        f'<br><span style="color:#666;font-size:.75em">{lib}</span></td>'
                        f'<td class="size">{sz_display}</td>'
                        f'<td class="actions"><button class="trim" onclick="confirmDeletePath(\'{ph}\', \'{label_js}\', this)">Delete</button></td></tr>'
                    )
                parts.append('</tbody></table></details>')

        render_section("Security concern — should not be in media libraries", DANGER_ORDER, "#e04040", "DANGER")
        render_section("Safe to delete", SAFE_ORDER, "#00d474", "SAFE")
        render_section("May affect torrent seeding", RISKY_ORDER, "#e6a817", "CAUTION")
    else:
        parts.append('<div class="empty">No cleanup items found. Run a scan to check.</div>')
    hint = _no_libs_hint()
    if hint:
        parts.append(hint)
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


def resolve_path_hash(phash):
    """Find a filesystem path matching a hash across all scan results."""
    for scan_file in [DEDUP_FILE, TRICKPLAY_FILE]:
        data = load_json(scan_file, {})
        for group in data.get("duplicates", []):
            for entry in group.get("entries", []):
                if path_hash(entry.get("path", "")) == phash:
                    return entry["path"], entry.get("dirname", "")
        for item in data.get("flagged", []):
            if path_hash(item.get("path", "")) == phash:
                return item["path"], item.get("movie", "")
    cleanup = load_json(CLEANUP_FILE, {})
    for item in cleanup.get("items", []):
        if path_hash(item.get("path", "")) == phash:
            return item["path"], item.get("label", "")
    return None, None


def delete_path(phash):
    target_path, label = resolve_path_hash(phash)
    if not target_path:
        return False, "Path not found in scan results"
    if not os.path.exists(target_path):
        return False, "Path no longer exists on disk"
    if not any(target_path.startswith(p) for p in
               [p.strip() for p in get_config("MEDIA_LIBRARIES").split(",") if p.strip()]):
        return False, "Path is not under a configured media library"
    try:
        if os.path.isdir(target_path):
            shutil.rmtree(target_path)
        else:
            os.remove(target_path)
    except Exception as e:
        return False, str(e)
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


def run_dedup_scan():
    try:
        result = subprocess.run(
            ["python3", "/app/dedup-scan.py"],
            capture_output=True, text=True, timeout=300,
        )
        return result.returncode == 0, result.stderr[-500:] if result.returncode != 0 else "ok"
    except subprocess.TimeoutExpired:
        return False, "scan timed out"
    except Exception as e:
        return False, str(e)


def run_trickplay_scan():
    try:
        result = subprocess.run(
            ["python3", "/app/trickplay-scan.py"],
            capture_output=True, text=True, timeout=300,
        )
        return result.returncode == 0, result.stderr[-500:] if result.returncode != 0 else "ok"
    except subprocess.TimeoutExpired:
        return False, "scan timed out"
    except Exception as e:
        return False, str(e)


def run_cleanup_scan():
    try:
        result = subprocess.run(
            ["python3", "/app/cleanup-scan.py"],
            capture_output=True, text=True, timeout=300,
        )
        return result.returncode == 0, result.stderr[-500:] if result.returncode != 0 else "ok"
    except subprocess.TimeoutExpired:
        return False, "scan timed out"
    except Exception as e:
        return False, str(e)


# === Explorer + AI ===

def build_explorer_html():
    tree_data = load_json(TREE_FILE, {})
    scan_time = tree_data.get("last_scan", "never")
    total = tree_data.get("total_size", 0)
    total_files = tree_data.get("total_files", 0)
    total_dirs = tree_data.get("total_dirs", 0)

    has_data = total > 0

    if has_data:
        summary = (
            '<div style="display:flex;align-items:center;gap:12px;margin-bottom:12px;margin-top:12px">'
            '<h2 style="margin:0;border:none;padding:0">Storage Explorer</h2>'
            f'<button class="refresh-btn" onclick="runTreeScan(this)">Scan</button>'
            f'<span style="color:#666;font-size:.8em">Last scan: {html.escape(scan_time)} &mdash; '
            f'{fmt_size(gb=total / (1024**3))} / {total_files:,} files / {total_dirs:,} dirs</span>'
            '</div>'
        )
    else:
        return (
            '<div class="explorer-empty">'
            '<h2>Storage Explorer</h2>'
            '<p>No tree scan data yet. Run a scan to visualize your media library.</p>'
            '<button class="refresh-btn" onclick="runTreeScan(this)">Scan Now</button>'
            '</div>'
        )

    return f"""{summary}
<div class="explorer-wrap">
<div class="breadcrumbs" id="explorer-breadcrumbs"></div>
<div class="color-modes">
<button class="active" data-mode="size" onclick="setColorMode('size')">By Size</button>
<button data-mode="age" onclick="setColorMode('age')">By Age</button>
<button data-mode="type" onclick="setColorMode('type')">By Type</button>
<button data-mode="ai" onclick="setColorMode('ai')">By AI Rec</button>
</div>
<div class="treemap-container" id="explorer-treemap"></div>
<div class="tree-list" id="explorer-tree-list"></div>
<div class="ai-panel">
<h3>AI Recommendations <span class="ai-status disconnected" id="ai-connection-badge">checking...</span></h3>
<p style="font-size:.8em;color:#666;margin-bottom:10px">Analyzes the current directory's contents against your taste profile to suggest what's safe to delete.</p>
<button class="refresh-btn" onclick="explorerAiAnalyze()" style="margin-bottom:10px">Analyze Current View</button>
<div id="ai-panel-status" style="font-size:.8em;color:#888;margin-bottom:8px"></div>
<div id="ai-recs-list"></div>
</div>
</div>"""


def run_tree_scan():
    try:
        result = subprocess.run(
            ["python3", str(APP_DIR / "tree-scan.py")],
            capture_output=True, text=True, timeout=600,
        )
        return result.returncode == 0, result.stderr[-500:] if result.returncode != 0 else "ok"
    except subprocess.TimeoutExpired:
        return False, "tree scan timed out"
    except Exception as e:
        return False, str(e)


def ollama_call(messages, schema=None):
    """Call Ollama API with optional schema-constrained output."""
    config = load_json(CONFIG_FILE, {})
    url = config.get("OLLAMA_URL", "").rstrip("/")
    model = config.get("OLLAMA_MODEL", "llama3.1:8b")
    temperature = float(config.get("OLLAMA_TEMPERATURE", "0.3"))
    timeout = int(config.get("OLLAMA_TIMEOUT", "60"))

    if not url:
        return None, "OLLAMA_URL not configured"

    payload = {
        "model": model,
        "stream": False,
        "messages": messages,
        "options": {"temperature": temperature, "num_predict": 512},
    }
    if schema:
        payload["format"] = schema

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{url}/api/chat", data=data,
        headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read())
            content = body.get("message", {}).get("content", "")
            if schema:
                return json.loads(content), None
            return content, None
    except urllib.error.URLError as e:
        return None, f"Connection failed: {e.reason}"
    except Exception as e:
        return None, str(e)


def ollama_check():
    """Check Ollama connection, return model list or error."""
    config = load_json(CONFIG_FILE, {})
    url = config.get("OLLAMA_URL", "").rstrip("/")
    if not url:
        return {"connected": False, "error": "OLLAMA_URL not configured"}
    try:
        req = urllib.request.Request(f"{url}/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            models = [m["name"] for m in data.get("models", [])]
            return {"connected": True, "models": models}
    except Exception as e:
        return {"connected": False, "error": str(e)}


def compute_taste_profile():
    """Build a media taste profile from Trimbin's watch/trim/ignore data."""
    movies = load_json(WATCHED_LIST_FILE, [])
    shows = load_json(SHOWS_LIST_FILE, [])
    ignored_ids = set(load_json(IGNORED_FILE, []))
    trimmed = load_json(DATA_DIR / "trimbin_trimmed.json", {"count": 0, "gb": 0})
    trim_log = load_json(DATA_DIR / "trimbin_trim_log.json", [])

    genre_affinity = {}
    kept_titles = []
    trimmed_titles = [t.get("title", "") for t in trim_log]

    radarr_genres = {}
    sonarr_genres = {}
    try:
        radarr_url = get_radarr_url()
        radarr_key = get_config("RADARR_API_KEY")
        if radarr_url and radarr_key:
            for rm in (arr_api(radarr_url, radarr_key, "GET", "/movie") or []):
                radarr_genres[rm.get("tmdbId")] = [g.get("name", "") for g in rm.get("genres", [])]
    except Exception:
        pass
    try:
        sonarr_url = get_sonarr_url()
        sonarr_key = get_config("SONARR_API_KEY")
        if sonarr_url and sonarr_key:
            for ss in (arr_api(sonarr_url, sonarr_key, "GET", "/series") or []):
                sonarr_genres[ss.get("tvdbId")] = [g.get("name", g) if isinstance(g, dict) else g for g in ss.get("genres", [])]
    except Exception:
        pass

    for m in movies:
        title = m.get("title", "")
        genres = m.get("genres", [])
        if isinstance(genres, str):
            genres = [g.strip() for g in genres.split(",") if g.strip()]
        if not genres:
            genres = radarr_genres.get(m.get("tmdb_id"), [])
        is_ignored = m.get("tmdb_id") in ignored_ids

        if is_ignored:
            for g in genres:
                genre_affinity[g] = genre_affinity.get(g, 0) + 2
            kept_titles.append(title)
        else:
            for g in genres:
                genre_affinity[g] = genre_affinity.get(g, 0) + 1

    for s in shows:
        genres = s.get("genres", [])
        if isinstance(genres, str):
            genres = [g.strip() for g in genres.split(",") if g.strip()]
        if not genres:
            genres = sonarr_genres.get(s.get("tvdb_id"), [])
        for g in genres:
            genre_affinity[g] = genre_affinity.get(g, 0) + 1

    avg_size = 0
    sizes = [m.get("size_gb", 0) for m in movies if m.get("size_gb", 0) > 0]
    if sizes:
        avg_size = sum(sizes) / len(sizes)

    profile = {
        "genre_affinity": genre_affinity,
        "total_movies_watched": len(movies),
        "total_shows_watched": len(shows),
        "avg_movie_size_gb": round(avg_size, 1),
        "total_trimmed": trimmed.get("count", 0),
        "trimmed_gb": trimmed.get("gb", 0),
        "kept_count": len(ignored_ids),
        "sample_kept": kept_titles[:10],
        "sample_trimmed": trimmed_titles[:10],
        "computed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    save_json(TASTE_FILE, profile)
    return profile


def ai_analyze_items(items):
    """Get AI recommendations for a list of media items."""
    profile = load_json(TASTE_FILE, {})
    if not profile:
        profile = compute_taste_profile()

    config = load_json(CONFIG_FILE, {})
    model = config.get("OLLAMA_MODEL", "llama3.1:8b")

    top_genres = sorted(profile.get("genre_affinity", {}).items(), key=lambda x: x[1], reverse=True)[:10]
    genre_str = ", ".join(f"{g} ({c})" for g, c in top_genres)
    trimmed_str = ", ".join(profile.get("sample_trimmed", [])[:5]) or "none yet"
    kept_str = ", ".join(profile.get("sample_kept", [])[:5]) or "none yet"

    items_desc = "\n".join(
        f"- {it['name']} | {it['size'] / (1024**3):.1f} GB | "
        f"modified {time.strftime('%Y-%m-%d', time.localtime(it.get('mtime', 0)))} | "
        f"files: {it.get('files', '?')} | "
        f"types: {', '.join(f'{k}={v/(1024**3):.1f}GB' for k, v in (it.get('types') or {}).items())}"
        for it in items[:20]
    )

    system_prompt = """You are a media library curator helping a user decide what to delete from their personal media server to free up disk space. You analyze media items and provide keep/delete recommendations based on the user's taste profile.

Be practical: large files that don't align with taste are the best candidates for deletion. Recently modified files are more likely to be actively watched. Items with only metadata/subtitles and no video are likely orphans.

Respond with a JSON array of recommendations, one per item, in the same order as the input."""

    user_prompt = f"""## User's Taste Profile
- Top genres: {genre_str}
- Average movie size: {profile.get('avg_movie_size_gb', 0)} GB
- Previously trimmed: {trimmed_str}
- Explicitly kept: {kept_str}
- Total trimmed: {profile.get('total_trimmed', 0)} items ({profile.get('trimmed_gb', 0)} GB)

## Items to Analyze (current directory)
{items_desc}

For each item, recommend: "keep", "consider_deleting", or "safe_to_delete" with confidence (0-1) and a brief reasoning (one sentence). Return a JSON array."""

    schema = {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "recommendation": {"type": "string", "enum": ["keep", "consider_deleting", "safe_to_delete"]},
                "confidence": {"type": "number"},
                "reasoning": {"type": "string"},
            },
            "required": ["name", "recommendation", "confidence", "reasoning"],
        },
    }

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    result, error = ollama_call(messages, schema=schema)
    if error:
        return {"error": error}

    recs = []
    if isinstance(result, list):
        for i, rec in enumerate(result):
            if i < len(items):
                rec["path"] = items[i].get("path", "")
                rec["size"] = items[i].get("size", 0)
                if "name" not in rec:
                    rec["name"] = items[i].get("name", "?")
            recs.append(rec)

    return {"recommendations": recs, "model": model}


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
        elif self.path == "/api/tree":
            data = load_json(TREE_FILE, {})
            self._json_response(data)
        elif self.path == "/api/ai/status":
            self._json_response(ollama_check())
        elif self.path == "/api/ai/profile":
            profile = compute_taste_profile()
            self._json_response({"ok": True, "profile": profile})
        elif self.path == "/ping":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"pong")
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        try:
            self._route_post()
        except Exception as e:
            try:
                self._json_response({"ok": False, "error": str(e)}, status=500)
            except Exception:
                pass

    def _route_post(self):
        if self.path == "/api/scan":
            ok, msg = run_scan()
            self._json_response({"ok": ok, "error": msg if not ok else None})
        elif self.path == "/api/scan-dedup":
            ok, msg = run_dedup_scan()
            self._json_response({"ok": ok, "error": msg if not ok else None})
        elif self.path == "/api/scan-trickplay":
            ok, msg = run_trickplay_scan()
            self._json_response({"ok": ok, "error": msg if not ok else None})
        elif self.path == "/api/scan-cleanup":
            ok, msg = run_cleanup_scan()
            self._json_response({"ok": ok, "error": msg if not ok else None})
        elif self.path == "/api/scan-tree":
            ok, msg = run_tree_scan()
            self._json_response({"ok": ok, "error": msg if not ok else None})
        elif self.path == "/api/ai/analyze":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            items = body.get("items", [])
            if not items:
                self._json_response({"error": "No items provided"})
            else:
                result = ai_analyze_items(items)
                self._json_response(result)
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
            auto_ignored = load_json(AUTO_IGNORED_FILE, {})
            if str(tmdb_id) in auto_ignored:
                auto_ignored[str(tmdb_id)]["restored"] = True
                save_json(AUTO_IGNORED_FILE, auto_ignored)
            if hasattr(_auto_ignored_cache, "_data"):
                del _auto_ignored_cache._data
            self._json_response({"ok": True})
        elif self.path.startswith("/api/ignore-show/"):
            tvdb_id = int(self.path.split("/")[-1])
            ignored = set(load_json(IGNORED_SHOWS_FILE, []))
            ignored.add(tvdb_id)
            save_json(IGNORED_SHOWS_FILE, sorted(ignored))
            self._json_response({"ok": True})
        elif self.path.startswith("/api/unignore-show/"):
            tvdb_id = int(self.path.split("/")[-1])
            ignored = set(load_json(IGNORED_SHOWS_FILE, []))
            ignored.discard(tvdb_id)
            save_json(IGNORED_SHOWS_FILE, sorted(ignored))
            self._json_response({"ok": True})
        elif self.path.startswith("/api/delete-path/"):
            phash = self.path.split("/")[-1]
            ok, msg = delete_path(phash)
            self._json_response({"ok": ok, "error": msg if not ok else None})
        elif self.path.startswith("/api/delete-category/"):
            category = self.path.split("/")[-1]
            cleanup = load_json(CLEANUP_FILE, {})
            items = [i for i in cleanup.get("items", []) if i.get("category") == category]
            if not items:
                self._json_response({"ok": False, "error": "No items in category"})
                return
            deleted = 0
            errors = []
            for item in items:
                ph = path_hash(item.get("path", ""))
                ok, msg = delete_path(ph)
                if ok:
                    deleted += 1
                elif "no longer exists" not in msg:
                    errors.append(msg)
            self._json_response({"ok": True, "deleted": deleted, "errors": errors[:5]})
        elif self.path == "/api/ignore-dedup":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            key = body.get("key", "")
            if key:
                ignored = set(load_json(DEDUP_IGNORE_FILE, []))
                ignored.add(key)
                save_json(DEDUP_IGNORE_FILE, sorted(ignored))
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
        if hasattr(_auto_ignored_cache, "_data"):
            del _auto_ignored_cache._data
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

        ignored_show_ids = set(load_json(IGNORED_SHOWS_FILE, []))
        active_shows = [s for s in shows if s.get("tvdb_id") not in ignored_show_ids]
        ignored_shows = [s for s in shows if s.get("tvdb_id") in ignored_show_ids]
        shows_gb = sum(s["size_gb"] for s in active_shows)

        if active_shows:
            s_rows = "\n".join(build_show_row(s) for s in active_shows)
            shows_table = (
                "<table><thead><tr><th>Show</th><th>Progress</th><th>Size</th><th>Actions</th></tr></thead>"
                f"<tbody>{s_rows}</tbody></table>"
            )
        else:
            shows_table = '<div class="empty">No watched shows on disk. Enable Simkl + Sonarr to track shows.</div>'

        if ignored_shows:
            ig_s_rows = "\n".join(build_show_row(s, ignored=True) for s in ignored_shows)
            ignored_shows_html = (
                '<div class="ignored-section">'
                f'<h2>Ignored ({len(ignored_shows)} shows, {sum(s["size_gb"] for s in ignored_shows):.0f} GB)</h2>'
                "<table><thead><tr><th>Show</th><th>Progress</th><th>Size</th><th>Actions</th></tr></thead>"
                f"<tbody>{ig_s_rows}</tbody></table></div>"
            )
        else:
            ignored_shows_html = ""

        page = PAGE_TEMPLATE.substitute(
            last_run=html.escape(status.get("last_run", "never")),
            movies_count=len(active_movies),
            movies_gb=round(active_gb),
            shows_count=len(active_shows),
            shows_gb=round(shows_gb),
            new_count=status.get("new_since_last", 0),
            trimmed_gb=trimmed.get("gb", 0),
            movies_table=movies_table,
            ignored_section=ignored_html,
            shows_table=shows_table,
            ignored_shows_section=ignored_shows_html,
            duplicates_html=build_duplicates_html(),
            trickplay_html=build_trickplay_html(),
            cleanup_html=build_cleanup_html(),
            explorer_html=build_explorer_html(),
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
