#!/usr/bin/env python3
"""
Kyma Dub Studio v2 — Dubbing (optional) + Voice Clone + HyperFramers + Captions.
Runs on port 7861 (v1 stays on 7860).
"""
import os, json, subprocess, threading, uuid, re, sys, tempfile, shutil
from pathlib import Path

import requests as _req
from flask import Flask, request, jsonify, send_file

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 2 * 1024 * 1024 * 1024

# ── env ──────────────────────────────────────────────────────────────
def _load_env():
    for f in [Path(".env"), Path.home()/".config/kyma-dub/env",
              Path.home()/"kyma-api/.env"]:
        if f.exists():
            for line in f.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#"): continue
                m = re.match(r'^([A-Z_][A-Z0-9_]*)=(.*)$', line)
                if m:
                    k, v = m.group(1), m.group(2).strip('"\'')
                    if k not in os.environ: os.environ[k] = v

_load_env()
os.environ.setdefault("KYMA_DUB_MODE", "direct")

UPLOAD_DIR = Path(os.path.expanduser("~/.kyma-dub/web_uploads_v2"))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
PIPELINE = Path(__file__).parent / "lib" / "pipeline.py"
LIB_DIR  = Path(__file__).parent / "lib"

JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()

_VOICE_IDS = {
    "charlie": "IKne3meq5aSn9XLyUdCD", "will": "bIHbv24MWmeRgasZH58o",
    "liam":    "TX3LPaxmHKxFdv7VOQHJ", "brian": "nPczCjzI2devNBz1zQrb",
    "rachel":  "21m00Tcm4TlvDq8ikWAM", "adam":  "pNInz6obpgDQGcFmaJgB",
    "jessica": "cgSgspJ2msm6clMCkdW9",
}

# ── voice clone helpers ───────────────────────────────────────────────
def _extract_sample(video: str, out: str, dur: int = 40) -> str:
    subprocess.run(["ffmpeg","-hide_banner","-loglevel","error","-y",
                    "-i", video, "-ss","5","-t",str(dur),
                    "-vn","-ac","1","-ar","16000","-b:a","64k", out], check=True)
    return out

def _clone_voice(eleven_key: str, sample: str) -> str | None:
    try:
        with open(sample,"rb") as fh:
            r = _req.post("https://api.elevenlabs.io/v1/voices/add",
                          headers={"xi-api-key": eleven_key},
                          data={"name": f"kyma_clone_{uuid.uuid4().hex[:6]}"},
                          files={"files":("sample.mp3", fh, "audio/mpeg")}, timeout=60)
        r.raise_for_status()
        return r.json().get("voice_id")
    except Exception as ex:
        return None

def _delete_voice(eleven_key: str, vid: str):
    try: _req.delete(f"https://api.elevenlabs.io/v1/voices/{vid}",
                     headers={"xi-api-key": eleven_key}, timeout=10)
    except: pass


# ── no-dub transcription path (HyperFramers / captions on original) ──
def _transcribe_only(video: str, workdir: str, cfg: dict) -> tuple[list, list, str]:
    """Extract audio + transcribe without translation.
    Returns (chunks, raw_segs, lang).
    chunks  = merged segments (for HyperFramers LLM context)
    raw_segs = short whisper segments (for captions, 1-3s each)
    """
    sys.path.insert(0, str(LIB_DIR))
    from pipeline import extract_audio, transcribe, chunk_segments  # type: ignore
    audio = extract_audio(video, workdir)
    segs, lang = transcribe(cfg, audio)
    chunks = chunk_segments(segs)
    for c in chunks:
        c["en"] = c.get("vi","")
    return chunks, segs, lang

def _write_srt(segs: list, path: str, max_words: int = 9):
    """Write SRT file. Splits entries > max_words into shorter sub-segments."""
    def ts(t):
        h=int(t//3600); m=int(t%3600//60); s=int(t%60); ms=int(round((t-int(t))*1000))
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
    entries = []
    for s in segs:
        text = (s.get("text") or s.get("vi") or "").strip()
        if not text: continue
        t0, t1 = float(s["start"]), float(s["end"])
        words = text.split()
        if len(words) <= max_words:
            entries.append((t0, t1, text))
        else:
            n = (len(words) + max_words - 1) // max_words
            t_seg = (t1 - t0) / n
            w_per = len(words) / n
            for i in range(n):
                wi = int(i * w_per); we = int((i+1)*w_per) if i<n-1 else len(words)
                entries.append((t0 + i*t_seg, t0 + (i+1)*t_seg, " ".join(words[wi:we])))
    with open(path, "w") as fh:
        for n, (t0, t1, text) in enumerate(entries, 1):
            fh.write(f"{n}\n{ts(t0)} --> {ts(t1)}\n{text}\n\n")


def _burn_caps_only(src_video: str, srt_path: str, out_path: str, job_id: str):
    """Burn SRT subtitles onto video using ffmpeg subtitles filter."""
    try:
        # Compute font size based on video dimensions
        try:
            out = subprocess.check_output(
                ["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", src_video],
                stderr=subprocess.DEVNULL).decode().strip()
            vw, vh = [int(x) for x in out.split("x")]
        except Exception:
            vw, vh = 1920, 1080
        short   = min(vw, vh)
        fs_size = max(11, int(short * 0.014))
        mv      = max(16, int(vh * 0.025))
        style   = (f"FontName=Arial,FontSize={fs_size},PrimaryColour=&H00FFFFFF,"
                   f"BackColour=&H1A000000,BorderStyle=3,Outline=0,Shadow=0,"
                   f"MarginV={mv},Alignment=2,Bold=0")
        safe_srt = srt_path.replace("'", "\\'")
        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", src_video,
            "-vf", f"subtitles='{safe_srt}':force_style='{style}'",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "copy", out_path
        ], check=True)
        _log(job_id, "Captions burned ✓")
    except Exception as ex:
        _log(job_id, f"⚠ Caption burn failed: {ex}")


# ── job runner ────────────────────────────────────────────────────────
def _set(job_id, **kw):
    with JOBS_LOCK:
        JOBS[job_id].update(kw)

def _log(job_id, msg):
    with JOBS_LOCK:
        JOBS[job_id]["log"].append(msg)
    print(f"[v2:{job_id}] {msg}", flush=True)

def _pct(job_id, p):
    with JOBS_LOCK:
        JOBS[job_id]["progress"] = p

def _progress_from_line(line: str) -> int:
    l = line.lower()
    if "extract" in l:               return 5
    if "transcrib" in l:             return 15
    if "grouped" in l:               return 25
    if "translat" in l:              return 40
    if "tts" in l or "locked" in l:  return 52
    if "chunk" in l and "slot" in l: return 65
    if "reassembl" in l or "assembl" in l: return 78
    if "burn" in l or "caption" in l: return 88
    if "done" in l:                  return 100
    return 0


def run_job_v2(job_id: str, input_path: str, options: dict):
    with JOBS_LOCK:
        JOBS[job_id] = {"status":"running","log":[],"output":None,
                        "progress":0,"filename":Path(input_path).name,
                        "tags": options.get("tags",[])}

    workdir = Path(tempfile.mkdtemp(prefix=f"dub2_{job_id}_"))
    cloned_id = None
    eleven_key = options.get("eleven_key","").strip()

    try:
        groq_key = os.environ.get("GROQ_API_KEY","")
        kyma_key = os.environ.get("KYMA_API_KEY","")
        mode = "kyma" if kyma_key else ("direct" if groq_key else "none")

        do_dub   = options.get("dubbing", True)
        do_vc    = options.get("voice_clone", False) and bool(eleven_key)
        do_hf    = options.get("hyperframes", False)
        do_recut = options.get("recut", True)   # new talking-head recut engine
        do_cap   = options.get("captions", True)
        do_bi    = options.get("bilingual", False)
        to_lang  = options.get("to_lang","en")
        voice    = options.get("voice","charlie")
        model    = options.get("model","") or "llama-3.3-70b-versatile"

        dubbed_out  = UPLOAD_DIR / f"{job_id}_dubbed.mp4"
        chunks_path = workdir / "chunks.json"
        srt_path    = str(UPLOAD_DIR / f"{job_id}_dubbed.srt")
        chunks      = []
        hf_cards    = None   # set in no-dub path by analyze_transcript
        recut_beats = None   # set in no-dub path by analyze_beats (recut engine)

        # ── 1. voice clone ────────────────────────────────────────────
        voice_id = _VOICE_IDS.get(voice, voice)
        if do_vc:
            _log(job_id, "Extracting voice sample for cloning…")
            try:
                sample = str(workdir / "vc_sample.mp3")
                _extract_sample(input_path, sample)
                cloned_id = _clone_voice(eleven_key, sample)
                if cloned_id:
                    voice_id = cloned_id
                    _log(job_id, f"Voice cloned → {cloned_id[:14]}…")
                else:
                    _log(job_id, "⚠ Clone failed — using preset voice")
            except Exception as ex:
                _log(job_id, f"⚠ Voice clone error ({ex}) — using preset voice")
        _pct(job_id, 8)

        # ── 2a. dubbing pipeline ──────────────────────────────────────
        if do_dub:
            # Pipeline burns captions only when HF is OFF (HF handles it in one pass)
            cfg = {
                "video":       str(Path(input_path).resolve()),
                "out":         str(dubbed_out),
                "mode":        mode,
                "kyma_base":   os.environ.get("KYMA_DUB_BASE","https://api.kymaapi.com"),
                "kyma_key":    kyma_key, "groq_key": groq_key,
                "eleven_key":  eleven_key or os.environ.get("ELEVENLABS_API_KEY",""),
                "ua":          "kyma-dub/0.2.0",
                "source_lang": "auto", "target_lang": to_lang,
                "voice": voice, "voice_id": voice_id,
                "minimax_voice": "English_expressive_narrator",
                "tts":           "kyma" if kyma_key else "elevenlabs",
                "translate_model": model,
                "max_speed": 1.5, "chunk_sec": 22.0,
                "allow_voice_fallback": True,
                "burn":     do_cap and not do_hf,   # HF will burn if it runs
                "srt":      do_cap,                  # always write SRT when captions wanted
                "dual_sub": do_bi, "bilingual": False,
                "keep_temp": False, "orig_vol": 0.08,
                "chunks_out": str(chunks_path),
            }
            cfg_file = workdir / "cfg.json"
            cfg_file.write_text(json.dumps(cfg))

            env = os.environ.copy(); env["PYTHONPATH"] = str(LIB_DIR)
            proc = subprocess.Popen(
                ["python3", str(PIPELINE), str(cfg_file)],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, env=env)
            for line in proc.stdout:
                line = line.rstrip()
                print(f"[v2:{job_id}] {line}", flush=True)
                with JOBS_LOCK:
                    JOBS[job_id]["log"].append(line)
                    p = _progress_from_line(line)
                    if p: JOBS[job_id]["progress"] = int(8 + p * 0.72)  # maps 0-100 → 8-80

            proc.wait()
            if proc.returncode != 0 or not dubbed_out.exists():
                raise RuntimeError("Dubbing pipeline failed — check log")

            if chunks_path.exists():
                try: chunks = json.loads(chunks_path.read_text())
                except: pass
            _pct(job_id, 80)

        # ── 2b. no-dub path: transcribe + AI correct + plan HF cards ───
        else:
            if do_hf or do_cap:
                _log(job_id, "Transcribing original audio…")
                cfg_tr = {"mode": mode, "kyma_base": os.environ.get("KYMA_DUB_BASE","https://api.kymaapi.com"),
                          "kyma_key": kyma_key, "groq_key": groq_key,
                          "ua": "kyma-dub/0.2.0", "source_lang": "auto"}
                cfg_hf_nodub = {"groq_key": groq_key, "kyma_key": kyma_key,
                                "kyma_base": os.environ.get("KYMA_DUB_BASE","https://api.kymaapi.com")}
                try:
                    chunks, raw_segs, lang = _transcribe_only(input_path, str(workdir), cfg_tr)
                    _log(job_id, f"Transcribed: {len(chunks)} chunks / {len(raw_segs)} segs, lang={lang}")

                    sys.path.insert(0, str(LIB_DIR))
                    if do_hf and do_recut:
                        # New talking-head recut engine: dense per-sentence beats
                        _log(job_id, "AI: planning talking-head recut beats…")
                        from recut import analyze_beats  # type: ignore
                        recut_beats = analyze_beats(
                            cfg_hf_nodub, raw_segs,
                            log=lambda m: _log(job_id, m))
                        n_gfx = sum(1 for b in recut_beats if b.get("template"))
                        _log(job_id, f"AI: {len(recut_beats)} beats, {n_gfx} có đồ hoạ")
                    elif do_hf:
                        # Legacy overlay-card path
                        _log(job_id, "AI: correcting transcript + planning HyperFramers cards…")
                        from hyperframes import analyze_transcript  # type: ignore
                        _, hf_cards = analyze_transcript(cfg_hf_nodub, raw_segs)
                        _log(job_id, f"AI: {len(hf_cards)} cards planned")

                    if do_cap:
                        # Use raw whisper segments for captions — AI correction can
                        # silently strip Vietnamese diacritics on small models.
                        cap_segs = [{"start": s["start"], "end": s["end"],
                                     "text": s.get("text","") or s.get("vi","")}
                                    for s in raw_segs]
                        _write_srt(cap_segs, srt_path)

                except Exception as ex:
                    _log(job_id, f"⚠ Transcription/AI failed ({ex}) — continuing without transcript")
                    import traceback; traceback.print_exc()
                    chunks = []; hf_cards = None; recut_beats = None
            _pct(job_id, 40)
            dubbed_out = Path(input_path)   # original video is the "dubbed" base

        # ── 3. HyperFramers overlay ───────────────────────────────────
        _pct(job_id, 82)
        final_out = dubbed_out

        if do_hf and dubbed_out.exists():
            sys.path.insert(0, str(LIB_DIR))
            cfg_hf  = {"groq_key": groq_key, "kyma_key": kyma_key,
                       "kyma_base": os.environ.get("KYMA_DUB_BASE","https://api.kymaapi.com")}
            hf_out  = str(UPLOAD_DIR / f"{job_id}_final.mp4")
            srt_arg = srt_path if do_cap and Path(srt_path).exists() else None
            use_pip = options.get("hf_pip", True)

            try:
                if recut_beats:
                    # New talking-head recut engine (GSAP per-frame render)
                    from recut import render_recut  # type: ignore
                    _log(job_id, f"Recut: rendering {len(recut_beats)} beats "
                                 f"(GSAP từng frame, có thể mất vài phút)…")
                    ok = render_recut(
                        str(dubbed_out), recut_beats, str(workdir), hf_out,
                        srt_path=srt_arg, fps=25, canvas=(1280, 720),
                        log=lambda m: _log(job_id, m))
                elif hf_cards is not None:
                    # No-dub path: segment-accurate individual cards from analyze_transcript
                    from hyperframes import overlay_cards_v2  # type: ignore
                    _log(job_id, f"Compositing {len(hf_cards)} timed cards "
                                 f"({'PIP' if use_pip else 'overlay'}, small/large styles)…")
                    ok = overlay_cards_v2(str(dubbed_out), hf_cards, str(workdir), hf_out,
                                          srt_path=srt_arg, pip=use_pip)
                else:
                    # Dub path: chunk-level scene grouping (existing behavior)
                    from hyperframes import extract_scenes, overlay_scenes  # type: ignore
                    _log(job_id, "Analysing transcript for HyperFramers scenes…")
                    scenes  = extract_scenes(cfg_hf, chunks)
                    n_sc    = sum(len(s.get("cards",[])) for s in scenes)
                    _log(job_id, f"AI created {len(scenes)} scenes / {n_sc} cards")
                    _log(job_id, f"Compositing ({'PIP layout' if use_pip else 'overlay'})…")
                    ok = overlay_scenes(str(dubbed_out), scenes, str(workdir), hf_out,
                                        srt_path=srt_arg, pip=use_pip)

                if ok and Path(hf_out).exists():
                    final_out = Path(hf_out)
                    _log(job_id, "HyperFramers composited ✓")
                else:
                    _log(job_id, "⚠ No HyperFramers output — falling back")
                    if do_cap and Path(srt_path).exists():
                        _burn_caps_only(str(dubbed_out), srt_path,
                                        str(UPLOAD_DIR / f"{job_id}_final.mp4"), job_id)
                        if Path(UPLOAD_DIR / f"{job_id}_final.mp4").exists():
                            final_out = UPLOAD_DIR / f"{job_id}_final.mp4"
            except Exception as ex:
                _log(job_id, f"⚠ HyperFramers error: {ex}")
                import traceback; traceback.print_exc()

        elif do_cap and not do_hf and dubbed_out.exists():
            # no HF, captions only (works for both dub and no-dub)
            if Path(srt_path).exists():
                _burn_caps_only(str(dubbed_out), srt_path,
                                str(UPLOAD_DIR / f"{job_id}_final.mp4"), job_id)
                if Path(UPLOAD_DIR / f"{job_id}_final.mp4").exists():
                    final_out = UPLOAD_DIR / f"{job_id}_final.mp4"

        _pct(job_id, 95)

        # ── 4. cleanup ────────────────────────────────────────────────
        if cloned_id and eleven_key:
            _delete_voice(eleven_key, cloned_id)

        with JOBS_LOCK:
            JOBS[job_id].update({"status":"done","output":str(final_out),"progress":100})

    except Exception as ex:
        if cloned_id and eleven_key: _delete_voice(eleven_key, cloned_id)
        with JOBS_LOCK:
            JOBS[job_id].update({"status":"error","log": JOBS[job_id]["log"] + [f"ERROR: {ex}"]})
    finally:
        shutil.rmtree(str(workdir), ignore_errors=True)


# ── HTML ──────────────────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Kyma Dub Studio v2</title>
<style>
*, *::before, *::after { box-sizing:border-box; margin:0; padding:0; }
:root {
  --blue:#2563eb; --blue-l:#3b82f6; --blue-50:#eff6ff; --blue-100:#dbeafe; --blue-700:#1d4ed8;
  --g50:#f9fafb; --g100:#f3f4f6; --g200:#e5e7eb; --g300:#d1d5db;
  --g400:#9ca3af; --g500:#6b7280; --g600:#4b5563; --g700:#374151; --g800:#1f2937; --g900:#111827;
  --green:#059669; --green-100:#d1fae5;
  --red:#dc2626; --red-50:#fef2f2;
  --purple:#7c3aed; --purple-50:#f5f3ff; --purple-100:#ede9fe;
  --amber:#d97706; --amber-50:#fffbeb; --amber-100:#fef3c7;
  --r:10px;
}
body { background:var(--g50); color:var(--g800); font-family:-apple-system,BlinkMacSystemFont,'Inter','Segoe UI',sans-serif; min-height:100vh; font-size:14px; }

header { background:#fff; border-bottom:1px solid var(--g200); padding:14px 24px; display:flex; align-items:center; gap:10px; }
.logo { width:30px; height:30px; background:linear-gradient(135deg,var(--blue),var(--purple)); border-radius:8px; display:flex; align-items:center; justify-content:center; }
.logo svg { width:16px; height:16px; }
header h1 { font-size:15px; font-weight:700; color:var(--g900); }
header h1 span { color:var(--blue); }
.v2pill { margin-left:6px; background:var(--purple-100); color:var(--purple); font-size:10px; font-weight:700; padding:2px 8px; border-radius:99px; }

.container { max-width:960px; margin:0 auto; padding:24px 20px; }

.card { background:#fff; border:1px solid var(--g200); border-radius:var(--r); margin-bottom:14px; }
.card-header { padding:13px 18px; border-bottom:1px solid var(--g100); }
.card-header h2 { font-size:11px; font-weight:700; color:var(--g500); text-transform:uppercase; letter-spacing:.06em; }
.card-body { padding:18px; }

/* dropzone */
.dropzone { border:2px dashed var(--g300); border-radius:var(--r); padding:34px 20px; text-align:center; cursor:pointer; transition:all .2s; position:relative; background:var(--g50); }
.dropzone:hover,.dropzone.over { border-color:var(--blue); background:var(--blue-50); }
.dropzone input { position:absolute; inset:0; opacity:0; cursor:pointer; width:100%; height:100%; }
.dropzone .icon { font-size:26px; margin-bottom:6px; }
.dropzone p { color:var(--g500); font-size:13px; }
#file-list { margin-top:10px; }
.file-item { display:flex; align-items:center; gap:8px; padding:7px 11px; background:var(--blue-50); border:1px solid var(--blue-100); border-radius:6px; margin-bottom:5px; font-size:12px; }
.file-item .name { flex:1; font-weight:500; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.file-item .size { color:var(--g400); font-size:11px; }
.file-item .rm { background:none; border:none; color:var(--g400); cursor:pointer; font-size:16px; line-height:1; }
.file-item .rm:hover { color:var(--red); }

/* feature cards grid */
.feats { display:flex; flex-direction:column; gap:10px; }

.fc {
  background:#fff; border:1.5px solid var(--g200); border-radius:12px;
  padding:16px 18px; transition:border-color .18s, box-shadow .18s;
}
.fc.on        { border-color:var(--blue);   box-shadow:0 0 0 3px rgba(37,99,235,.07); }
.fc.on.purple { border-color:var(--purple); box-shadow:0 0 0 3px rgba(124,58,237,.07); }
.fc.on.green  { border-color:var(--green);  box-shadow:0 0 0 3px rgba(5,150,105,.07); }
.fc.on.amber  { border-color:var(--amber);  box-shadow:0 0 0 3px rgba(217,119,6,.07); }

.fc-top { display:flex; align-items:flex-start; gap:10px; cursor:pointer; }
.fc-icon { font-size:20px; line-height:1; flex-shrink:0; margin-top:1px; }
.fc-info { flex:1; }
.fc-name { font-size:14px; font-weight:700; color:var(--g800); }
.fc-desc { font-size:12px; color:var(--g500); margin-top:2px; line-height:1.4; }

/* iOS toggle */
.tog { position:relative; width:44px; height:26px; display:inline-block; flex-shrink:0; }
.tog input { opacity:0; width:0; height:0; }
.tslider {
  position:absolute; inset:0; border-radius:13px; background:var(--g300);
  transition:background .2s; cursor:pointer;
}
.tslider::before {
  content:''; position:absolute; width:20px; height:20px; border-radius:50%;
  background:#fff; top:3px; left:3px; transition:transform .2s;
  box-shadow:0 1px 3px rgba(0,0,0,.2);
}
.tog input:checked + .tslider             { background:var(--blue); }
.tog input:checked + .tslider.purple      { background:var(--purple); }
.tog input:checked + .tslider.green       { background:var(--green); }
.tog input:checked + .tslider.amber       { background:var(--amber); }
.tog input:checked + .tslider::before     { transform:translateX(18px); }
.tog input:disabled + .tslider            { opacity:.4; cursor:not-allowed; }

/* sub-options */
.fc-sub { margin-top:14px; padding-top:14px; border-top:1px solid var(--g100); display:none; }
.fc-sub.open { display:block; }
.sub-grid { display:grid; grid-template-columns:1fr 1fr 1fr; gap:10px; }
@media(max-width:600px){ .sub-grid { grid-template-columns:1fr; } }

label.fl { font-size:11px; color:var(--g500); font-weight:600; display:block; margin-bottom:4px; text-transform:uppercase; letter-spacing:.04em; }
select {
  width:100%; background:#fff; border:1px solid var(--g300); border-radius:7px;
  padding:8px 10px; color:var(--g700); font-size:13px; outline:none; appearance:none;
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'%3E%3Cpath d='M3 5l3 3 3-3' stroke='%239ca3af' fill='none' stroke-width='1.5'/%3E%3C/svg%3E");
  background-repeat:no-repeat; background-position:right 10px center;
}
select:focus { border-color:var(--blue); box-shadow:0 0 0 3px rgba(37,99,235,.1); }

/* vc sub */
.vc-row { display:flex; align-items:flex-start; gap:8px; margin-top:10px; }
.vc-toggle-area { display:flex; align-items:center; gap:8px; font-size:13px; font-weight:500; color:var(--g700); cursor:pointer; user-select:none; }
.api-input {
  flex:1; border:1px solid var(--g300); border-radius:7px; padding:7px 10px;
  font-size:12px; color:var(--g700); outline:none; font-family:monospace;
}
.api-input:focus { border-color:var(--blue); box-shadow:0 0 0 3px rgba(37,99,235,.1); }
.api-input::placeholder { font-family:-apple-system,sans-serif; color:var(--g400); }

/* radio pills */
.pills { display:flex; gap:6px; flex-wrap:wrap; }
.pill { position:relative; }
.pill input { position:absolute; opacity:0; }
.pill label { display:inline-flex; align-items:center; gap:4px; padding:5px 12px; border:1.5px solid var(--g300); border-radius:99px; font-size:12px; font-weight:500; color:var(--g600); cursor:pointer; transition:all .15s; }
.pill input:checked + label { border-color:var(--blue); color:var(--blue); background:var(--blue-50); }
.pill-green input:checked + label { border-color:var(--green); color:var(--green); background:var(--green-100); }

/* submit */
.btn {
  margin-top:16px; width:100%; padding:11px; border:none; border-radius:9px;
  background:linear-gradient(135deg,var(--blue),var(--blue-l)); color:#fff;
  font-size:14px; font-weight:700; cursor:pointer; transition:opacity .15s; letter-spacing:.01em;
}
.btn:hover { opacity:.88; }
.btn:disabled { background:var(--g300); cursor:not-allowed; opacity:1; }

/* jobs */
#jobs-section { display:none; margin-top:18px; }
.jcard { margin-bottom:12px; }
.jhead { display:flex; align-items:center; gap:10px; margin-bottom:9px; }
.jname { font-weight:600; font-size:13px; color:var(--g800); flex:1; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.badge { display:inline-flex; align-items:center; gap:4px; padding:2px 9px; border-radius:99px; font-size:11px; font-weight:700; }
.badge.running { background:var(--blue-100); color:var(--blue-700); }
.badge.done    { background:var(--green-100); color:var(--green); }
.badge.error   { background:var(--red-50);    color:var(--red); }
.dot { width:6px; height:6px; border-radius:50%; background:currentColor; }
.dot.pulse { animation:pulse 1.2s infinite; }
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.3} }
.prow { display:flex; align-items:center; gap:10px; margin-bottom:6px; }
.pwrap { flex:1; background:var(--g100); border-radius:99px; height:6px; overflow:hidden; }
.pbar { height:100%; background:linear-gradient(90deg,var(--blue),var(--purple)); border-radius:99px; transition:width .4s; }
.pbar.done { background:var(--green); }
.pct { font-size:12px; color:var(--g400); min-width:32px; text-align:right; }
.ltog { background:none; border:none; color:var(--g400); font-size:12px; cursor:pointer; padding:3px 0; }
.log { background:var(--g900); border-radius:6px; padding:12px; max-height:150px; overflow-y:auto; font-family:'SF Mono',Menlo,monospace; font-size:11px; color:#9ca3af; line-height:1.5; margin-top:6px; display:none; }
.log.open { display:block; }
.ll.err { color:#f87171; }
.result-video { width:100%; border-radius:8px; background:#000; max-height:360px; margin-top:10px; }
.dl { display:inline-flex; align-items:center; gap:6px; padding:7px 14px; background:var(--green); border:none; border-radius:6px; color:#fff; text-decoration:none; font-size:12px; font-weight:600; margin-top:8px; }
.dl:hover { background:#047857; }
.new-btn { display:inline-flex; align-items:center; gap:6px; padding:8px 16px; background:var(--blue); border:none; border-radius:7px; color:#fff; font-size:13px; font-weight:600; cursor:pointer; margin-top:10px; }

.tags { display:flex; gap:4px; margin-bottom:8px; flex-wrap:wrap; }
.tag { display:inline-flex; align-items:center; gap:3px; padding:1px 8px; border-radius:99px; font-size:10px; font-weight:700; }
.tag.dub  { background:var(--blue-100);   color:var(--blue-700); }
.tag.vc   { background:#e0e7ff;            color:#4338ca; }
.tag.hf   { background:var(--purple-100); color:var(--purple); }
.tag.cap  { background:var(--green-100);  color:var(--green); }

.stage { font-size:11px; color:var(--g400); margin-bottom:5px; }

.info { background:var(--blue-50); border:1px solid var(--blue-100); border-radius:8px; padding:10px 14px; font-size:12px; color:var(--blue-700); display:flex; gap:8px; align-items:center; margin-bottom:14px; }
</style>
</head>
<body>

<header>
  <div class="logo">
    <svg viewBox="0 0 16 16" fill="none"><path d="M4 6l4-2 4 2v4l-4 2-4-2V6z" stroke="white" stroke-width="1.2"/><circle cx="8" cy="8" r="1.5" fill="white"/></svg>
  </div>
  <h1>Penny <span>Dub</span><span class="v2pill">studio</span></h1>
  <a href="/" style="margin-left:auto;font-size:13px;font-weight:600;color:var(--g500);text-decoration:none;padding:7px 12px">← Trang chủ</a>
  <a href="/recut" style="font-size:13px;font-weight:600;color:var(--blue);text-decoration:none;padding:7px 14px;border:1px solid var(--blue-100);border-radius:8px;background:var(--blue-50)">🎬 Recut →</a>
</header>

<div class="container">
  <div class="info">&#9432;&nbsp; Bật/tắt từng tính năng. HyperFramers tự chọn số card dựa theo độ dài video.</div>

  <form id="form">

    <!-- Upload -->
    <div class="card">
      <div class="card-header"><h2>Upload Video</h2></div>
      <div class="card-body">
        <div class="dropzone" id="drop">
          <input type="file" id="vfiles" accept="video/*" multiple>
          <div class="icon">&#8679;</div>
          <p><strong>Kéo thả video vào đây</strong> hoặc click để chọn</p>
          <p style="font-size:12px;margin-top:3px">MP4 / MOV / MKV &mdash; nhiều file cùng lúc</p>
        </div>
        <div id="file-list"></div>
      </div>
    </div>

    <!-- Features -->
    <div class="card">
      <div class="card-header"><h2>Tính năng</h2></div>
      <div class="card-body">
        <div class="feats">

          <!-- Dubbing -->
          <div class="fc" id="fc-dub">
            <div class="fc-top" onclick="toggleFeat('dub')">
              <div class="fc-icon">🎤</div>
              <div class="fc-info">
                <div class="fc-name">Dubbing</div>
                <div class="fc-desc">Dịch và lồng tiếng sang ngôn ngữ khác</div>
              </div>
              <label class="tog" onclick="event.stopPropagation()">
                <input type="checkbox" id="feat-dub" checked onchange="syncFeat('dub')">
                <span class="tslider"></span>
              </label>
            </div>
            <div class="fc-sub open" id="sub-dub">
              <div class="sub-grid">
                <div>
                  <label class="fl">Ngôn ngữ đích</label>
                  <select id="to-lang">
                    <option value="en">English</option>
                    <option value="vi">Tiếng Việt</option>
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
                  <label class="fl">Giọng đọc</label>
                  <select id="voice">
                    <option value="charlie">Charlie — trẻ, tự nhiên</option>
                    <option value="will">Will — trẻ, thân thiện</option>
                    <option value="liam">Liam — narrator</option>
                    <option value="brian">Brian — trầm, chín chắn</option>
                    <option value="rachel">Rachel — nữ, ấm</option>
                    <option value="adam">Adam — nam, trầm</option>
                    <option value="jessica">Jessica — nữ, trẻ</option>
                  </select>
                </div>
                <div>
                  <label class="fl">Model dịch</label>
                  <select id="model">
                    <option value="">Llama 3.3 70B</option>
                  </select>
                </div>
              </div>

              <!-- Voice Clone inside Dubbing -->
              <div style="margin-top:12px;padding-top:12px;border-top:1px solid var(--g100)">
                <div class="vc-row">
                  <div class="vc-toggle-area" onclick="toggleFeat('vc')">
                    <label class="tog" onclick="event.stopPropagation()">
                      <input type="checkbox" id="feat-vc" onchange="syncFeat('vc')">
                      <span class="tslider"></span>
                    </label>
                    🎙 Clone giọng gốc (ElevenLabs)
                  </div>
                </div>
                <div id="sub-vc" style="display:none;margin-top:10px">
                  <input class="api-input" type="password" id="eleven-key"
                    placeholder="ElevenLabs API Key — sk-... hoặc el_...">
                  <p style="margin-top:5px;font-size:11px;color:var(--g400)">Tự xóa voice sau khi dùng. Cần key ElevenLabs riêng.</p>
                </div>
              </div>
            </div>
          </div>

          <!-- HyperFramers -->
          <div class="fc" id="fc-hf">
            <div class="fc-top" onclick="toggleFeat('hf')">
              <div class="fc-icon">🎬</div>
              <div class="fc-info">
                <div class="fc-name">HyperFramers</div>
                <div class="fc-desc">AI tự phân tích transcript → tạo các khối trắng animated xuất hiện đúng lúc</div>
              </div>
              <label class="tog" onclick="event.stopPropagation()">
                <input type="checkbox" id="feat-hf" onchange="syncFeat('hf')">
                <span class="tslider purple"></span>
              </label>
            </div>
            <div class="fc-sub" id="sub-hf">
              <p style="font-size:12px;color:var(--g500);margin-top:2px;margin-bottom:8px">
                Mỗi scene hiện <strong>2-3 card cùng lúc</strong> — AI tự phân tích transcript để chọn từ khóa phù hợp.
              </p>
              <div class="pills">
                <div class="pill pill-purple">
                  <input type="radio" name="hf-layout" id="hf-overlay" value="overlay" checked>
                  <label for="hf-overlay">📌 Overlay (card chèn lên video gốc)</label>
                </div>
                <div class="pill pill-purple">
                  <input type="radio" name="hf-layout" id="hf-pip" value="pip">
                  <label for="hf-pip">🎬 Side layout (presenter trái, card phải)</label>
                </div>
              </div>
            </div>
          </div>

          <!-- Captions -->
          <div class="fc on green" id="fc-cap">
            <div class="fc-top" onclick="toggleFeat('cap')">
              <div class="fc-icon">💬</div>
              <div class="fc-info">
                <div class="fc-name">Captions</div>
                <div class="fc-desc">Burn phụ đề vào video — tiếng Anh hoặc song ngữ</div>
              </div>
              <label class="tog" onclick="event.stopPropagation()">
                <input type="checkbox" id="feat-cap" checked onchange="syncFeat('cap')">
                <span class="tslider green"></span>
              </label>
            </div>
            <div class="fc-sub open" id="sub-cap">
              <div class="pills">
                <div class="pill pill-green">
                  <input type="radio" name="cap-mode" id="cap-en" value="en" checked>
                  <label for="cap-en">&#127757; Ngôn ngữ đích</label>
                </div>
                <div class="pill pill-green">
                  <input type="radio" name="cap-mode" id="cap-bi" value="bilingual">
                  <label for="cap-bi">&#127881; Song ngữ</label>
                </div>
              </div>
            </div>
          </div>

        </div>
      </div>
    </div>

    <button class="btn" type="submit" id="dub-btn">&#9654;&nbsp; Bắt đầu xử lý</button>
  </form>

  <div id="jobs-section">
    <div class="card">
      <div class="card-header"><h2>Hàng đợi</h2></div>
      <div class="card-body" id="jcont"></div>
    </div>
    <button class="new-btn" id="new-btn" style="display:none">+ Video mới</button>
  </div>
</div>

<script>
let files = [], jobs = {}, timer = null;

// ── file input ────────────────────────────────────────────────────────
const fi = document.getElementById('vfiles');
const dz = document.getElementById('drop');
fi.addEventListener('change', () => addFiles(fi.files));
dz.addEventListener('dragover',  e => { e.preventDefault(); dz.classList.add('over'); });
dz.addEventListener('dragleave', () => dz.classList.remove('over'));
dz.addEventListener('drop', e => { e.preventDefault(); dz.classList.remove('over'); addFiles(e.dataTransfer.files); });

function addFiles(list) {
  for (const f of list)
    if (!files.find(x => x.name===f.name && x.size===f.size)) files.push(f);
  renderFiles();
}
function rmFile(i) { files.splice(i,1); renderFiles(); }
function renderFiles() {
  document.getElementById('file-list').innerHTML = files.map((f,i) =>
    `<div class="file-item"><span class="name">${esc(f.name)}</span><span class="size">${(f.size/1048576).toFixed(1)} MB</span><button type="button" class="rm" onclick="rmFile(${i})">&times;</button></div>`
  ).join('');
}

// ── feature toggles ───────────────────────────────────────────────────
const COLOR = { dub:'', hf:'purple', cap:'green' };

function syncFeat(id) {
  const cb  = document.getElementById('feat-'+id);
  const fc  = document.getElementById('fc-'+id);
  const sub = document.getElementById('sub-'+id);
  const col = COLOR[id];
  fc.classList.toggle('on', cb.checked);
  if (col) fc.classList.toggle(col, cb.checked);
  if (sub) sub.classList.toggle('open', cb.checked);

  // vc special: show/hide api key input
  if (id === 'vc') {
    document.getElementById('sub-vc').style.display = cb.checked ? 'block' : 'none';
  }
  // dubbing off → disable voice clone
  if (id === 'dub') {
    const vcCb = document.getElementById('feat-vc');
    vcCb.disabled = !cb.checked;
    if (!cb.checked) {
      vcCb.checked = false;
      document.getElementById('sub-vc').style.display = 'none';
    }
  }
}

function toggleFeat(id) {
  const cb = document.getElementById('feat-'+id);
  cb.checked = !cb.checked;
  syncFeat(id);
}

// init state on load
document.addEventListener('DOMContentLoaded', () => {
  syncFeat('dub');
  syncFeat('hf');
  syncFeat('cap');
});

// ── form submit ───────────────────────────────────────────────────────
document.getElementById('form').addEventListener('submit', async function(e) {
  e.preventDefault();
  if (!files.length) { alert('Chưa chọn video!'); return; }

  const btn = document.getElementById('dub-btn');
  btn.disabled = true;
  document.getElementById('jobs-section').style.display = 'block';

  const payload = {
    dubbing:     document.getElementById('feat-dub').checked ? '1' : '0',
    to_lang:     document.getElementById('to-lang').value,
    voice:       document.getElementById('voice').value,
    model:       document.getElementById('model').value,
    voice_clone: document.getElementById('feat-vc').checked ? '1' : '0',
    eleven_key:  document.getElementById('eleven-key')?.value || '',
    hyperframes: document.getElementById('feat-hf').checked ? '1' : '0',
    hf_pip:      (document.querySelector('input[name="hf-layout"]:checked')?.value !== 'overlay') ? '1' : '0',
    captions:    document.getElementById('feat-cap').checked ? '1' : '0',
    bilingual:   document.querySelector('input[name="cap-mode"]:checked')?.value === 'bilingual' ? '1' : '0',
  };

  for (const file of files) {
    const fd = new FormData();
    fd.append('video', file);
    Object.entries(payload).forEach(([k,v]) => fd.append(k,v));
    try {
      const r = await fetch('/api/dub', {method:'POST', body:fd});
      const d = await r.json();
      if (d.job_id) {
        jobs[d.job_id] = {filename:file.name, status:'running', progress:0, log:[], tags:d.tags||[]};
        renderJobs();
      }
    } catch(err) { console.error(err); }
  }
  if (!timer) timer = setInterval(pollAll, 2500);
});

// ── polling ───────────────────────────────────────────────────────────
async function pollAll() {
  const running = Object.keys(jobs).filter(id => jobs[id].status==='running');
  if (!running.length) {
    clearInterval(timer); timer = null;
    document.getElementById('dub-btn').disabled = false;
    document.getElementById('new-btn').style.display = 'inline-flex';
    return;
  }
  for (const id of running) {
    try {
      const d = await (await fetch('/api/status/'+id)).json();
      Object.assign(jobs[id], d);
    } catch(e){}
  }
  renderJobs();
}

function renderJobs() {
  const el = document.getElementById('jcont');
  const ids = Object.keys(jobs);
  el.innerHTML = ids.map((id,k) => {
    const j = jobs[id];
    const done=j.status==='done', err=j.status==='error', run=j.status==='running';
    const bc = done?'done':err?'error':'running';
    const bt = done?'Hoàn thành':err?'Lỗi':'Đang xử lý';
    const bp = run?' pulse':'';
    const bc2 = done?' done':'';
    const logLines = (j.log||[]).slice(-80);
    const last = logLines.filter(l=>l.includes('[kyma-dub]')||l.includes('[v2:')).slice(-1)[0]||'';
    const stage = last.replace(/\[.*?\]/g,'').trim().slice(0,70);
    const tagHtml = (j.tags||[]).map(t =>
      `<span class="tag ${t}">${{dub:'Dubbing',vc:'Voice Clone',hf:'HyperFramers',cap:'Captions'}[t]||t}</span>`
    ).join('');
    return `<div class="jcard">
      <div class="jhead"><span class="jname">${esc(j.filename)}</span><span class="badge ${bc}"><span class="dot${bp}"></span> ${bt}</span></div>
      ${tagHtml ? `<div class="tags">${tagHtml}</div>` : ''}
      <div class="prow"><div class="pwrap"><div class="pbar${bc2}" style="width:${j.progress}%"></div></div><span class="pct">${j.progress}%</span></div>
      ${stage && !done && !err ? `<div class="stage">${esc(stage)}</div>` : ''}
      <button type="button" class="ltog" onclick="tlog('${id}')">Xem log</button>
      <div class="log" id="log-${id}">${logLines.map(l=>`<div class="ll${l.startsWith('ERROR')||l.toLowerCase().includes('error')||l.includes('⚠')?' err':''}">${esc(l)}</div>`).join('')}</div>
      ${done?`<video class="result-video" controls src="/api/video/${id}"></video><a class="dl" href="/api/video/${id}" download>&#8681; Tải xuống</a>`:''}
    </div>${k<ids.length-1?'<hr style="border:none;border-top:1px solid var(--g100);margin:10px 0">':''}`;
  }).join('');
}

function tlog(id) {
  const el = document.getElementById('log-'+id);
  el.classList.toggle('open');
  if (el.classList.contains('open')) el.scrollTop = el.scrollHeight;
}

document.getElementById('new-btn').addEventListener('click', () => {
  jobs={}; files=[]; renderFiles(); renderJobs();
  document.getElementById('jobs-section').style.display='none';
  document.getElementById('new-btn').style.display='none';
  document.getElementById('dub-btn').disabled=false;
  fi.value='';
});

function esc(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
</script>
</body>
</html>"""


# ── Recut Studio page ─────────────────────────────────────────────────
RECUT_HTML = r"""<!DOCTYPE html>
<html lang="vi"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Recut Studio — Kyma</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{--blue:#2563eb;--blue2:#3b82f6;--blue50:#eff6ff;--ink:#0f172a;
  --slate:#475569;--g100:#f1f5f9;--g200:#e2e8f0;--g300:#cbd5e1;--r:12px}
body{background:#f8fafc;color:var(--ink);font-family:-apple-system,'Segoe UI',sans-serif;font-size:14px}
header{background:#fff;border-bottom:1px solid var(--g200);padding:14px 24px;display:flex;align-items:center;gap:12px}
.logo{width:30px;height:30px;background:linear-gradient(135deg,var(--blue),#7c3aed);border-radius:8px}
header h1{font-size:15px;font-weight:700}
header h1 span{color:var(--blue)}
.back{margin-left:auto;font-size:13px;color:var(--slate);text-decoration:none}
.wrap{max-width:1000px;margin:0 auto;padding:24px 20px}
.steps{display:flex;gap:8px;margin-bottom:22px}
.step{flex:1;padding:10px 14px;border-radius:10px;background:#fff;border:1px solid var(--g200);
  font-size:13px;font-weight:600;color:var(--g300);display:flex;align-items:center;gap:8px}
.step.active{color:var(--blue);border-color:var(--blue);background:var(--blue50)}
.step.done{color:#059669;border-color:#a7f3d0;background:#ecfdf5}
.step .n{width:22px;height:22px;border-radius:50%;background:currentColor;color:#fff;
  display:flex;align-items:center;justify-content:center;font-size:12px}
.card{background:#fff;border:1px solid var(--g200);border-radius:var(--r);padding:20px;margin-bottom:16px}
.drop{border:2px dashed var(--g300);border-radius:var(--r);padding:48px;text-align:center;cursor:pointer;background:#fff}
.drop:hover{border-color:var(--blue);background:var(--blue50)}
.drop p{color:var(--slate);margin-top:6px}
input[type=file]{display:none}
.btn{background:var(--blue);color:#fff;border:none;padding:11px 20px;border-radius:9px;
  font-size:14px;font-weight:600;cursor:pointer}
.btn:hover{background:#1d4ed8}
.btn.ghost{background:#fff;color:var(--slate);border:1px solid var(--g300)}
.btn:disabled{opacity:.5;cursor:default}
.row{display:flex;gap:16px;align-items:flex-start;padding:12px 0;border-bottom:1px solid var(--g100)}
.tm{font-size:11px;color:var(--slate);font-variant-numeric:tabular-nums;white-space:nowrap;padding-top:8px;min-width:88px}
textarea,input[type=text],select{width:100%;border:1px solid var(--g200);border-radius:8px;
  padding:8px 10px;font-size:13px;font-family:inherit;color:var(--ink);background:#fff}
textarea{resize:vertical;min-height:38px}
.sb-text{font-size:12px;color:var(--slate);padding:6px 0;line-height:1.4}
.sb-ctrls{display:grid;grid-template-columns:100px 120px 1fr 1fr;gap:8px;margin-top:6px}
.sb-ctrls .full{grid-column:1/-1;display:grid;grid-template-columns:1fr 1fr;gap:8px}
.sbrow{display:grid;grid-template-columns:1fr 250px;gap:18px;padding:16px 0;border-bottom:1px solid var(--g100)}
.sb-head{display:flex;gap:10px;align-items:center;margin-bottom:2px}
.rowbtns{display:flex;gap:6px;margin-left:auto}
.iconbtn{border:1px solid var(--g300);background:#fff;border-radius:6px;padding:3px 9px;cursor:pointer;font-size:12px;color:var(--slate)}
.iconbtn:hover{background:var(--g100)}
.iconbtn.del:hover{background:#fef2f2;color:#dc2626;border-color:#fecaca}
.sb-wire svg{width:100%;height:auto;display:block;border:1px solid var(--g200);border-radius:8px}
.wire-cap{font-size:11px;color:var(--g300);text-align:center;margin-top:4px}
.cap-toggle{display:flex;align-items:center;gap:6px;font-size:13px;color:var(--slate);margin-right:auto}
.cap-toggle input{width:auto}
.sb-head select{width:auto;padding:5px 8px;font-size:12px}
.blocks{margin-top:8px}
.blockrow{border:1px solid var(--g200);border-radius:9px;padding:8px;margin:6px 0;display:grid;grid-template-columns:1fr 96px 30px;gap:6px;align-items:center;background:#fbfdff}
.blockrow .bfields{grid-column:1/-1;display:grid;grid-template-columns:1fr 1fr;gap:6px}
.blockrow select{padding:6px 8px;font-size:12px}
.addblock{font-size:12px;color:var(--blue);background:var(--blue50);border:1px dashed #bfdbfe;border-radius:8px;padding:7px 12px;cursor:pointer;margin-top:2px}
.addblock:hover{background:#dbeafe}
.fields{margin-top:10px;display:flex;flex-direction:column;gap:7px}
.fields .fg{display:flex;gap:8px;align-items:center}
.fields input{flex:1}
.icsel{max-width:130px;flex:0 0 auto}
.sb-head select[data-f="template"]{width:auto;padding:5px 10px;font-size:13px}
.bar{display:flex;gap:10px;align-items:center;margin-top:16px}
.hint{font-size:12px;color:var(--slate)}
.spin{display:inline-block;width:16px;height:16px;border:2px solid var(--g200);
  border-top-color:var(--blue);border-radius:50%;animation:sp 0.7s linear infinite;vertical-align:middle}
@keyframes sp{to{transform:rotate(360deg)}}
.prog{height:8px;background:var(--g200);border-radius:99px;overflow:hidden;margin:12px 0}
.prog>i{display:block;height:100%;background:linear-gradient(90deg,var(--blue),var(--blue2));width:0;transition:width .3s}
.log{font-family:ui-monospace,monospace;font-size:12px;color:var(--slate);background:#f8fafc;
  border:1px solid var(--g200);border-radius:8px;padding:10px;max-height:180px;overflow:auto;white-space:pre-wrap}
video{width:100%;border-radius:var(--r);background:#000}
.hidden{display:none}
.facepill{font-size:10px;font-weight:700;padding:2px 7px;border-radius:99px;text-transform:uppercase}
.f-full{background:var(--g100);color:var(--slate)}
.f-side{background:var(--blue50);color:var(--blue)}
.f-hero{background:#f5f3ff;color:#7c3aed}
</style></head><body>
<header>
  <div class="logo"></div>
  <h1>Penny <span>Recut</span></h1>
  <a class="back" href="/">← Trang chủ</a>
</header>
<div class="wrap">
  <div class="steps">
    <div class="step active" id="st1"><span class="n">1</span> Transcript</div>
    <div class="step" id="st2"><span class="n">2</span> Storyboard</div>
    <div class="step" id="st3"><span class="n">3</span> Video</div>
  </div>

  <!-- Upload -->
  <div id="panel-up" class="card">
    <div class="drop" id="drop">
      <input type="file" id="vfile" accept="video/*">
      <strong>Kéo thả / chọn video</strong>
      <p>Bóc transcript → sửa → lên khung minh hoạ → duyệt → gen</p>
    </div>
    <div id="up-status" class="bar hidden"><span class="spin"></span><span class="hint" id="up-msg"></span></div>
  </div>

  <!-- Step 1: transcript -->
  <div id="panel-1" class="card hidden">
    <h3 style="margin-bottom:6px">1 · Transcript — sửa cho đúng lời nói</h3>
    <p class="hint" style="margin-bottom:12px">AI có thể tự sửa lỗi nghe nhầm, hoặc bạn sửa tay. Giữ đúng nghĩa để minh hoạ chuẩn.</p>
    <div id="tr-rows"></div>
    <div class="bar">
      <button class="btn ghost" id="btn-ai-correct">✨ AI sửa transcript</button>
      <button class="btn" id="btn-to-sb">Tiếp: Lên khung →</button>
      <span class="hint" id="tr-msg"></span>
    </div>
  </div>

  <!-- Step 2: storyboard -->
  <div id="panel-2" class="card hidden">
    <h3 style="margin-bottom:6px">2 · Storyboard — mỗi câu minh hoạ thế nào</h3>
    <p class="hint" style="margin-bottom:12px">Chọn kiểu hiển thị + nội dung đồ hoạ cho từng câu. Timing đã khoá đúng theo voice.</p>
    <div id="sb-rows"></div>
    <div class="bar" style="border-top:1px solid var(--g200);padding-top:14px;margin-top:8px">
      <label class="cap-toggle"><input type="checkbox" id="cap-on" checked> Caption (nhỏ, nền đen 90%)</label>
      <button class="btn ghost" id="btn-back-1">← Transcript</button>
      <button class="btn" id="btn-gen">🎬 Gen video</button>
      <span class="hint" id="sb-msg"></span>
    </div>
  </div>

  <!-- Step 3: render -->
  <div id="panel-3" class="card hidden">
    <h3 style="margin-bottom:10px">3 · Đang dựng video</h3>
    <div class="prog"><i id="pbar"></i></div>
    <div class="log" id="rlog"></div>
    <div id="result" class="hidden" style="margin-top:16px">
      <video id="vid" controls></video>
      <div class="bar"><a class="btn" id="dl" download>⬇ Tải video</a>
        <button class="btn ghost" id="btn-redo">Sửa storyboard</button></div>
    </div>
  </div>
</div>

<script>
const TPL = ['','concept','minimal','list','flow','slide','stat','compare'];
const TPL_LBL = {'':'— không minh hoạ —','concept':'Khái niệm','minimal':'Tối giản',
  'list':'Liệt kê','flow':'Sơ đồ luồng','slide':'Slide','stat':'Con số','compare':'So sánh'};
const ICON_OPTS = ['','notebook','cloud','code','youtube','search','chart','link','doc','video','gear','rocket','bulb'];
let RID=null, SEG=[], ROWS=[];
const $=(s)=>document.querySelector(s);
const esc=(s)=>String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
const tfmt=(t)=>{t=+t;let m=Math.floor(t/60),s=(t%60);return m+':'+s.toFixed(1).padStart(4,'0');};

function setStep(n){for(let i=1;i<=3;i++){const e=$('#st'+i);e.classList.remove('active','done');
  if(i<n)e.classList.add('done'); else if(i===n)e.classList.add('active');}}
function show(id){['panel-up','panel-1','panel-2','panel-3'].forEach(p=>$('#'+p).classList.add('hidden'));$('#'+id).classList.remove('hidden');}

// ── upload (click + drag-drop) ──
const drop=$('#drop');
drop.onclick=()=>$('#vfile').click();
drop.addEventListener('dragover',(e)=>{e.preventDefault();drop.style.borderColor='#2563eb';drop.style.background='#eff6ff';});
drop.addEventListener('dragleave',(e)=>{e.preventDefault();drop.style.borderColor='';drop.style.background='';});
drop.addEventListener('drop',(e)=>{
  e.preventDefault();drop.style.borderColor='';drop.style.background='';
  const f=e.dataTransfer.files[0];
  if(f&&f.type.startsWith('video')){startUpload(f);}
  else{$('#up-status').classList.remove('hidden');$('#up-msg').textContent='File không phải video.';}
});
$('#vfile').onchange=(e)=>{const f=e.target.files[0]; if(f)startUpload(f);};
async function startUpload(f){
  $('#up-status').classList.remove('hidden');
  $('#up-msg').innerHTML='<span class="spin"></span> Đang tải & bóc transcript…';
  const fd=new FormData(); fd.append('video',f);
  try{
    const r=await(await fetch('/api/recut/start',{method:'POST',body:fd})).json();
    RID=r.rid; pollTranscribe();
  }catch(e){$('#up-msg').textContent='Lỗi tải video: '+e;}
}
async function pollTranscribe(){
  const d=await(await fetch('/api/recut/status/'+RID)).json();
  $('#up-msg').innerHTML='<span class="spin"></span> '+((d.log&&d.log.slice(-1)[0])||'…');
  if(d.status==='transcribed'){SEG=d.segments;renderTranscript();show('panel-1');setStep(1);return;}
  if(d.status==='error'){
    $('#up-msg').innerHTML='⚠ '+(d.error||'Lỗi bóc transcript.')+' — <a href="javascript:location.reload()">thử lại</a>';
    return;}
  setTimeout(pollTranscribe,1500);
}

// ── step 1: transcript editor ──
function renderTranscript(){
  $('#tr-rows').innerHTML=SEG.map((s,i)=>
    `<div class="row"><div class="tm">${tfmt(s.start)} → ${tfmt(s.end)}</div>
     <textarea data-i="${i}">${esc(s.text)}</textarea></div>`).join('');
}
function gatherTranscript(){
  document.querySelectorAll('#tr-rows textarea').forEach(t=>{SEG[+t.dataset.i].text=t.value;});
  return SEG;
}
$('#btn-ai-correct').onclick=async()=>{
  gatherTranscript();
  $('#tr-msg').innerHTML='<span class="spin"></span> AI đang sửa…';
  const r=await(await fetch('/api/recut/correct',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({rid:RID,segments:SEG})})).json();
  SEG=r.segments; renderTranscript(); $('#tr-msg').textContent='Đã sửa ✓';
};
$('#btn-to-sb').onclick=async()=>{
  gatherTranscript();
  $('#tr-msg').innerHTML='<span class="spin"></span> Đang lên khung…';
  const r=await(await fetch('/api/recut/storyboard',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({rid:RID,segments:SEG})})).json();
  ROWS=r.rows; renderStoryboard(); show('panel-2'); setStep(2); $('#tr-msg').textContent='';
};

// ── step 2: storyboard editor (scene templates) ──
const trunc=(s,n)=>{s=String(s||'');return s.length>n?s.slice(0,n-1)+'…':s;};
const inp=(i,f,ph,val)=>`<input type="text" data-i="${i}" data-f="${f}" placeholder="${ph}" value="${esc(val||'')}">`;
function iconSel(i,f,val,k){
  const kk=(k==null?'':` data-k="${k}"`);
  return `<select class="icsel" data-i="${i}" data-f="${f}"${kk}>`+ICON_OPTS.map(o=>`<option value="${o}"${(val||'')===o?' selected':''}>${o||'— icon —'}</option>`).join('')+`</select>`;
}
function fieldsHTML(row,i){
  const t=row.template; if(!t) return '<div class="hint">Câu này không minh hoạ.</div>';
  if(t==='concept') return `<div class="fg">${inp(i,'eyebrow','Nhãn (vd CÔNG CỤ)',row.eyebrow)}${iconSel(i,'icon',row.icon)}</div>${inp(i,'title','Tên (≤3 từ)',row.title)}${inp(i,'sub','Mô tả (≤5 từ)',row.sub)}`;
  if(t==='minimal') return `<div class="fg">${inp(i,'title','Từ khoá',row.title)}${iconSel(i,'icon',row.icon)}</div><div class="fg">${inp(i,'sub','Từ khoá 2 (tuỳ chọn)',row.sub)}${iconSel(i,'sub_icon',row.sub_icon)}</div>`;
  if(t==='stat') return `<div class="fg">${inp(i,'value','Số (vd 100+)',row.value)}${inp(i,'label','Nhãn',row.label)}</div>`;
  if(t==='compare') return `<div class="fg">${inp(i,'a','Vế A',row.a)}${inp(i,'b','Vế B',row.b)}</div>`;
  if(t==='flow'){
    const st=row.steps||[];
    let h=st.map((s,k)=>`<div class="fg"><input type="text" data-i="${i}" data-f="step" data-k="${k}" placeholder="Bước ${k+1}" value="${esc(s)}"><button class="iconbtn del" data-act="delstep" data-i="${i}" data-k="${k}">✕</button></div>`).join('');
    if(st.length<3) h+=`<button class="addblock" data-act="addstep" data-i="${i}">+ Bước</button>`;
    h+=`<div style="margin-top:6px">${inp(i,'label','Nhãn mũi tên (vd kết nối)',row.label)}</div>`;
    return h;
  }
  // list / slide
  const isList=(t==='list');
  let h=inp(i,'title','Tiêu đề',row.title);
  const its=row.items||[];
  h+=its.map((it,k)=>`<div class="fg"><input type="text" data-i="${i}" data-f="item-text" data-k="${k}" placeholder="Mục ${k+1}" value="${esc(it.text)}">`+(isList?iconSel(i,'item-icon',it.icon,k):'')+`<button class="iconbtn del" data-act="delitem" data-i="${i}" data-k="${k}">✕</button></div>`).join('');
  if(its.length<4) h+=`<button class="addblock" data-act="additem" data-i="${i}">+ Mục</button>`;
  return h;
}

function wireframeSVG(row){
  const W=224,H=126,t=row.template;
  const face=(t==='minimal'||t==='flow'||t==='compare'||!t)?'full':(t==='slide'?'hero':'side');
  let fR; if(face==='side')fR={x:132,y:8,w:84,h:110}; else if(face==='hero')fR={x:178,y:80,w:38,h:42}; else fR={x:0,y:0,w:W,h:H};
  const id='g'+String(row.t0).replace('.','');
  const T=(x,y,txt,sz,col,w)=>`<text x="${x}" y="${y}" font-size="${sz}" font-weight="${w||700}" fill="${col||'#0f172a'}" font-family="-apple-system,Arial">${esc(txt)}</text>`;
  let s=`<svg viewBox="0 0 ${W} ${H}" xmlns="http://www.w3.org/2000/svg"><defs><linearGradient id="${id}" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#ffffff"/><stop offset="1" stop-color="#e3edfb"/></linearGradient></defs>`;
  s+=`<rect width="${W}" height="${H}" fill="url(#${id})"/>`;
  s+=`<rect x="${fR.x}" y="${fR.y}" width="${fR.w}" height="${fR.h}" rx="${face==='full'?0:8}" fill="#cbd5e1"/><circle cx="${fR.x+fR.w/2}" cy="${fR.y+fR.h*0.42}" r="${Math.min(fR.w,fR.h)*0.16}" fill="#94a3b8"/>`;
  if(!t){}
  else if(t==='concept'){ if(row.eyebrow)s+=T(12,26,trunc(row.eyebrow,10),8,'#2563eb',800); s+=T(12,45,trunc(row.title,13),15,'#0f172a',800); if(row.sub)s+=T(12,61,trunc(row.sub,18),9,'#475569',500); s+=`<rect x="12" y="68" width="26" height="4" rx="2" fill="#2563eb"/>`; }
  else if(t==='minimal'){ s+=`<rect x="66" y="20" width="92" height="22" rx="11" fill="#2563eb"/>`+T(78,35,trunc(row.title,11),11,'#fff',700); if(row.sub){s+=`<rect x="78" y="48" width="68" height="18" rx="9" fill="#fff" stroke="#bfdbfe"/>`+T(86,60,trunc(row.sub,9),9,'#1e3a8a',700);} }
  else if(t==='list'){ s+=T(12,28,trunc(row.title,15),12,'#0f172a',800); (row.items||[]).slice(0,3).forEach((it,k)=>{const yy=45+k*16; s+=`<circle cx="16" cy="${yy-4}" r="4" fill="#2563eb"/>`+T(26,yy,trunc(it.text,13),9,'#0f172a',600);}); }
  else if(t==='flow'){ const st=(row.steps||[]).slice(0,3); let x=24; st.forEach((sp,k)=>{ if(k>0){s+=`<line x1="${x}" y1="30" x2="${x+14}" y2="30" stroke="#2563eb" stroke-width="2"/>`; x+=18;} const w=Math.max(38,String(sp).length*7+12); s+=`<rect x="${x}" y="20" width="${w}" height="20" rx="10" fill="${k%2?'#2563eb':'#fff'}" stroke="#bfdbfe"/>`+T(x+7,34,trunc(sp,8),9,k%2?'#fff':'#1e3a8a',700); x+=w+4;}); }
  else if(t==='slide'){ s+=T(12,30,trunc(row.title,16),13,'#0f172a',800); (row.items||[]).slice(0,3).forEach((it,k)=>{const yy=49+k*15; s+=`<circle cx="19" cy="${yy-4}" r="7" fill="#2563eb"/>`+T(16,yy-1,''+(k+1),8,'#fff',800)+T(31,yy,trunc(it.text,15),9,'#0f172a',600);}); }
  else if(t==='stat'){ s+=T(12,60,trunc(row.value||'12',5),34,'#2563eb',800); if(row.label)s+=T(12,78,trunc(row.label,16),9,'#0f172a',600); }
  else if(t==='compare'){ s+=`<rect x="12" y="34" width="70" height="18" rx="6" fill="#fff" stroke="#bfdbfe"/>`+T(18,47,trunc(row.a,9),9,'#1e3a8a',700)+T(40,64,'VS',8,'#94a3b8',700)+`<rect x="12" y="68" width="70" height="18" rx="6" fill="#2563eb"/>`+T(18,81,trunc(row.b,9),9,'#fff',700); }
  s+='</svg>'; return s;
}

function ensureFields(row){
  if((row.template==='list'||row.template==='slide')&&!(row.items&&row.items.length)) row.items=[{text:'',icon:''},{text:'',icon:''}];
  if(row.template==='flow'&&!(row.steps&&row.steps.length)) row.steps=['',''];
}
function renderStoryboard(){
  $('#sb-rows').innerHTML=ROWS.map((row,i)=>{
    const tplSel=TPL.map(t=>`<option value="${t}"${(row.template||'')===t?' selected':''}>${TPL_LBL[t]}</option>`).join('');
    return `<div class="sbrow" data-row="${i}">
      <div class="sb-left">
        <div class="sb-head"><span class="tm">${tfmt(row.t0)} → ${tfmt(row.t1)}</span>
          <select data-i="${i}" data-f="template">${tplSel}</select>
          <div class="rowbtns"><button class="iconbtn" data-act="split" data-i="${i}">✂ Tách</button><button class="iconbtn del" data-act="del" data-i="${i}">🗑</button></div>
        </div>
        <div class="sb-text">${esc(row.text)}</div>
        <div class="fields">${fieldsHTML(row,i)}</div>
      </div>
      <div class="sb-wire"><div id="wire-${i}">${wireframeSVG(row)}</div><div class="wire-cap">xem trước khung</div></div>
    </div>`;
  }).join('');
  bindStoryboard();
}
function updateWire(i){const el=document.getElementById('wire-'+i);if(el)el.innerHTML=wireframeSVG(ROWS[i]);}
function bindStoryboard(){
  document.querySelectorAll('#sb-rows select[data-f="template"]').forEach(s=>s.onchange=()=>{const i=+s.dataset.i;ROWS[i].template=s.value;ensureFields(ROWS[i]);renderStoryboard();});
  document.querySelectorAll('#sb-rows [data-act]').forEach(b=>b.onclick=()=>{const i=+b.dataset.i,k=+b.dataset.k,a=b.dataset.act;
    if(a==='split')splitBeat(i); else if(a==='del')delBeat(i);
    else if(a==='additem'){(ROWS[i].items=ROWS[i].items||[]).push({text:'',icon:''});renderStoryboard();}
    else if(a==='delitem'){ROWS[i].items.splice(k,1);renderStoryboard();}
    else if(a==='addstep'){(ROWS[i].steps=ROWS[i].steps||[]).push('');renderStoryboard();}
    else if(a==='delstep'){ROWS[i].steps.splice(k,1);renderStoryboard();}
  });
  bindFields();
}
function bindFields(){
  document.querySelectorAll('#sb-rows [data-f]').forEach(el=>{
    if(el.dataset.f==='template') return;
    const h=()=>{const i=+el.dataset.i,f=el.dataset.f,k=el.dataset.k;
      if(f==='item-text')ROWS[i].items[+k].text=el.value;
      else if(f==='item-icon')ROWS[i].items[+k].icon=el.value;
      else if(f==='step')ROWS[i].steps[+k]=el.value;
      else ROWS[i][f]=el.value;
      updateWire(i);
    };
    el.oninput=h; el.onchange=h;
  });
}
function splitBeat(i){const r=ROWS[i],mid=+((r.t0+r.t1)/2).toFixed(2);const a=JSON.parse(JSON.stringify(r));a.t1=mid;const b=JSON.parse(JSON.stringify(r));b.t0=mid;ROWS.splice(i,1,a,b);renderStoryboard();}
function delBeat(i){if(ROWS.length<=1)return;const r=ROWS[i];if(i>0)ROWS[i-1].t1=r.t1;else ROWS[1].t0=r.t0;ROWS.splice(i,1);renderStoryboard();}

$('#btn-back-1').onclick=()=>{show('panel-1');setStep(1);};
$('#btn-gen').onclick=async()=>{
  $('#sb-msg').innerHTML='<span class="spin"></span> Bắt đầu dựng…';
  const caps=$('#cap-on').checked;
  await fetch('/api/recut/generate',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({rid:RID,segments:SEG,rows:ROWS,captions:caps})});
  show('panel-3'); setStep(3); $('#result').classList.add('hidden'); pollRender();
};

// ── step 3: render ──
async function pollRender(){
  const d=await(await fetch('/api/recut/status/'+RID)).json();
  $('#pbar').style.width=(d.progress||0)+'%';
  $('#rlog').textContent=(d.log||[]).slice(-14).join('\n');
  $('#rlog').scrollTop=1e9;
  if(d.status==='done'){
    $('#vid').src='/api/recut/video/'+RID+'?t='+Date.now();
    $('#dl').href='/api/recut/video/'+RID; $('#result').classList.remove('hidden');return;}
  if(d.status==='error'){$('#rlog').textContent+='\n⚠ Lỗi.';return;}
  setTimeout(pollRender,2000);
}
$('#btn-redo').onclick=()=>{show('panel-2');setStep(2);};
</script>
</body></html>"""


# ── landing page (2 versions) ─────────────────────────────────────────
LANDING_HTML = r"""<!DOCTYPE html><html lang="vi"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Penny Studio</title><style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#f8fafc;color:#0f172a;font-family:-apple-system,'Segoe UI',sans-serif;min-height:100vh;
  display:flex;flex-direction:column;align-items:center;justify-content:center;padding:24px}
.logo{width:52px;height:52px;border-radius:14px;background:linear-gradient(135deg,#2563eb,#7c3aed);margin-bottom:18px}
h1{font-size:30px;font-weight:800}h1 span{color:#2563eb}
.tag{color:#64748b;margin:8px 0 34px;font-size:15px}
.cards{display:flex;gap:20px;flex-wrap:wrap;justify-content:center;max-width:760px}
.card{background:#fff;border:1px solid #e2e8f0;border-radius:16px;padding:28px;width:340px;text-decoration:none;
  color:inherit;transition:.15s;box-shadow:0 4px 18px rgba(15,23,42,.05)}
.card:hover{border-color:#2563eb;box-shadow:0 10px 30px rgba(37,99,235,.14);transform:translateY(-2px)}
.ic{font-size:34px}
.card h2{font-size:20px;font-weight:700;margin:12px 0 6px}
.card p{color:#475569;font-size:14px;line-height:1.55}
.card .go{margin-top:16px;color:#2563eb;font-weight:600;font-size:14px}
.foot{margin-top:30px;color:#94a3b8;font-size:12px}
</style></head><body>
<div class="logo"></div>
<h1>Penny <span>Studio</span></h1>
<div class="tag">Bộ công cụ video AI — lồng tiếng & dựng minh hoạ theo giọng nói</div>
<div class="cards">
  <a class="card" href="/dub">
    <div class="ic">🎤</div><h2>Penny Dub</h2>
    <p>Dịch & lồng tiếng video sang ngôn ngữ khác bằng giọng AI tự nhiên, khớp thời gian gốc. Có voice-clone + phụ đề.</p>
    <div class="go">Mở Dub →</div>
  </a>
  <a class="card" href="/recut">
    <div class="ic">🎬</div><h2>Penny Recut</h2>
    <p>Giữ nguyên tiếng gốc, AI tự thiết kế minh hoạ (khái niệm, sơ đồ, con số…) theo từng câu nói. Duyệt storyboard rồi render.</p>
    <div class="go">Mở Recut →</div>
  </a>
</div>
<div class="foot">Penny Studio · chạy local · cần API key (xem README)</div>
</body></html>"""


# ── routes ───────────────────────────────────────────────────────────
@app.route("/")
def index(): return LANDING_HTML

@app.route("/dub")
def dub_page(): return HTML

@app.route("/api/dub", methods=["POST"])
def start_dub():
    file = request.files.get("video")
    if not file: return jsonify({"error":"no video"}), 400

    options = {
        "dubbing":     request.form.get("dubbing","1") == "1",
        "to_lang":     request.form.get("to_lang","en"),
        "voice":       request.form.get("voice","charlie"),
        "model":       request.form.get("model",""),
        "voice_clone": request.form.get("voice_clone") == "1",
        "eleven_key":  request.form.get("eleven_key","").strip(),
        "hyperframes": request.form.get("hyperframes") == "1",
        "hf_pip":      request.form.get("hf_pip","1") == "1",
        "recut":       request.form.get("recut","1") == "1",
        "captions":    request.form.get("captions","1") == "1",
        "bilingual":   request.form.get("bilingual") == "1",
    }

    tags = []
    if options["dubbing"]:     tags.append("dub")
    if options["voice_clone"] and options["eleven_key"]: tags.append("vc")
    if options["hyperframes"]: tags.append("hf")
    if options["captions"]:    tags.append("cap")
    options["tags"] = tags

    job_id = uuid.uuid4().hex[:12]
    suf = Path(file.filename).suffix or ".mp4"
    in_path = UPLOAD_DIR / f"{job_id}_input{suf}"
    file.save(str(in_path))

    t = threading.Thread(target=run_job_v2,
                         args=(job_id, str(in_path), options), daemon=True)
    t.start()
    return jsonify({"job_id": job_id, "tags": tags})

@app.route("/api/status/<job_id>")
def status(job_id):
    with JOBS_LOCK: j = dict(JOBS.get(job_id,{}))
    return jsonify(j)

@app.route("/api/video/<job_id>")
def video(job_id):
    with JOBS_LOCK: j = JOBS.get(job_id,{})
    out = j.get("output")
    if not out or not Path(out).exists(): return "not found", 404
    return send_file(out, mimetype="video/mp4")

# ── Recut Studio: staged transcript → storyboard → render ─────────────
RECUT: dict[str, dict] = {}
RECUT_LOCK = threading.Lock()

def _rset(rid, **kw):
    with RECUT_LOCK: RECUT[rid].update(kw)

def _rlog(rid, msg):
    with RECUT_LOCK: RECUT[rid]["log"].append(msg)
    print(f"[recut:{rid}] {msg}", flush=True)

def _recut_cfg():
    return {"groq_key": os.environ.get("GROQ_API_KEY",""),
            "kyma_key": os.environ.get("KYMA_API_KEY",""),
            "kyma_base": os.environ.get("KYMA_DUB_BASE","https://api.kymaapi.com")}

def _recut_transcribe(rid, video):
    try:
        _rset(rid, status="transcribing", progress=10)
        _rlog(rid, "Đang bóc transcript…")
        cfg_tr = {"mode": ("kyma" if os.environ.get("KYMA_API_KEY") else "direct"),
                  "kyma_base": os.environ.get("KYMA_DUB_BASE","https://api.kymaapi.com"),
                  "kyma_key": os.environ.get("KYMA_API_KEY",""),
                  "groq_key": os.environ.get("GROQ_API_KEY",""),
                  "ua": "kyma-dub/0.2.0", "source_lang": "auto"}
        workdir = UPLOAD_DIR / f"{rid}_work"
        workdir.mkdir(parents=True, exist_ok=True)
        last_ex = None
        raw_segs = None
        for attempt in range(1, 4):          # retry transient network errors
            try:
                _, raw_segs, lang = _transcribe_only(video, str(workdir), cfg_tr)
                break
            except Exception as ex:
                last_ex = ex
                _rlog(rid, f"Bóc lần {attempt} lỗi mạng, thử lại…")
                import time as _t; _t.sleep(3)
        if raw_segs is None:
            raise last_ex or RuntimeError("transcribe failed")
        segs = [{"i": i, "start": round(float(s.get("start",0)),2),
                 "end": round(float(s.get("end",0)),2),
                 "text": (s.get("text","") or s.get("vi","")).strip()}
                for i, s in enumerate(raw_segs)]
        _rset(rid, segments=segs, lang=lang, status="transcribed", progress=100)
        _rlog(rid, f"Bóc xong {len(segs)} câu ({lang})")
    except Exception as ex:
        msg = str(ex)
        if "35" in msg or "timed out" in msg.lower() or "empty response" in msg.lower():
            msg = "Lỗi mạng khi gọi API phiên âm (Groq). Kiểm tra internet rồi thử lại."
        _rlog(rid, f"Lỗi bóc transcript: {msg}")
        import traceback; traceback.print_exc()
        _rset(rid, status="error", error=msg)

def _recut_render(rid, segments, rows):
    try:
        _rset(rid, status="generating", progress=5)
        sys.path.insert(0, str(LIB_DIR))
        from recut import row_to_beat, render_recut  # type: ignore
        beats = [row_to_beat(r) for r in rows]
        video = RECUT[rid]["video"]
        workdir = str(UPLOAD_DIR / f"{rid}_work")
        srt = str(UPLOAD_DIR / f"{rid}.srt")
        LEAD = 0.4   # nudge captions earlier to reduce perceived lag
        _write_srt([{"start": max(0.0, s["start"] - LEAD),
                     "end": max(0.2, s["end"] - LEAD), "text": s["text"]}
                    for s in segments if s.get("text","").strip()], srt)
        out = str(UPLOAD_DIR / f"{rid}_recut.mp4")
        ok = render_recut(video, beats, workdir, out, srt_path=srt,
                          fps=25, canvas=(1280,720),
                          captions=RECUT[rid].get("captions", True),
                          log=lambda m: _rlog(rid, m))
        if ok and Path(out).exists():
            _rset(rid, status="done", output=out, progress=100)
            _rlog(rid, "Xong ✓")
        else:
            _rset(rid, status="error"); _rlog(rid, "Render thất bại")
    except Exception as ex:
        _rlog(rid, f"Lỗi render: {ex}")
        import traceback; traceback.print_exc()
        _rset(rid, status="error")

@app.route("/api/recut/start", methods=["POST"])
def recut_start():
    file = request.files.get("video")
    if not file: return jsonify({"error":"no video"}), 400
    rid = uuid.uuid4().hex[:12]
    suf = Path(file.filename).suffix or ".mp4"
    in_path = UPLOAD_DIR / f"{rid}_input{suf}"
    file.save(str(in_path))
    with RECUT_LOCK:
        RECUT[rid] = {"status":"transcribing","log":[],"progress":0,
                      "video":str(in_path),"segments":[],"rows":[],
                      "filename":file.filename}
    threading.Thread(target=_recut_transcribe, args=(rid, str(in_path)),
                     daemon=True).start()
    return jsonify({"rid": rid})

@app.route("/api/recut/status/<rid>")
def recut_status(rid):
    with RECUT_LOCK: r = dict(RECUT.get(rid, {}))
    r.pop("video", None)
    return jsonify(r)

@app.route("/api/recut/correct", methods=["POST"])
def recut_correct():
    d = request.get_json(force=True)
    rid = d.get("rid"); segs = d.get("segments") or []
    if rid not in RECUT: return jsonify({"error":"unknown rid"}), 404
    sys.path.insert(0, str(LIB_DIR))
    from recut import correct_transcript  # type: ignore
    corr = correct_transcript(_recut_cfg(),
                              [{"start":s["start"],"end":s["end"],"text":s["text"]} for s in segs],
                              log=lambda m: _rlog(rid, m))
    out = [{"i":i,"start":c["start"],"end":c["end"],"text":c["text"]}
           for i, c in enumerate(corr)]
    _rset(rid, segments=out)
    return jsonify({"segments": out})

@app.route("/api/recut/storyboard", methods=["POST"])
def recut_storyboard():
    d = request.get_json(force=True)
    rid = d.get("rid"); segs = d.get("segments") or []
    if rid not in RECUT: return jsonify({"error":"unknown rid"}), 404
    _rset(rid, segments=segs)
    sys.path.insert(0, str(LIB_DIR))
    from recut import analyze_beats, beat_to_row  # type: ignore
    beats = analyze_beats(_recut_cfg(),
                          [{"start":s["start"],"end":s["end"],"text":s["text"]} for s in segs],
                          log=lambda m: _rlog(rid, m))
    rows = [beat_to_row(b, [{"start":s["start"],"end":s["end"],"text":s["text"]} for s in segs])
            for b in beats]
    _rset(rid, rows=rows)
    return jsonify({"rows": rows})

@app.route("/api/recut/generate", methods=["POST"])
def recut_generate():
    d = request.get_json(force=True)
    rid = d.get("rid")
    if rid not in RECUT: return jsonify({"error":"unknown rid"}), 404
    segs = d.get("segments") or RECUT[rid]["segments"]
    rows = d.get("rows") or RECUT[rid]["rows"]
    _rset(rid, segments=segs, rows=rows, captions=bool(d.get("captions", True)),
          status="generating", progress=0, log=[])
    threading.Thread(target=_recut_render, args=(rid, segs, rows),
                     daemon=True).start()
    return jsonify({"ok": True})

@app.route("/api/recut/video/<rid>")
def recut_video(rid):
    with RECUT_LOCK: r = RECUT.get(rid, {})
    out = r.get("output")
    if not out or not Path(out).exists(): return "not found", 404
    return send_file(out, mimetype="video/mp4")

@app.route("/recut")
def recut_page(): return RECUT_HTML


if __name__ == "__main__":
    port = int(os.environ.get("PORT_V2", 7861))
    print(f"\n  Penny Studio  →  http://localhost:{port}\n"
          f"    Dub    →  http://localhost:{port}/dub\n"
          f"    Recut  →  http://localhost:{port}/recut\n")
    app.run(host="0.0.0.0", port=port, debug=False)
