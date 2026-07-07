#!/usr/bin/env python3
"""
Kyma-Dub Web UI — batch video dubbing with clean white/blue interface.
"""
import os, json, subprocess, threading, uuid, time, re
from pathlib import Path
from flask import Flask, request, jsonify, send_file, Response

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 2 * 1024 * 1024 * 1024  # 2GB max upload

def _load_kyma_env():
    candidates = [Path(".env"), Path.home() / ".config/kyma-dub/env", Path.home() / "kyma-api/.env"]
    for f in candidates:
        if f.exists():
            for line in f.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#"): continue
                m = re.match(r'^([A-Z_][A-Z0-9_]*)=(.*)$', line)
                if m:
                    key, val = m.group(1), m.group(2).strip('"\'')
                    if key not in os.environ: os.environ[key] = val
_load_kyma_env()

UPLOAD_DIR = Path(os.path.expanduser("~/.kyma-dub/web_uploads"))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()

PIPELINE = Path(__file__).parent / "lib" / "pipeline.py"
_VOICE_IDS = {
    "charlie": "IKne3meq5aSn9XLyUdCD", "will": "bIHbv24MWmeRgasZH58o",
    "liam": "TX3LPaxmHKxFdv7VOQHJ", "brian": "nPczCjzI2devNBz1zQrb",
    "rachel": "21m00Tcm4TlvDq8ikWAM", "adam": "pNInz6obpgDQGcFmaJgB",
    "jessica": "cgSgspJ2msm6clMCkdW9",
}


def run_job(job_id: str, input_path: str, to_lang: str, voice: str, model: str, dual_sub: bool = False):
    with JOBS_LOCK:
        JOBS[job_id] = {"status": "running", "log": [], "output": None, "progress": 0, "filename": Path(input_path).name}

    out_path = UPLOAD_DIR / f"{job_id}_dubbed.mp4"
    groq_key = os.environ.get("GROQ_API_KEY", "")
    kyma_key = os.environ.get("KYMA_API_KEY", "")
    mode = "kyma" if kyma_key else ("direct" if groq_key else "none")
    voice_id = _VOICE_IDS.get(voice, voice)
    translate_model = model if model else "llama-3.3-70b-versatile"

    cfg = {
        "video": str(Path(input_path).resolve()),
        "out": str(out_path),
        "mode": mode,
        "kyma_base": os.environ.get("KYMA_DUB_BASE", "https://api.kymaapi.com"),
        "kyma_key": kyma_key, "groq_key": groq_key,
        "eleven_key": os.environ.get("ELEVENLABS_API_KEY", ""),
        "ua": "kyma-dub/0.2.0",
        "source_lang": "auto", "target_lang": to_lang,
        "voice": voice, "voice_id": voice_id,
        "minimax_voice": "English_expressive_narrator",
        "tts": "kyma" if kyma_key else "elevenlabs",
        "translate_model": translate_model,
        "max_speed": 1.5, "chunk_sec": 22.0,
        "allow_voice_fallback": True,
        "burn": True,          # always burn captions
        "srt": True,           # also save .srt
        "dual_sub": dual_sub,  # EN + original sub
        "bilingual": False,
        "keep_temp": False,
        "orig_vol": 0.08,      # keep original voice at 8%
    }
    cfg_path = UPLOAD_DIR / f"{job_id}_cfg.json"
    cfg_path.write_text(json.dumps(cfg))
    cmd = ["python3", str(PIPELINE), str(cfg_path)]

    def update(line):
        with JOBS_LOCK:
            JOBS[job_id]["log"].append(line)
            l = line.lower()
            if "extract" in l:         JOBS[job_id]["progress"] = 5
            elif "transcrib" in l:     JOBS[job_id]["progress"] = 15
            elif "grouped" in l:       JOBS[job_id]["progress"] = 25
            elif "translat" in l:      JOBS[job_id]["progress"] = 40
            elif "tts" in l or "locked" in l: JOBS[job_id]["progress"] = 50
            elif "chunk" in l and "slot" in l: JOBS[job_id]["progress"] = 65
            elif "reassembl" in l or "assembl" in l: JOBS[job_id]["progress"] = 80
            elif "burn" in l or "caption" in l: JOBS[job_id]["progress"] = 90
            elif "done" in l:          JOBS[job_id]["progress"] = 100

    try:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(PIPELINE.parent)
        proc = subprocess.Popen(cmd, stderr=subprocess.STDOUT, stdout=subprocess.PIPE, text=True, bufsize=1, env=env)
        for line in proc.stdout:
            line = line.rstrip()
            print(f"[job:{job_id}] {line}", flush=True)  # show in Railway logs
            update(line)
        proc.wait()
        if proc.returncode == 0 and out_path.exists():
            with JOBS_LOCK:
                JOBS[job_id]["status"] = "done"
                JOBS[job_id]["output"] = str(out_path)
                JOBS[job_id]["progress"] = 100
        else:
            with JOBS_LOCK:
                JOBS[job_id]["status"] = "error"
    except Exception as e:
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "error"
            JOBS[job_id]["log"].append(str(e))
    finally:
        try: cfg_path.unlink()
        except: pass


HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kyma Dub Studio</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  :root {
    --blue: #2563eb; --blue-light: #3b82f6; --blue-50: #eff6ff;
    --blue-100: #dbeafe; --blue-700: #1d4ed8;
    --gray-50: #f9fafb; --gray-100: #f3f4f6; --gray-200: #e5e7eb;
    --gray-300: #d1d5db; --gray-400: #9ca3af; --gray-500: #6b7280;
    --gray-600: #4b5563; --gray-700: #374151; --gray-800: #1f2937;
    --gray-900: #111827;
    --green: #059669; --green-50: #ecfdf5; --green-100: #d1fae5;
    --red: #dc2626; --red-50: #fef2f2;
    --radius: 10px; --shadow: 0 1px 3px rgba(0,0,0,.08), 0 1px 2px rgba(0,0,0,.04);
    --shadow-md: 0 4px 6px -1px rgba(0,0,0,.07), 0 2px 4px -2px rgba(0,0,0,.05);
  }
  body { background: var(--gray-50); color: var(--gray-800); font-family: -apple-system,BlinkMacSystemFont,'Inter','Segoe UI',sans-serif; min-height: 100vh; font-size: 14px; line-height: 1.5; }

  /* header */
  header { background: #fff; border-bottom: 1px solid var(--gray-200); padding: 16px 24px; display: flex; align-items: center; gap: 10px; }
  header .logo { width: 28px; height: 28px; background: var(--blue); border-radius: 7px; display: flex; align-items: center; justify-content: center; }
  header .logo svg { width: 16px; height: 16px; }
  header h1 { font-size: 16px; font-weight: 600; color: var(--gray-900); }
  header h1 span { color: var(--blue); }

  .container { max-width: 960px; margin: 0 auto; padding: 24px 20px; }

  /* card */
  .card { background: #fff; border: 1px solid var(--gray-200); border-radius: var(--radius); box-shadow: var(--shadow); margin-bottom: 16px; }
  .card-header { padding: 14px 18px; border-bottom: 1px solid var(--gray-100); }
  .card-header h2 { font-size: 13px; font-weight: 600; color: var(--gray-600); text-transform: uppercase; letter-spacing: .04em; }
  .card-body { padding: 18px; }

  /* dropzone */
  .dropzone {
    border: 2px dashed var(--gray-300); border-radius: var(--radius);
    padding: 40px 20px; text-align: center; cursor: pointer;
    transition: all .2s; position: relative; background: var(--gray-50);
  }
  .dropzone:hover, .dropzone.over { border-color: var(--blue); background: var(--blue-50); }
  .dropzone input { position: absolute; inset: 0; opacity: 0; cursor: pointer; width: 100%; height: 100%; }
  .dropzone .icon { font-size: 32px; margin-bottom: 8px; color: var(--blue-light); }
  .dropzone p { color: var(--gray-500); font-size: 13px; }
  .dropzone p strong { color: var(--gray-700); }

  /* file list */
  #file-list { margin-top: 12px; }
  .file-item { display: flex; align-items: center; gap: 8px; padding: 8px 12px; background: var(--blue-50); border: 1px solid var(--blue-100); border-radius: 6px; margin-bottom: 6px; font-size: 13px; color: var(--gray-700); }
  .file-item .name { flex: 1; font-weight: 500; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .file-item .size { color: var(--gray-400); font-size: 12px; }
  .file-item .remove { background: none; border: none; color: var(--gray-400); cursor: pointer; font-size: 16px; padding: 0 4px; line-height: 1; }
  .file-item .remove:hover { color: var(--red); }

  /* options row */
  .options { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 14px; }
  @media(max-width:640px){ .options { grid-template-columns: 1fr; } }
  label.field-label { font-size: 12px; color: var(--gray-500); font-weight: 500; display: block; margin-bottom: 4px; }
  select {
    width: 100%; background: #fff; border: 1px solid var(--gray-300);
    border-radius: 7px; padding: 8px 12px; color: var(--gray-700); font-size: 13px;
    outline: none; appearance: none;
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'%3E%3Cpath d='M3 5l3 3 3-3' stroke='%239ca3af' fill='none' stroke-width='1.5'/%3E%3C/svg%3E");
    background-repeat: no-repeat; background-position: right 10px center;
  }
  select:focus { border-color: var(--blue); box-shadow: 0 0 0 3px rgba(37,99,235,.1); }

  /* button */
  .btn-primary {
    margin-top: 16px; width: 100%; padding: 10px; border: none; border-radius: 8px;
    background: var(--blue); color: #fff; font-size: 14px; font-weight: 600;
    cursor: pointer; transition: background .15s;
  }
  .btn-primary:hover { background: var(--blue-700); }
  .btn-primary:disabled { background: var(--gray-300); cursor: not-allowed; }

  /* jobs section */
  #jobs-section { display: none; }
  .job-card { margin-bottom: 12px; }
  .job-header { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; }
  .job-filename { font-weight: 600; font-size: 13px; color: var(--gray-800); flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

  /* badge */
  .badge { display: inline-flex; align-items: center; gap: 4px; padding: 2px 10px; border-radius: 99px; font-size: 11px; font-weight: 600; }
  .badge.running { background: var(--blue-100); color: var(--blue-700); }
  .badge.done { background: var(--green-100); color: var(--green); }
  .badge.error { background: var(--red-50); color: var(--red); }
  .badge .dot { width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
  .badge .dot.pulse { animation: pulse 1.2s infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.3} }

  /* progress */
  .progress-row { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
  .progress-wrap { flex: 1; background: var(--gray-100); border-radius: 99px; height: 6px; overflow: hidden; }
  .progress-bar { height: 100%; background: var(--blue); border-radius: 99px; transition: width .4s ease; width: 0%; }
  .progress-bar.done { background: var(--green); }
  .pct { font-size: 12px; color: var(--gray-400); min-width: 32px; text-align: right; }

  /* log toggle */
  .log-toggle { background: none; border: none; color: var(--gray-400); font-size: 12px; cursor: pointer; padding: 4px 0; }
  .log-toggle:hover { color: var(--gray-600); }
  .log { background: var(--gray-900); border-radius: 6px; padding: 12px; max-height: 160px; overflow-y: auto; font-family: 'SF Mono',Menlo,monospace; font-size: 11px; color: #9ca3af; line-height: 1.5; margin-top: 6px; display: none; }
  .log.open { display: block; }
  .log-line.error { color: #f87171; }

  /* video result */
  .result-row { display: flex; align-items: center; gap: 12px; margin-top: 10px; }
  .result-video { width: 100%; border-radius: 8px; background: #000; max-height: 360px; }
  .dl-btn {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 7px 14px; background: var(--green); border: none; border-radius: 6px;
    color: #fff; text-decoration: none; font-size: 12px; font-weight: 500; cursor: pointer;
    transition: background .15s;
  }
  .dl-btn:hover { background: #047857; }
  .new-btn {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 7px 14px; background: var(--blue); border: none; border-radius: 6px;
    color: #fff; font-size: 12px; font-weight: 500; cursor: pointer; margin-top: 12px;
  }
  .new-btn:hover { background: var(--blue-700); }

  /* info bar */
  .info-bar { background: var(--blue-50); border: 1px solid var(--blue-100); border-radius: 7px; padding: 10px 14px; margin-bottom: 16px; font-size: 12px; color: var(--blue-700); display: flex; align-items: center; gap: 8px; }
  .info-bar .icon { font-size: 16px; }
</style>
</head>
<body>
<header>
  <div class="logo">
    <svg viewBox="0 0 16 16" fill="none"><path d="M4 6l4-2 4 2v4l-4 2-4-2V6z" stroke="white" stroke-width="1.2" fill="none"/><circle cx="8" cy="8" r="1.5" fill="white"/></svg>
  </div>
  <h1>Kyma <span>Dub Studio</span></h1>
</header>

<div class="container">
  <div class="info-bar">
    <span class="icon">&#9432;</span>
    Output: dubbed voice (100%) + original voice (8%) + English captions (white on black)
  </div>

  <form id="dub-form">
    <div class="card">
      <div class="card-header"><h2>Upload Videos</h2></div>
      <div class="card-body">
        <div class="dropzone" id="drop">
          <input type="file" id="video-files" accept="video/*" multiple>
          <div class="icon">&#8679;</div>
          <p><strong>Drop videos here</strong> or click to browse</p>
          <p style="margin-top:4px; font-size:12px">MP4, MOV, MKV &mdash; multiple files supported</p>
        </div>
        <div id="file-list"></div>
      </div>
    </div>

    <div class="card">
      <div class="card-header"><h2>Settings</h2></div>
      <div class="card-body">
        <div class="options">
          <div>
            <label class="field-label">Target Language</label>
            <select id="to-lang">
              <option value="en">English</option>
              <option value="vi">Vietnamese</option>
              <option value="es">Spanish</option>
              <option value="fr">French</option>
              <option value="de">German</option>
              <option value="ja">Japanese</option>
              <option value="ko">Korean</option>
              <option value="zh">Chinese</option>
              <option value="pt">Portuguese</option>
              <option value="id">Indonesian</option>
            </select>
          </div>
          <div>
            <label class="field-label">Voice</label>
            <select id="voice">
              <option value="charlie">Charlie &mdash; young, natural</option>
              <option value="will">Will &mdash; young, friendly</option>
              <option value="liam">Liam &mdash; narrator</option>
              <option value="brian">Brian &mdash; mature</option>
              <option value="rachel">Rachel &mdash; female, warm</option>
              <option value="adam">Adam &mdash; male, deep</option>
              <option value="jessica">Jessica &mdash; female, young</option>
            </select>
          </div>
          <div>
            <label class="field-label">Translation Model</label>
            <select id="model">
              <option value="">Llama 3.3 70B (default)</option>
              <option value="llama-3.3-70b-versatile">Llama 3.3 70B</option>
            </select>
          </div>
        </div>
      </div>
    </div>

    <div class="card" style="padding: 14px 18px; display:flex; align-items:center; gap:10px">
      <label style="display:flex; align-items:center; gap:8px; cursor:pointer; font-size:13px; color:var(--gray-700)">
        <input type="checkbox" id="dual-sub" style="width:16px;height:16px;accent-color:var(--blue)">
        Show original subtitle (Vietnamese) below English caption
      </label>
    </div>

    <button class="btn-primary" type="submit" id="dub-btn">Start Dubbing</button>
  </form>

  <div id="jobs-section" style="margin-top: 20px">
    <div class="card">
      <div class="card-header"><h2>Processing Queue</h2></div>
      <div class="card-body" id="jobs-container"></div>
    </div>

    <button class="new-btn" id="new-batch-btn" style="display:none">+ New Batch</button>
  </div>
</div>

<script>
let selectedFiles = [];
let activeJobs = {};
let pollTimer = null;

// --- file selection ---
const fileInput = document.getElementById('video-files');
const drop = document.getElementById('drop');

fileInput.addEventListener('change', () => addFiles(fileInput.files));
drop.addEventListener('dragover', e => { e.preventDefault(); drop.classList.add('over'); });
drop.addEventListener('dragleave', () => drop.classList.remove('over'));
drop.addEventListener('drop', e => { e.preventDefault(); drop.classList.remove('over'); addFiles(e.dataTransfer.files); });

function addFiles(fileList) {
  for (const f of fileList) {
    if (!selectedFiles.find(x => x.name === f.name && x.size === f.size)) {
      selectedFiles.push(f);
    }
  }
  renderFileList();
}

function removeFile(idx) {
  selectedFiles.splice(idx, 1);
  renderFileList();
}

function renderFileList() {
  const el = document.getElementById('file-list');
  if (!selectedFiles.length) { el.innerHTML = ''; return; }
  el.innerHTML = selectedFiles.map((f, i) =>
    `<div class="file-item">
      <span class="name">${esc(f.name)}</span>
      <span class="size">${(f.size/1048576).toFixed(1)} MB</span>
      <button type="button" class="remove" onclick="removeFile(${i})">&times;</button>
    </div>`
  ).join('');
}

// --- form submit ---
document.getElementById('dub-form').addEventListener('submit', async function(e) {
  e.preventDefault();
  if (!selectedFiles.length) return;

  const btn = document.getElementById('dub-btn');
  btn.disabled = true;
  document.getElementById('jobs-section').style.display = 'block';

  const toLang = document.getElementById('to-lang').value;
  const voice = document.getElementById('voice').value;
  const model = document.getElementById('model').value;
  const dualSub = document.getElementById('dual-sub').checked ? '1' : '0';

  for (const file of selectedFiles) {
    const fd = new FormData();
    fd.append('video', file);
    fd.append('to', toLang);
    fd.append('voice', voice);
    fd.append('model', model);
    fd.append('dual_sub', dualSub);

    try {
      const r = await fetch('/api/dub', { method: 'POST', body: fd });
      const data = await r.json();
      if (data.job_id) {
        activeJobs[data.job_id] = { filename: file.name, status: 'running', progress: 0, log: [] };
        renderJobs();
      }
    } catch (err) {
      console.error('upload error', err);
    }
  }

  if (!pollTimer) pollTimer = setInterval(pollAll, 2000);
});

// --- polling ---
async function pollAll() {
  const ids = Object.keys(activeJobs).filter(id => activeJobs[id].status === 'running');
  if (!ids.length) {
    clearInterval(pollTimer); pollTimer = null;
    document.getElementById('dub-btn').disabled = false;
    document.getElementById('new-batch-btn').style.display = 'inline-flex';
    return;
  }
  for (const id of ids) {
    try {
      const r = await fetch('/api/status/' + id);
      const d = await r.json();
      activeJobs[id].status = d.status || 'running';
      activeJobs[id].progress = d.progress || 0;
      activeJobs[id].log = d.log || [];
      activeJobs[id].output = d.output || null;
    } catch (e) {}
  }
  renderJobs();
}

function renderJobs() {
  const el = document.getElementById('jobs-container');
  const ids = Object.keys(activeJobs);
  el.innerHTML = ids.map(id => {
    const j = activeJobs[id];
    const isDone = j.status === 'done';
    const isErr = j.status === 'error';
    const badgeCls = isDone ? 'done' : isErr ? 'error' : 'running';
    const badgeText = isDone ? 'Done' : isErr ? 'Error' : 'Processing';
    const pulse = j.status === 'running' ? ' pulse' : '';
    const barCls = isDone ? ' done' : '';
    return `
    <div class="job-card">
      <div class="job-header">
        <span class="job-filename">${esc(j.filename)}</span>
        <span class="badge ${badgeCls}"><span class="dot${pulse}"></span> ${badgeText}</span>
      </div>
      <div class="progress-row">
        <div class="progress-wrap"><div class="progress-bar${barCls}" style="width:${j.progress}%"></div></div>
        <span class="pct">${j.progress}%</span>
      </div>
      <button type="button" class="log-toggle" onclick="toggleLog('${id}')">Show log</button>
      <div class="log" id="log-${id}">${(j.log||[]).map(l => `<div class="log-line${l.toLowerCase().includes('error')?' error':''}">${esc(l)}</div>`).join('')}</div>
      ${isDone ? `
        <video class="result-video" controls src="/api/video/${id}"></video>
        <div style="margin-top:8px"><a class="dl-btn" href="/api/video/${id}" download>&#8681; Download</a></div>
      ` : ''}
    </div>
    ${ids.indexOf(id) < ids.length - 1 ? '<hr style="border:none;border-top:1px solid var(--gray-100);margin:12px 0">' : ''}`;
  }).join('');
}

function toggleLog(id) {
  const el = document.getElementById('log-' + id);
  el.classList.toggle('open');
  if (el.classList.contains('open')) el.scrollTop = el.scrollHeight;
}

document.getElementById('new-batch-btn').addEventListener('click', () => {
  activeJobs = {};
  selectedFiles = [];
  renderFileList();
  renderJobs();
  document.getElementById('jobs-section').style.display = 'none';
  document.getElementById('new-batch-btn').style.display = 'none';
  document.getElementById('dub-btn').disabled = false;
  // reset file input
  fileInput.value = '';
});

function esc(s) { return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
</script>
</body>
</html>
"""

@app.route("/")
def index():
    return HTML

@app.route("/api/dub", methods=["POST"])
def start_dub():
    file = request.files.get("video")
    if not file:
        return jsonify({"error": "no video"}), 400
    to_lang = request.form.get("to", "en")
    voice   = request.form.get("voice", "charlie")
    model   = request.form.get("model", "")
    dual_sub = request.form.get("dual_sub") == "1"
    job_id = uuid.uuid4().hex[:12]
    in_path = UPLOAD_DIR / f"{job_id}_input{Path(file.filename).suffix or '.mp4'}"
    file.save(str(in_path))
    t = threading.Thread(target=run_job, args=(job_id, str(in_path), to_lang, voice, model, dual_sub), daemon=True)
    t.start()
    return jsonify({"job_id": job_id})

@app.route("/api/status/<job_id>")
def job_status(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id, {})
    return jsonify(job)

@app.route("/api/video/<job_id>")
def serve_video(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id, {})
    out = job.get("output")
    if not out or not Path(out).exists():
        return "not found", 404
    return send_file(out, mimetype="video/mp4", as_attachment=False)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7860))
    print(f"\n  Kyma Dub Studio  ->  http://localhost:{port}\n")
    app.run(host="0.0.0.0", port=port, debug=False)
