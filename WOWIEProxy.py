#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import time
import requests
import tempfile
import zipfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, send_file, abort, jsonify, request

FINGERPRINT_DIR = "BS-FINGERPRINTS"
CACHE_DIR = "cache_and_downloads"

ASSET_BASES = [
    "https://game-assets.brawlstarsgame.com/",
    "https://game-assets.tencent-cloud.com/"
]

TIMEOUT = 15
MAX_THREADS = 16
RATE_LIMIT_MAX = 20  # requests per minute per IP

app = Flask(__name__)

progress = {}
progress_lock = threading.Lock()

rate_limit_cache = {}  # {ip: [(timestamps)]}
rate_limit_lock = threading.Lock()


# --------------------------------------------------
# Rate Limit
# --------------------------------------------------

def rate_limit(ip):
    now = time.time()
    window = 60  # 1 minute

    with rate_limit_lock:
        entries = rate_limit_cache.get(ip, [])
        entries = [t for t in entries if now - t < window]
        if len(entries) >= RATE_LIMIT_MAX:
            return False
        entries.append(now)
        rate_limit_cache[ip] = entries
        return True


@app.before_request
def check_rate_limit():
    ip = request.remote_addr or "unknown"
    # exclude heavy polling endpoints and CDN static access
    if request.path.startswith("/cdn"):
        return
    if request.path.startswith("/static"):
        return
    if request.path.startswith("/progress"):
        return

    if not rate_limit(ip):
        return jsonify({"error": "Rate limit exceeded"}), 429


def ensure_dir(path):
    os.makedirs(path, exist_ok=True)


# --------------------------------------------------
# Fingerprint loader
# --------------------------------------------------

def load_all_fingerprints():
    versions = {}

    if not os.path.isdir(FINGERPRINT_DIR):
        return versions

    for fn in os.listdir(FINGERPRINT_DIR):
        if not fn.endswith(".json"):
            continue

        full = os.path.join(FINGERPRINT_DIR, fn)

        try:
            with open(full, "r", encoding="utf-8") as f:
                data = json.load(f)
        except:
            continue

        assets = data.get("files") or data.get("assets") or []
        normalized = []

        for a in assets:
            file = a.get("file")
            sha = a.get("sha256") or a.get("sha")
            if file and sha:
                normalized.append({"file": file, "sha": sha})

        master_sha = data.get("sha")
        if not master_sha and normalized:
            master_sha = normalized[-1]["sha"]

        if not master_sha:
            continue

        versions[master_sha] = {
            "file": full,
            "assets": normalized,
            "master_sha": master_sha
        }

    return versions


# --------------------------------------------------
# Asset resolver
# --------------------------------------------------

def resolve_asset(master_sha, file_path):
    cache_path = os.path.join(CACHE_DIR, master_sha, file_path)
    ensure_dir(os.path.dirname(cache_path))

    if os.path.exists(cache_path):
        return cache_path

    for base in ASSET_BASES:
        url = f"{base}{master_sha}/{file_path}"

        try:
            r = requests.get(url, timeout=TIMEOUT, stream=True)
            if r.status_code == 200:
                with open(cache_path, "wb") as f:
                    for chunk in r.iter_content(65536):
                        f.write(chunk)
                return cache_path
        except:
            pass

    return None


# --------------------------------------------------
# Web UI
# --------------------------------------------------

@app.route("/")
def root():
    return "<h1>Frontview</h1><a href='/explorer'>Open Explorer</a>"


@app.route("/explorer")
def explorer_home():
    versions = load_all_fingerprints()
    list_items = "".join(f"<li><a href='/explorer/{sha}'>{sha}</a></li>" for sha in versions)
    html = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Asset Explorer</title>
<style>
:root{
  --bg:#ffffff; --fg:#0b0b0b; --card:#f3f6fa; --border:#dcdfe6; --accent:#0066ff;
  --status-color:#333; --muted:#666;
}
.dark {
  --bg:#0b0d10; --fg:#e6eef8; --card:#0f1418; --border:#23303a; --accent:#4da3ff;
  --status-color:#ffd54a; --muted:#9aa5b1;
}
body{background:var(--bg);color:var(--fg);font-family:Inter,Arial,Helvetica,sans-serif;margin:0;padding:20px;}
.header{display:flex;align-items:center;justify-content:space-between;gap:12px}
.card{background:var(--card);padding:16px;border-radius:10px;border:1px solid var(--border)}
a{color:var(--accent)}
button{cursor:pointer;border-radius:8px;padding:8px 12px;border:1px solid var(--border);background:transparent;color:var(--fg)}
#themeToggle{position:fixed;right:18px;top:18px;padding:8px 12px}
ul{margin:10px 0 0 20px}
li{margin:6px 0}
</style>
</head>
<body>
<button id="themeToggle">🌙</button>
<div class="card">
  <div class="header">
    <div>
      <h1 style="margin:0 0 6px 0">Welcome to BS-GRIP Web Version!</h1>
      <div style="color:var(--muted)">Select a master SHA from your fingerprints folder, if you want to download an asset of your desired version, go to the version you want, for example 26.165, go into /assets (if you want to get using apk) then open fingerprints.json, go to the end of the file, until ou see something like sha:"3c70ffc507e8aecc9b7ea8136a2871a398f0beda, thats the master sha of v26, once you really selected the version you want, select these master sha below, also heres an advice, since this contain almost fingerprints of every version of brawl stars, some may have been deleted, like 1.1714.'</div>
    </div>
  </div>

  <hr style="margin:12px 0">
  <ul>
  {list_items}
  </ul>
</div>

<script>
(function(){
  const toggle = document.getElementById("themeToggle");
  const saved = localStorage.getItem("bsgrip_theme") || "light";
  function applyTheme(t){
    if(t==="dark"){ document.documentElement.classList.add("dark"); toggle.innerText="☀️"; }
    else { document.documentElement.classList.remove("dark"); toggle.innerText="🌙"; }
  }
  applyTheme(saved);
  toggle.addEventListener("click", ()=>{
    const next = document.documentElement.classList.contains("dark") ? "light" : "dark";
    localStorage.setItem("bsgrip_theme", next);
    applyTheme(next);
  });
})();
</script>
</body>
</html>"""
    return html.replace("{list_items}", list_items)


@app.route("/explorer/<master_sha>")
def explorer_sha(master_sha):
    versions = load_all_fingerprints()
    if master_sha not in versions:
        return "SHA not found."

    # initialize progress immediately so /progress returns correct total
    with progress_lock:
        progress[master_sha] = {
            "total": len(versions[master_sha]["assets"]),
            "done": 0
        }

    # build tree HTML
    tree = {}
    for a in versions[master_sha]["assets"]:
        parts = a["file"].split("/")
        node = tree
        for p in parts:
            node = node.setdefault(p, {})

    def render(node, prefix=""):
        html = "<ul>"
        for name, sub in sorted(node.items()):
            path = prefix + name
            if sub:
                html += f"<li><b>{name}/</b> {render(sub, path + '/')}</li>"
            else:
                url = f"/cdn/{master_sha}/{path}"
                html += f"<li><a href='{url}' target='_blank'>{name}</a></li>"
        html += "</ul>"
        return html

    tree_html = render(tree)

    template = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>BS-GRIP - {master_sha}</title>
<style>
:root{
  --bg:#ffffff; --fg:#0b0b0b; --card:#f3f6fa; --border:#dcdfe6; --accent:#0066ff;
  --status-color:#333; --muted:#666;
}
.dark {
  --bg:#0b0d10; --fg:#e6eef8; --card:#0f1418; --border:#23303a; --accent:#4da3ff;
  --status-color:#ffd54a; --muted:#9aa5b1;
}
body{background:var(--bg);color:var(--fg);font-family:Inter,Arial,Helvetica,sans-serif;margin:0;padding:20px;}
.header{display:flex;align-items:center;justify-content:space-between;gap:12px}
.card{background:var(--card);padding:16px;border-radius:10px;border:1px solid var(--border)}
a{color:var(--accent)}
button{cursor:pointer;border-radius:8px;padding:8px 12px;border:1px solid var(--border);background:transparent;color:var(--fg)}
#themeToggle{position:fixed;right:18px;top:18px;padding:8px 12px}
.status{font-weight:bold;margin:10px 0;color:var(--status-color);font-size:16px;text-shadow:1px 1px 0 #0002}
</style>
</head>
<body>
<button id="themeToggle">🌙</button>
<div class="card">
  <div style="display:flex;justify-content:space-between;align-items:center;">
    <div>
      <h2 style="margin:0">Asset Explorer</h2>
      <div style="color:var(--muted);margin-top:6px"><code>{master_sha}</code></div>
    </div>
    <div>
      <button id="downloadBtn">Download All (ZIP)</button>
    </div>
  </div>

  <div id="status" class="status">Ready</div>

  <!-- Brawl-style progress bar -->
  <div style="width:420px;height:28px;background:#0a0a0a;border:3px solid #ffffff;border-radius:6px;overflow:hidden;box-shadow:0 0 10px #0008;margin-top:8px;">
    <div id="bar" style="height:100%;width:0%;background:linear-gradient(90deg,#ffb300,#ff9c00);transition:width 0.25s ease-out;box-shadow:inset 0 0 4px #0007"></div>
  </div>

  <hr style="margin:12px 0">

  <div style="margin-top:10px">{tree}</div>
</div>

<script>
(function(){
  // theme
  const toggle = document.getElementById("themeToggle");
  const saved = localStorage.getItem("bsgrip_theme") || "light";
  function applyTheme(t){
    if(t==="dark"){ document.documentElement.classList.add("dark"); toggle.innerText="☀️"; }
    else { document.documentElement.classList.remove("dark"); toggle.innerText="🌙"; }
  }
  applyTheme(saved);
  toggle.addEventListener("click", ()=>{
    const next = document.documentElement.classList.contains("dark") ? "light" : "dark";
    localStorage.setItem("bsgrip_theme", next);
    applyTheme(next);
  });

  // download button
  const dl = document.getElementById("downloadBtn");
  dl.addEventListener("click", ()=>{
    document.getElementById("status").innerText = "Starting download...";
    // trigger download (this will start zip generation on server and stream)
    window.location = "/download_all/{master_sha}";
    poll();
  });

  // poll progress
  function poll(){
    fetch("/progress/{master_sha}")
      .then(r => {
         if (!r.ok) return {total:0, done:0};
         return r.json();
      })
      .then(d => {
        const total = d.total || 1;
        const done = d.done || 0;
        const pct = Math.floor((done / total) * 100);
        document.getElementById("bar").style.width = pct + "%";
        document.getElementById("status").innerText = done + " / " + total + " files";
        if (done < total) setTimeout(poll, 1000);
        else {
          document.getElementById("status").innerText = "Completed!";
          document.getElementById("bar").style.background = "linear-gradient(90deg,#00e000,#00aa00)";
        }
      })
      .catch(()=> setTimeout(poll,1000));
  }

  // start polling immediately to show total
  poll();
})();
</script>
</body>
</html>"""
    html = template.replace("{master_sha}", master_sha).replace("{tree}", tree_html)
    return html


# --------------------------------------------------
# Progress API
# --------------------------------------------------

@app.route("/progress/<master_sha>")
def progress_api(master_sha):
    with progress_lock:
        return jsonify(progress.get(master_sha, {"total": 0, "done": 0}))


# --------------------------------------------------
# CDN Access
# --------------------------------------------------

@app.route("/cdn/<master_sha>/<path:file_path>")
def cdn(master_sha, file_path):
    full = resolve_asset(master_sha, file_path)
    if not full:
        abort(404)
    return send_file(full)


# --------------------------------------------------
# Parallel Download (ZIP)
# --------------------------------------------------

@app.route("/download_all/<master_sha>")
def download_all(master_sha):
    versions = load_all_fingerprints()
    if master_sha not in versions:
        return "SHA not found.", 404

    assets = versions[master_sha]["assets"]

    with progress_lock:
        progress[master_sha] = {"total": len(assets), "done": 0}

    def task(asset):
        file_path = asset["file"]
        resolved = resolve_asset(master_sha, file_path)

        with progress_lock:
            progress[master_sha]["done"] += 1

        return (resolved, file_path)

    downloaded = []
    with ThreadPoolExecutor(max_workers=MAX_THREADS) as exe:
        futures = [exe.submit(task, a) for a in assets]
        for f in as_completed(futures):
            try:
                resolved, file_path = f.result()
            except Exception:
                continue
            if resolved:
                downloaded.append((resolved, file_path))

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
    zip_path = tmp.name
    tmp.close()

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for resolved, file_path in downloaded:
            z.write(resolved, arcname=file_path)

    return send_file(
        zip_path,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"{master_sha}.zip"
    )


# --------------------------------------------------
# Startup
# --------------------------------------------------

if __name__ == "__main__":
    ensure_dir(FINGERPRINT_DIR)
    ensure_dir(CACHE_DIR)

    PORT = 8080

    print("============================================")
    print("  Frontview Started")
    print(f"  Explorer: http://localhost:{PORT}/explorer")
    print("============================================")

    app.run(host="0.0.0.0", port=PORT, debug=False)
