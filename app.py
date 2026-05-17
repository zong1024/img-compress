#!/usr/bin/env python3
"""Flask web UI for the image compression tool."""

from __future__ import annotations

import argparse
import os
import tempfile
import threading
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from flask import Flask, jsonify, render_template_string, request, send_file, session
from werkzeug.utils import secure_filename

from img_compress import (
    DEFAULT_MAX_DIM,
    DEFAULT_TARGET_MB,
    DEFAULT_WORKERS,
    MAX_TARGET_MB,
    MIN_TARGET_MB,
    SUPPORTED_EXTENSIONS,
    compress_image,
)

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-only-change-me")
UPLOADS_DIR = Path(os.environ.get("UPLOADS_DIR", "uploads"))
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()


def clamp_int(value: str | None, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value or default)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def clamp_float(value: str | None, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value or default)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def unique_filename(directory: Path, filename: str) -> str:
    safe = secure_filename(filename) or "image"
    candidate = safe
    stem, suffix = Path(safe).stem, Path(safe).suffix
    counter = 2
    while (directory / candidate).exists():
        candidate = f"{stem}-{counter}{suffix}"
        counter += 1
    return candidate


def current_job() -> dict | None:
    job_id = session.get("job_id")
    if not job_id:
        return None
    with JOBS_LOCK:
        return JOBS.get(job_id)


@app.get("/")
def index():
    return render_template_string(TEMPLATE)


@app.post("/compress")
def compress():
    files = request.files.getlist("files")
    valid_files = [f for f in files if Path(f.filename or "").suffix.lower() in SUPPORTED_EXTENSIONS]
    if not valid_files:
        return jsonify({"error": "Upload at least one supported image."}), 400

    target_mb = clamp_float(request.form.get("target_size"), DEFAULT_TARGET_MB, MIN_TARGET_MB, MAX_TARGET_MB)
    max_dim = clamp_int(request.form.get("max_dim"), DEFAULT_MAX_DIM, 1024, 8192)
    workers = clamp_int(request.form.get("workers"), DEFAULT_WORKERS, 1, 16)

    requested_job_id = request.form.get("job_id", "")
    job_id = requested_job_id if requested_job_id.isalnum() else uuid.uuid4().hex
    upload_dir = Path(tempfile.mkdtemp(prefix="img-compress-", dir=UPLOADS_DIR))
    compressed_dir = upload_dir / "compressed"
    compressed_dir.mkdir(exist_ok=True)

    sources: list[Path] = []
    for uploaded in valid_files:
        filename = unique_filename(upload_dir, uploaded.filename or "image")
        destination = upload_dir / filename
        uploaded.save(destination)
        sources.append(destination)

    job = {
        "id": job_id,
        "upload_dir": upload_dir,
        "compressed_dir": compressed_dir,
        "total": len(sources),
        "completed": 0,
        "running": True,
        "results": [],
    }
    with JOBS_LOCK:
        JOBS[job_id] = job
    session["job_id"] = job_id

    target_bytes = int(target_mb * 1024 * 1024)
    results = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(compress_image, source, compressed_dir, target_bytes, max_dim): source
            for source in sources
        }
        for future in as_completed(futures):
            source = futures[future]
            try:
                result = future.result()
                payload = {
                    "filename": source.name,
                    "download_name": result.output.name if result.output else None,
                    "original_size": result.original_size,
                    "compressed_size": result.compressed_size,
                    "ratio": round(result.ratio, 2) if result.ratio else None,
                    "warning": result.warning,
                }
            except Exception as exc:  # keep the batch moving
                payload = {
                    "filename": source.name,
                    "download_name": None,
                    "original_size": source.stat().st_size,
                    "compressed_size": None,
                    "ratio": None,
                    "warning": f"unexpected error: {exc}",
                }
            results.append(payload)
            with JOBS_LOCK:
                job["completed"] += 1
                job["results"].append(payload)

    with JOBS_LOCK:
        job["running"] = False

    return jsonify({
        "job_id": job_id,
        "settings": {"target_size": target_mb, "max_dim": max_dim, "workers": workers},
        "results": results,
    })


@app.get("/status")
def status():
    requested_job_id = request.args.get("job_id")
    if requested_job_id:
        with JOBS_LOCK:
            job = JOBS.get(requested_job_id)
    else:
        job = current_job()
    if not job:
        return jsonify({"total": 0, "completed": 0, "running": False})
    return jsonify({
        "total": job["total"],
        "completed": job["completed"],
        "running": job["running"],
    })


@app.get("/download/<path:filename>")
def download(filename: str):
    job = current_job()
    if not job:
        return jsonify({"error": "No active compression job."}), 404
    safe_name = Path(filename).name
    path = job["compressed_dir"] / safe_name
    if not path.exists() or not path.is_file():
        return jsonify({"error": "File not found."}), 404
    return send_file(path, as_attachment=True, download_name=safe_name)


@app.get("/download-all")
def download_all():
    job = current_job()
    if not job:
        return jsonify({"error": "No active compression job."}), 404
    compressed_files = sorted(path for path in job["compressed_dir"].iterdir() if path.is_file())
    if not compressed_files:
        return jsonify({"error": "No compressed files available."}), 404

    zip_path = job["upload_dir"] / "compressed-images.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in compressed_files:
            archive.write(path, arcname=path.name)
    return send_file(zip_path, as_attachment=True, download_name="compressed-images.zip")


TEMPLATE = r'''
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Image Compressor</title>
  <style>
    :root {
      --bg: #070b16;
      --panel: rgba(14, 20, 39, .78);
      --panel-strong: rgba(18, 26, 48, .94);
      --text: #eef4ff;
      --muted: #9aabd0;
      --line: rgba(255,255,255,.09);
      --blue: #4f8cff;
      --cyan: #65e7ff;
      --success: #5ee3a1;
      --danger: #ff8d9b;
      color-scheme: dark;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: var(--text);
      background:
        radial-gradient(circle at 10% 0%, rgba(79,140,255,.24), transparent 32rem),
        radial-gradient(circle at 90% 10%, rgba(101,231,255,.16), transparent 28rem),
        linear-gradient(180deg, #050814, var(--bg));
    }
    .shell { width: min(1180px, calc(100% - 32px)); margin: 0 auto; padding: 42px 0 56px; }
    .hero { margin-bottom: 24px; }
    .eyebrow { color: var(--cyan); letter-spacing: .14em; text-transform: uppercase; font-size: 12px; }
    h1 { font-size: clamp(30px, 5vw, 54px); line-height: 1; margin: 10px 0 12px; }
    .hero p { color: var(--muted); max-width: 650px; margin: 0; }
    .card {
      border: 1px solid var(--line);
      background: var(--panel);
      backdrop-filter: blur(18px);
      box-shadow: 0 24px 80px rgba(0,0,0,.35);
      border-radius: 24px;
    }
    .workspace { display: grid; grid-template-columns: minmax(0, 1fr) 330px; gap: 18px; }
    .dropzone {
      min-height: 260px; padding: 28px; display: grid; place-items: center; text-align: center;
      border: 1px dashed rgba(101,231,255,.35); border-radius: 20px; margin: 18px;
      background: linear-gradient(135deg, rgba(79,140,255,.12), rgba(101,231,255,.05));
      transition: .22s ease;
    }
    .dropzone.dragging { transform: translateY(-2px); border-color: var(--cyan); box-shadow: 0 0 0 4px rgba(101,231,255,.08); }
    .dropzone input { display:none; }
    .upload-icon { font-size: 40px; margin-bottom: 8px; }
    .dropzone strong { display:block; font-size: 20px; }
    .dropzone span { color: var(--muted); }
    .thumbs { display:grid; grid-template-columns: repeat(auto-fill, minmax(150px,1fr)); gap:14px; padding:0 18px 18px; }
    .thumb { overflow:hidden; background: var(--panel-strong); border:1px solid var(--line); border-radius:16px; }
    .thumb img { width:100%; height:120px; object-fit:cover; display:block; }
    .thumb div { padding:10px; }
    .thumb b { display:block; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-size:13px; }
    .thumb small { color:var(--muted); }
    .controls { padding:20px; display:flex; flex-direction:column; gap:18px; }
    .control label { display:flex; justify-content:space-between; color:var(--muted); margin-bottom:10px; }
    input[type=range] { width:100%; accent-color:var(--blue); }
    button {
      border:0; border-radius:16px; font-weight:700; cursor:pointer; transition:.18s ease;
    }
    #compressBtn {
      min-height:58px; color:white; font-size:18px;
      background: linear-gradient(135deg, #377dff, #59b7ff);
      box-shadow: 0 18px 36px rgba(55,125,255,.28);
    }
    #compressBtn:hover { transform: translateY(-1px); filter: brightness(1.05); }
    #compressBtn:disabled { opacity:.55; cursor:not-allowed; transform:none; }
    .progress-wrap { grid-column:1 / -1; padding:18px; }
    .progress-head { display:flex; justify-content:space-between; margin-bottom:10px; color:var(--muted); }
    .bar { height:12px; overflow:hidden; background:rgba(255,255,255,.08); border-radius:999px; }
    .bar i { display:block; width:0; height:100%; background:linear-gradient(90deg, var(--blue), var(--cyan)); transition:width .25s ease; }
    .results { margin-top:18px; padding:18px; display:none; }
    .results.show { display:block; animation: rise .35s ease both; }
    .results-head { display:flex; gap:12px; justify-content:space-between; align-items:center; margin-bottom:14px; }
    .zip { padding:12px 16px; color:#06101f; background:var(--success); text-decoration:none; border-radius:14px; font-weight:700; }
    .row { display:grid; grid-template-columns:minmax(180px,1fr) auto auto; gap:14px; align-items:center; padding:14px; border-top:1px solid var(--line); }
    .row:first-of-type { border-top:0; }
    .row p { margin:4px 0 0; color:var(--muted); }
    .download { color:white; text-decoration:none; background:rgba(79,140,255,.18); border:1px solid rgba(79,140,255,.35); padding:10px 12px; border-radius:12px; }
    .warning { color:var(--danger); }
    @keyframes rise { from { opacity:0; transform:translateY(8px);} to {opacity:1; transform:none;} }
    @media (max-width: 820px) { .workspace { grid-template-columns:1fr; } .row { grid-template-columns:1fr; } }
  </style>
</head>
<body>
  <main class="shell">
    <section class="hero">
      <div class="eyebrow">Image Compressor</div>
      <h1>Shrink images without shrinking the experience.</h1>
      <p>Drop in a batch, tune the limits, and let the compressor carve them down into clean JPEGs ready to share.</p>
    </section>

    <section class="workspace">
      <div class="card">
        <label class="dropzone" id="dropzone">
          <input id="files" type="file" multiple accept=".jpg,.jpeg,.png,.webp,.bmp,.tiff,.tif,image/*">
          <div>
            <div class="upload-icon">⬆</div>
            <strong>Drop images here</strong>
            <span>or click to upload JPG, PNG, WEBP, BMP, TIFF</span>
          </div>
        </label>
        <div class="thumbs" id="thumbs"></div>
      </div>

      <aside class="card controls">
        <div class="control"><label>Target size <b id="targetValue">2 MB</b></label><input id="target" type="range" min="1" max="5" step="0.1" value="2"></div>
        <div class="control"><label>Max dimension <b id="dimValue">4096 px</b></label><input id="maxDim" type="range" min="1024" max="8192" step="256" value="4096"></div>
        <div class="control"><label>Workers <b id="workersValue">4</b></label><input id="workers" type="range" min="1" max="16" step="1" value="4"></div>
        <button id="compressBtn" disabled>Compress</button>
      </aside>

      <div class="card progress-wrap">
        <div class="progress-head"><span>Compression progress</span><strong id="progressText">[0/0]</strong></div>
        <div class="bar"><i id="progressBar"></i></div>
      </div>
    </section>

    <section class="card results" id="results">
      <div class="results-head"><h2>Results</h2><a class="zip" href="/download-all">Download All as ZIP</a></div>
      <div id="resultRows"></div>
    </section>
  </main>
<script>
const input = document.querySelector('#files');
const dropzone = document.querySelector('#dropzone');
const thumbs = document.querySelector('#thumbs');
const btn = document.querySelector('#compressBtn');
const results = document.querySelector('#results');
const resultRows = document.querySelector('#resultRows');
const bar = document.querySelector('#progressBar');
const progressText = document.querySelector('#progressText');
let selectedFiles = [];
let poller;

const bindRange = (id, out, suffix='') => {
  const el = document.querySelector(id), label = document.querySelector(out);
  const render = () => label.textContent = `${el.value}${suffix}`;
  el.addEventListener('input', render); render();
};
bindRange('#target','#targetValue',' MB');
bindRange('#maxDim','#dimValue',' px');
bindRange('#workers','#workersValue');

const formatBytes = bytes => bytes == null ? '—' : `${(bytes / 1024 / 1024).toFixed(2)} MB`;
function setFiles(files) {
  selectedFiles = [...files].filter(file => /\.(jpe?g|png|webp|bmp|tiff?)$/i.test(file.name));
  thumbs.innerHTML = '';
  selectedFiles.forEach(file => {
    const card = document.createElement('article'); card.className = 'thumb';
    card.innerHTML = `<img alt=""><div><b>${file.name}</b><small>${formatBytes(file.size)}</small></div>`;
    card.querySelector('img').src = URL.createObjectURL(file);
    thumbs.append(card);
  });
  btn.disabled = selectedFiles.length === 0;
}
input.addEventListener('change', e => setFiles(e.target.files));
['dragenter','dragover'].forEach(evt => dropzone.addEventListener(evt, e => { e.preventDefault(); dropzone.classList.add('dragging'); }));
['dragleave','drop'].forEach(evt => dropzone.addEventListener(evt, e => { e.preventDefault(); dropzone.classList.remove('dragging'); }));
dropzone.addEventListener('drop', e => setFiles(e.dataTransfer.files));

async function pollStatus(jobId = '') {
  const suffix = jobId ? `?job_id=${encodeURIComponent(jobId)}` : '';
  const response = await fetch(`/status${suffix}`);
  const status = await response.json();
  const pct = status.total ? (status.completed / status.total) * 100 : 0;
  bar.style.width = `${pct}%`;
  progressText.textContent = `[${status.completed}/${status.total}]`;
  if (!status.running && status.total) clearInterval(poller);
}

btn.addEventListener('click', async () => {
  btn.disabled = true; btn.textContent = 'Compressing…'; results.classList.remove('show'); resultRows.innerHTML = '';
  const form = new FormData();
  const jobId = crypto.randomUUID().replaceAll('-', '');
  form.append('job_id', jobId);
  selectedFiles.forEach(file => form.append('files', file));
  form.append('target_size', document.querySelector('#target').value);
  form.append('max_dim', document.querySelector('#maxDim').value);
  form.append('workers', document.querySelector('#workers').value);
  poller = setInterval(() => pollStatus(jobId), 250); await pollStatus(jobId);
  const response = await fetch('/compress', { method:'POST', body:form });
  const payload = await response.json(); clearInterval(poller); await pollStatus(jobId);
  btn.disabled = false; btn.textContent = 'Compress';
  if (!response.ok) { alert(payload.error || 'Compression failed.'); return; }
  payload.results.forEach(item => {
    const row = document.createElement('div'); row.className = 'row';
    if (item.warning) {
      row.innerHTML = `<div><strong>${item.filename}</strong><p class="warning">${item.warning}</p></div>`;
    } else {
      row.innerHTML = `<div><strong>${item.filename}</strong><p>${formatBytes(item.original_size)} → ${formatBytes(item.compressed_size)}</p></div><b>${item.ratio}× smaller</b><a class="download" href="/download/${encodeURIComponent(item.download_name)}">Download</a>`;
    }
    resultRows.append(row);
  });
  results.classList.add('show');
});
</script>
</body>
</html>
'''


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Image Compressor web UI.")
    parser.add_argument(
        "--host",
        default=os.environ.get("HOST", "0.0.0.0"),
        help="Host interface to bind to (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PORT", "8080")),
        help="Port to listen on (default: 8080)",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    app.run(host=args.host, port=args.port, debug=os.environ.get("FLASK_DEBUG") == "1")
