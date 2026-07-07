#!/usr/bin/env python3
"""
Talking-Head Recut engine — HyperFramers-style, per-frame GSAP render.

Pipeline (the HyperFramers "frame adapter" pattern):
  1. ffmpeg extracts the source video → face frames  (frames/f%05d.jpg)
  2. ONE HTML page holds:
       - #face-wrapper (clip window whose geometry GSAP animates
         between FULL / SIDE / HERO modes)  containing #face-img
         (its src is swapped to the current frame each tick)
       - a graphics stage (left area) where per-beat template cards animate
       - one master GSAP timeline, paused, registered on window.__tl
  3. Python drives playwright: for each output frame i →
       set #face-img.src = frames[i], await decode,
       seek __tl to i/total, screenshot → out/f%05d.png
  4. ffmpeg assembles out frames + muxes the original audio.

Everything (face transform + graphics) renders in the same page at the same
instant, so mode transitions and animations stay perfectly in sync.
"""
import os, re, json, math, shutil, subprocess, urllib.request


# ── LLM beat planner ──────────────────────────────────────────────────
_VALID_TPL = {"label", "keyword", "bullets", "bigStat", "quote", "compare", "highlight"}
_VALID_FACE = {"full", "side", "hero"}


def _clean_txt(t):
    return re.sub(r'[*#`]+', '', str(t or '')).strip()


def _llm_url_key(cfg):
    if cfg.get("groq_key"):
        return "https://api.groq.com/openai/v1/chat/completions", cfg["groq_key"]
    return (cfg.get("kyma_base", "https://api.kymaapi.com") + "/v1/chat/completions",
            cfg.get("kyma_key", ""))


def correct_transcript(cfg, raw_segs, log=print):
    """
    AI rewrites Whisper transcription errors while KEEPING every segment's
    start/end and full Vietnamese diacritics. Fixes misheard tech terms
    (NotebookLM, TechCrunch, MCP, GitHub…) and obvious typos only.
    Returns [{start, end, text}] (same count/timing) — falls back to raw on error.
    """
    if not raw_segs:
        return []
    segs = [{"i": i, "tx": (s.get("text", "") or s.get("vi", "")).strip()}
            for i, s in enumerate(raw_segs)]
    prompt = (
        "Đây là transcript tiếng Việt từ Whisper STT (có thể nghe sai từ).\n"
        "Sửa lại cho ĐÚNG chính tả và thuật ngữ, GIỮ NGUYÊN số dòng và thứ tự.\n\n"
        f"Dòng:\n{json.dumps(segs, ensure_ascii=False)}\n\n"
        'Trả JSON: {"segments":[{"i":0,"tx":"câu đã sửa"}, ...]}\n'
        "Quy tắc:\n"
        "- GIỮ ĐỦ dấu tiếng Việt (á, à, ả, ã, ạ, ê, ô, ơ, ư, đ...). TUYỆT ĐỐI không bỏ dấu.\n"
        "- Chỉ sửa từ sai/nghe nhầm; giữ nguyên ý và văn nói tự nhiên.\n"
        "- Sửa thuật ngữ: 'notebook LAM/LM'→'NotebookLM', 'techcruch'→'TechCrunch', "
        "'MCP','GitHub','YouTube','repo','cloud' viết chuẩn.\n"
        "- KHÔNG thêm/bớt/gộp dòng, KHÔNG bịa nội dung mới.\n"
        "- Đúng số dòng đầu vào. CHỈ JSON thuần."
    )
    url, key = _llm_url_key(cfg)
    body = {"model": cfg.get("correct_model", "llama-3.3-70b-versatile"),
            "temperature": 0.1, "response_format": {"type": "json_object"},
            "messages": [{"role": "user", "content": prompt}]}
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + key,
                 "Content-Type": "application/json", "User-Agent": "kyma-dub/2.0"})
    try:
        resp = json.load(urllib.request.urlopen(req, timeout=60))
        raw = resp["choices"][0]["message"]["content"]
        if raw.startswith("```"):
            raw = raw.split("```")[1].lstrip("json").strip()
        fixed = {int(x["i"]): _clean_txt(x.get("tx", ""))
                 for x in json.loads(raw).get("segments", [])
                 if "i" in x and str(x.get("tx", "")).strip()}
        out = []
        for i, s in enumerate(raw_segs):
            out.append({"start": float(s.get("start", 0)),
                        "end": float(s.get("end", 0)),
                        "text": fixed.get(i, (s.get("text", "") or s.get("vi", "")).strip())})
        log(f"[recut] transcript corrected ({len(fixed)}/{len(raw_segs)} dòng)")
        return out
    except Exception as ex:
        log(f"[recut] correct_transcript failed: {ex}")
        return [{"start": float(s.get("start", 0)), "end": float(s.get("end", 0)),
                 "text": (s.get("text", "") or s.get("vi", "")).strip()} for s in raw_segs]


# ── scene templates (one clean template per beat) ────────────────────
_SCENE = {"concept", "minimal", "list", "flow", "slide", "stat", "compare"}
_VALID_ICONS = {"notebook", "cloud", "code", "youtube", "search", "chart",
                "link", "doc", "video", "gear", "rocket", "bulb"}
_ICON_ALIAS = {"github": "code", "file": "doc", "list": "doc", "play": "video",
               "magnifier": "search", "brain": "bulb", "idea": "bulb",
               "database": "chart", "graph": "chart", "note": "notebook"}


def _icon_ok(name):
    name = (name or "").strip().lower()
    name = _ICON_ALIAS.get(name, name)
    return name if name in _VALID_ICONS else ""


def _valid_scene(raw):
    """Validate one scene decision → beat-content dict, or None (= no graphic)."""
    if not isinstance(raw, dict):
        return None
    t = raw.get("template")
    if t not in _SCENE:
        return None
    b = {"template": t}
    if t == "concept":
        b["title"] = _clean_txt(raw.get("title"))
        if not b["title"]:
            return None
        b["eyebrow"] = _clean_txt(raw.get("eyebrow"))
        b["sub"] = _clean_txt(raw.get("sub"))
        b["icon"] = _icon_ok(raw.get("icon"))
    elif t == "minimal":
        b["title"] = _clean_txt(raw.get("title"))
        if not b["title"]:
            return None
        b["icon"] = _icon_ok(raw.get("icon"))
        b["sub"] = _clean_txt(raw.get("sub"))
        b["sub_icon"] = _icon_ok(raw.get("sub_icon"))
    elif t == "list":
        b["title"] = _clean_txt(raw.get("title"))
        items = []
        for it in (raw.get("items") or []):
            if isinstance(it, dict):
                txt, ic = _clean_txt(it.get("text") or it.get("title")), _icon_ok(it.get("icon"))
            else:
                txt, ic = _clean_txt(it), ""
            if txt:
                items.append({"text": txt, "icon": ic})
        b["items"] = items[:4]
        if len(b["items"]) < 2:
            return None
    elif t == "flow":
        steps = []
        for s in (raw.get("steps") or []):
            v = _clean_txt(s.get("text") or s.get("title") if isinstance(s, dict) else s)
            if v:
                steps.append(v)
        steps = steps[:3]
        if len(steps) < 2:
            return None
        b["steps"] = steps
        b["label"] = _clean_txt(raw.get("label"))
    elif t == "slide":
        b["title"] = _clean_txt(raw.get("title"))
        if not b["title"]:
            return None
        its = []
        for s in (raw.get("items") or []):
            v = _clean_txt(s.get("text") or s.get("title") if isinstance(s, dict) else s)
            if v:
                its.append(v)
        b["items"] = its[:4]
    elif t == "stat":
        b["value"] = _clean_txt(raw.get("value"))
        if not b["value"] or not re.search(r"\d", b["value"]):
            return None
        b["label"] = _clean_txt(raw.get("label") or raw.get("title"))
    elif t == "compare":
        b["a"], b["b"] = _clean_txt(raw.get("a")), _clean_txt(raw.get("b"))
        if not (b["a"] and b["b"]):
            return None
    return b


def scene_face(t):
    """Face-wrapper mode implied by the scene template."""
    if t in ("minimal", "flow", "compare"):
        return "full"
    if t == "slide":
        return "hero"
    return "side"   # concept, list, stat


# ── storyboard row <-> beat (editable UI) ────────────────────────────
def beat_to_row(beat, segs):
    text = " ".join(s.get("text", "") for s in segs
                    if float(s.get("start", 0)) >= beat["t0"] - 0.05
                    and float(s.get("end", 0)) <= beat["t1"] + 0.6).strip()
    return {"t0": beat["t0"], "t1": beat["t1"], "text": text,
            "template": beat.get("template", ""),
            "eyebrow": beat.get("eyebrow", ""), "title": beat.get("title", ""),
            "sub": beat.get("sub", ""), "icon": beat.get("icon", ""),
            "sub_icon": beat.get("sub_icon", ""),
            "value": beat.get("value", ""), "label": beat.get("label", ""),
            "a": beat.get("a", ""), "b": beat.get("b", ""),
            "steps": list(beat.get("steps", [])),
            "items": [(it if isinstance(it, dict) else {"text": it, "icon": ""})
                      for it in beat.get("items", [])]}


def row_to_beat(r):
    raw = {"template": r.get("template", ""),
           "eyebrow": r.get("eyebrow"), "title": r.get("title"),
           "sub": r.get("sub"), "icon": r.get("icon"), "sub_icon": r.get("sub_icon"),
           "value": r.get("value"), "label": r.get("label"),
           "a": r.get("a"), "b": r.get("b"),
           "steps": r.get("steps") or [], "items": r.get("items") or []}
    beat = _valid_scene(raw) or {"template": ""}
    beat["t0"], beat["t1"] = float(r["t0"]), float(r["t1"])
    return beat


def analyze_beats(cfg, raw_segs, log=print):
    """One LLM call → per-segment scene-template plan (one clean template each)."""
    if not raw_segs:
        return []
    total = max(float(s.get("end", 0)) for s in raw_segs)
    segs = [{"i": i, "t0": round(float(s.get("start", 0)), 1),
             "t1": round(float(s.get("end", 0)), 1),
             "tx": (_clean_txt(s.get("text", "") or s.get("vi", "")))[:140]}
            for i, s in enumerate(raw_segs)]

    prompt = (
        f"Bạn là NHÀ THIẾT KẾ motion-graphics cho video talking-head tiếng Việt. "
        f"Transcript từ Whisper (có thể nghe sai) chia thành {len(segs)} SEGMENT.\n"
        f"Với MỖI segment: chọn ĐÚNG 1 template minh hoạ hợp nội dung (hoặc none nếu câu đệm), "
        f"điền chữ MẠCH LẠC theo lời nói. Phong cách trắng-xanh, sạch, gọn.\n\n"
        f"Segments:\n{json.dumps(segs, ensure_ascii=False)}\n\n"
        f"Templates:\n"
        f"- concept: giới thiệu 1 khái niệm/công cụ. {{eyebrow(nhãn ngắn), title(tên ≤3 từ), sub(mô tả ≤5 từ), icon}}\n"
        f"- minimal: 1-2 từ khoá nổi gom giữa. {{title(≤3 từ), icon, sub(từ khoá 2, tuỳ chọn), sub_icon}}\n"
        f"- list: liệt kê 2-4 mục. {{title, items:[{{text ≤3 từ, icon}}]}}\n"
        f"- flow: luồng A→B(→C). {{steps:[2-3 pill ≤2 từ], label(nhãn mũi tên, vd 'kết nối')}}\n"
        f"- slide: điểm nhấn/tóm ý full màn. {{title, items:[2-4 bước ngắn]}}\n"
        f"- stat: con số nổi bật. {{value(có số vd '100+'), label}} — CHỈ khi câu có SỐ cụ thể\n"
        f"- compare: so sánh. {{a ≤3 từ, b ≤3 từ}}\n"
        f"- none: câu dẫn/đệm không có ý cụ thể → bỏ qua (không thêm vào beats)\n\n"
        f"icon hợp lệ: notebook cloud code youtube search chart link doc video gear rocket bulb\n\n"
        f'Trả JSON: {{"beats":[{{"i":idx, "template":"...", ...nội dung}}]}}\n\n'
        f"Quy tắc:\n"
        f"- Mật độ THEO nội dung câu: 1 ý→concept/minimal; kể nhiều→list; quan hệ/luồng→flow; có số→stat; so sánh→compare; chốt/tóm→slide\n"
        f"- Chữ là CỤM CÓ NGHĨA, KHÔNG tách từ (SAI: 'Tự'+'động'; ĐÚNG: 'Tự động')\n"
        f"- Sửa thuật ngữ Whisper: 'notebook LAM/LM'→'NotebookLM', 'techcruch'→'TechCrunch', MCP/GitHub/YouTube viết chuẩn\n"
        f"- Chọn icon hợp nội dung (YouTube→youtube, nghiên cứu→search, tài liệu/báo→doc, kết nối→link, dữ liệu→chart)\n"
        f"- Lấy ý từ lời nói, KHÔNG bịa số/tên. CHỈ JSON thuần"
    )

    url, key = _llm_url_key(cfg)
    body = {"model": cfg.get("planner_model", "llama-3.3-70b-versatile"),
            "temperature": 0.3, "response_format": {"type": "json_object"},
            "messages": [{"role": "user", "content": prompt}]}
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + key,
                 "Content-Type": "application/json", "User-Agent": "kyma-dub/2.0"})
    try:
        resp = json.load(urllib.request.urlopen(req, timeout=60))
        raw = resp["choices"][0]["message"]["content"]
        if raw.startswith("```"):
            raw = raw.split("```")[1].lstrip("json").strip()
        beats = _build_beats(json.loads(raw).get("beats", []), segs)
        ngfx = sum(1 for b in beats if b.get("template"))
        log(f"[recut] planner → {len(beats)} beats, {ngfx} cảnh có minh hoạ")
        return beats
    except Exception as ex:
        log(f"[recut] planner failed: {ex}")
        return []


def _build_beats(raw_beats, segs):
    """One scene per segment; merge consecutive identical scenes. Timing = segment."""
    n = len(segs)
    dec = [None] * n
    for b in raw_beats:
        if not isinstance(b, dict):
            continue
        try:
            i = int(b.get("i"))
        except (TypeError, ValueError):
            continue
        if 0 <= i < n:
            dec[i] = _valid_scene(b)
    beats, i = [], 0
    while i < n:
        d = dec[i]
        if not d:
            beats.append({"t0": round(segs[i]["t0"], 2), "t1": round(segs[i]["t1"], 2),
                          "template": ""})
            i += 1
            continue
        key = json.dumps(d, ensure_ascii=False, sort_keys=True)
        j = i + 1
        while j < n and dec[j] and json.dumps(dec[j], ensure_ascii=False, sort_keys=True) == key:
            j += 1
        beat = dict(d)
        beat["t0"], beat["t1"] = round(segs[i]["t0"], 2), round(segs[j - 1]["t1"], 2)
        beats.append(beat)
        i = j
    return beats


# ── ffprobe helpers ───────────────────────────────────────────────────
def _probe(video):
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,r_frame_rate",
         "-show_entries", "format=duration",
         "-of", "json", video]).decode()
    d = json.loads(out)
    st = d["streams"][0]
    num, den = st["r_frame_rate"].split("/")
    fps = float(num) / float(den or 1)
    return {"w": int(st["width"]), "h": int(st["height"]),
            "fps": fps, "dur": float(d["format"]["duration"])}


def _has_audio(video):
    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=index", "-of", "csv=p=0", video],
            stderr=subprocess.DEVNULL).decode().strip()
        return bool(out)
    except Exception:
        return False


# ── frame extraction ──────────────────────────────────────────────────
def _extract_face_frames(video, fdir, fps, cw, ch, trim=None):
    """Extract source → JPEG frames sized to the canvas (cover, centre-crop)."""
    os.makedirs(fdir, exist_ok=True)
    vf = (f"fps={fps},scale={cw}:{ch}:force_original_aspect_ratio=increase,"
          f"crop={cw}:{ch}")
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    if trim:
        cmd += ["-t", str(trim)]
    cmd += ["-i", video, "-vf", vf, "-q:v", "3",
            os.path.join(fdir, "f%05d.jpg")]
    subprocess.run(cmd, check=True)
    n = len([x for x in os.listdir(fdir) if x.endswith(".jpg")])
    return n


# ── face-mode geometry (canvas-relative fractions) ────────────────────
# Each mode = clip-window rect as fractions of canvas + corner radius (px).
def _mode_rects(cw, ch):
    return {
        # face fills the whole frame
        "full": {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0, "r": 0},
        # face cropped to a right-side panel; left ~58% freed for graphics
        "side": {"x": 0.585, "y": 0.067, "w": 0.378, "h": 0.866, "r": 18},
        # face shrinks to a small top-right PiP; graphics take over
        "hero": {"x": 0.772, "y": 0.050, "w": 0.200, "h": 0.400, "r": 14},
    }


# ── HTML page ─────────────────────────────────────────────────────────
_PAGE_CSS = """
* { margin:0; padding:0; box-sizing:border-box; }
html,body { overflow:hidden; }
#stage { position:relative; overflow:hidden;
         font-family:-apple-system,'Segoe UI',Arial,sans-serif; }
#bg { position:absolute; inset:0; z-index:0;
      background:radial-gradient(1200px 700px at 22% 12%, #ffffff 0%, #eef4ff 42%, #e3edfb 100%); }
#bg::before { content:""; position:absolute; left:-8%; top:6%; width:46%; height:62%;
      border-radius:50%; filter:blur(8px);
      background:radial-gradient(circle, rgba(59,130,246,.16), transparent 70%); }
#bg::after { content:""; position:absolute; right:5%; bottom:-8%; width:42%; height:54%;
      border-radius:50%;
      background:radial-gradient(circle, rgba(139,92,246,.10), transparent 70%); }
#face-wrapper { position:absolute; left:0; top:0; overflow:hidden; z-index:2;
      will-change:left,top,width,height; }
#face-img { display:block; width:100%; height:100%; object-fit:cover; object-position:center center; }
#gfx { position:absolute; inset:0; z-index:3; pointer-events:none; }

.slot { position:absolute; display:flex; flex-direction:column;
        justify-content:center; align-items:flex-start; }
.colstack { position:absolute; display:flex; flex-direction:column;
        justify-content:center; align-items:stretch; gap:16px; }
.blockwrap { display:inline-flex; flex-direction:column; align-items:flex-start;
        max-width:100%; overflow:hidden; position:relative; }
.shimmer { position:absolute; inset:0; pointer-events:none; transform:translateX(-130%);
        background:linear-gradient(105deg, transparent 42%, rgba(255,255,255,.55) 50%, transparent 58%); }
.card .title { will-change:clip-path; }
.blockwrap.panel { background:rgba(255,255,255,.96); border-radius:16px; padding:16px 20px;
        box-shadow:0 12px 40px rgba(15,23,42,.34); border:1px solid rgba(226,232,240,.9); }
.card { color:#0f172a; max-width:100%; overflow-wrap:anywhere; word-break:break-word; }
.statval { line-height:.92; }
.eyebrow { font-size:22px; letter-spacing:.16em; text-transform:uppercase;
           color:#2563eb; font-weight:800; margin-bottom:12px; }
.title { font-weight:800; line-height:1.14; color:#0f172a; }
.sub { color:#475569; font-weight:500; margin-top:12px; line-height:1.4; }
.accentbar { width:64px; height:6px; border-radius:99px;
             background:linear-gradient(90deg,#2563eb,#60a5fa); margin-top:20px;
             transform-origin:left center; }
.statval { font-weight:800; color:#2563eb; line-height:.9;
           text-shadow:0 10px 46px rgba(37,99,235,.30); }
.brow { display:flex; align-items:center; gap:12px; margin:10px 0;
        font-size:22px; color:#0f172a; font-weight:600; }
.chk { width:30px; height:30px; border-radius:50%; background:#2563eb; color:#fff;
       display:flex; align-items:center; justify-content:center; font-size:17px;
       flex-shrink:0; box-shadow:0 5px 14px rgba(37,99,235,.38); }
.qmark { color:#93c5fd; line-height:.5; font-weight:800; margin-bottom:4px; }
.chip { padding:12px 22px; border-radius:13px; font-weight:800; font-size:24px; }
.chipA { background:#fff; color:#1e3a8a; border:2px solid #bfdbfe;
         box-shadow:0 8px 22px rgba(15,23,42,.10); }
.chipB { background:linear-gradient(135deg,#2563eb,#3b82f6); color:#fff;
         box-shadow:0 10px 26px rgba(37,99,235,.40); }
.vs { font-size:24px; font-weight:800; color:#94a3b8; margin:14px 0; letter-spacing:.1em; }
.hterm { font-weight:800; position:relative; display:inline-block; color:#0f172a; }
.hunder { position:absolute; left:-4px; right:-4px; bottom:6px; height:16px;
          background:rgba(59,130,246,.38); z-index:-1; border-radius:5px;
          transform:scaleX(0); transform-origin:left center; }

/* ── full-face diagram: pills, icons, connectors ────────────────────── */
.slotel { position:absolute; display:flex; flex-direction:column; gap:10px;
          align-items:flex-start; }
.pill { display:inline-flex; align-items:center; gap:12px; padding:14px 26px;
        border-radius:999px; font-weight:800; font-size:32px; white-space:nowrap;
        box-shadow:0 10px 28px rgba(15,23,42,.20); }
.pill.solid   { background:linear-gradient(135deg,#2563eb,#3b82f6); color:#fff; }
.pill.outline { background:#fff; color:#1e3a8a; border:2.5px solid #bfdbfe; }
.pill.ghost   { background:rgba(255,255,255,.96); color:#0f172a;
                border:1px solid rgba(226,232,240,.9); }
.pill .ic { width:32px; height:32px; flex-shrink:0; display:flex; }
.pill.solid .ic { color:#fff; } .pill.outline .ic, .pill.ghost .ic { color:#2563eb; }
.pill .ic svg { width:100%; height:100%; }
.iconel { display:flex; flex-direction:column; align-items:center; gap:8px; }
.iconbox { width:74px; height:74px; color:#2563eb;
           filter:drop-shadow(0 8px 18px rgba(37,99,235,.28)); }
.iconbox svg { width:100%; height:100%; }
.iclabel { font-size:19px; font-weight:700; color:#0f172a; text-align:center; }
.fpanel { background:rgba(255,255,255,.96); border-radius:16px; padding:16px 22px;
          box-shadow:0 12px 36px rgba(15,23,42,.28); border:1px solid rgba(226,232,240,.9); }
#links { position:absolute; inset:0; pointer-events:none; overflow:visible; }
.lk-line { stroke:#3b82f6; stroke-width:3; fill:none; stroke-linecap:round; }
.lk-lbl { fill:#475569; font-size:20px; font-weight:700;
          font-family:-apple-system,'Segoe UI',Arial,sans-serif;
          stroke:#fff; stroke-width:5px; paint-order:stroke; }

/* ── clean scene templates ──────────────────────────────────────────── */
.scene { position:absolute; inset:0; z-index:3; }
.sc-left { position:absolute; left:5.5%; top:50%; transform:translateY(-50%); max-width:52%; }
.sc-leftmid { position:absolute; left:6%; top:50%; transform:translateY(-50%);
              display:flex; flex-direction:column; align-items:center; gap:10px; }
.sc-top { position:absolute; left:0; right:0; top:13%;
          display:flex; flex-direction:column; align-items:center; gap:18px; }
.sc-slide { position:absolute; left:7%; top:20%; max-width:72%; }
.eyebrow { font-size:22px; font-weight:800; letter-spacing:.14em; color:#2563eb; margin-bottom:10px; }
.ctitle { display:flex; align-items:center; gap:16px; font-size:54px; font-weight:800;
          color:#0f172a; line-height:1.08; }
.ctitle .ci { width:52px; height:52px; color:#2563eb; flex-shrink:0; }
.ctitle .ci svg { width:100%; height:100%; }
.csub { font-size:26px; color:#475569; margin-top:10px; }
.accentbar { width:64px; height:6px; border-radius:999px; background:#2563eb;
             margin-top:20px; transform-origin:left center; }
.pill.big { font-size:36px; padding:16px 30px; }
.lt { font-size:38px; font-weight:800; color:#0f172a; margin-bottom:18px; }
.lrow { display:flex; align-items:center; gap:16px; font-size:30px; font-weight:600;
        color:#0f172a; margin:12px 0; }
.lrow .li { width:38px; height:38px; color:#2563eb; flex-shrink:0; }
.lrow .li svg { width:100%; height:100%; }
.lrow .li.dot { width:14px; height:14px; border-radius:50%; background:#2563eb; }
.s-flow { display:flex; align-items:center; gap:18px; }
.fl-arrow { display:flex; flex-direction:column; align-items:center; }
.fl-lbl { font-size:20px; color:#475569; font-weight:700; }
.fl-ar { width:46px; height:30px; color:#2563eb; }
.fl-ar svg { width:100%; height:100%; }
.stitle { font-size:50px; font-weight:800; color:#0f172a; line-height:1.12; }
.slist { display:flex; flex-direction:column; gap:14px; margin-top:20px; }
.srow { display:flex; align-items:center; gap:14px; font-size:28px; color:#0f172a; font-weight:600; }
.snum { width:44px; height:44px; border-radius:50%; background:#2563eb; color:#fff;
        display:flex; align-items:center; justify-content:center; font-size:22px;
        font-weight:800; flex-shrink:0; }
.slabel { font-size:26px; color:#0f172a; font-weight:600; margin-top:8px; }
"""

_PAGE_JS = r"""
const CFG = window.__CFG;
const CW = CFG.cw, CH = CFG.ch, DUR = CFG.dur;
const BEATS = CFG.beats || [];
const M = CFG.modes, FRAMES = CFG.frames;
const MODE_DUR = 0.38;

// size the stage to the canvas
for (const el of [document.documentElement, document.body]) {
  el.style.width = CW+'px'; el.style.height = CH+'px';
}
const stage = document.getElementById('stage');
stage.style.width = CW+'px'; stage.style.height = CH+'px';

const img = document.getElementById('face-img');
const fw  = document.getElementById('face-wrapper');
const gfx = document.getElementById('gfx');

function setRect(el, m) {
  el.style.left = m.x+'px'; el.style.top = m.y+'px';
  el.style.width = m.w+'px'; el.style.height = m.h+'px';
  el.style.borderRadius = m.r+'px';
  el.style.boxShadow = m.r>0 ? '0 24px 60px rgba(15,23,42,.28)' : 'none';
}

const tl = gsap.timeline({ paused: true });

// ── face-mode timeline (mode implied by scene template) ─────────────
function faceOf(t){
  if(t==='minimal'||t==='flow'||t==='compare'||!t) return 'full';
  if(t==='slide') return 'hero';
  return 'side';
}
const firstMode = faceOf(BEATS[0] && BEATS[0].template);
setRect(fw, M[firstMode]);
let cur = firstMode;
for (const b of BEATS) {
  const mode = faceOf(b.template);
  if (mode !== cur) {
    const m = M[mode];
    tl.to(fw, { left:m.x, top:m.y, width:m.w, height:m.h, borderRadius:m.r,
                boxShadow: m.r>0 ? '0 24px 60px rgba(15,23,42,.28)' : '0px 0px 0px rgba(0,0,0,0)',
                duration:MODE_DUR, ease:'expo.inOut' },
           Math.max(0, b.t0 - 0.12));
    cur = mode;
  }
}

const esc = (s) => String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');

const ICONS = {
  notebook:'<rect x="5" y="3" width="14" height="18" rx="2"/><line x1="9" y1="3" x2="9" y2="21"/><line x1="12" y1="8" x2="16" y2="8"/><line x1="12" y1="12" x2="16" y2="12"/>',
  cloud:'<path d="M6 16a4 4 0 010-8 5 5 0 019.6-1.5A3.5 3.5 0 0117 16z"/>',
  code:'<polyline points="8 6 3 12 8 18"/><polyline points="16 6 21 12 16 18"/>',
  youtube:'<rect x="3" y="6" width="18" height="12" rx="3"/><polygon points="11 9 15 12 11 15" fill="currentColor" stroke="none"/>',
  search:'<circle cx="11" cy="11" r="7"/><line x1="16.5" y1="16.5" x2="21" y2="21"/>',
  chart:'<line x1="4" y1="20" x2="4" y2="13" stroke-width="2.5"/><line x1="10" y1="20" x2="10" y2="5" stroke-width="2.5"/><line x1="16" y1="20" x2="16" y2="15" stroke-width="2.5"/>',
  link:'<path d="M10 14l4-4"/><path d="M13 6l1-1a4 4 0 016 6l-1 1"/><path d="M11 18l-1 1a4 4 0 01-6-6l1-1"/>',
  doc:'<path d="M6 3h8l4 4v14H6z"/><polyline points="14 3 14 7 18 7"/><line x1="9" y1="12" x2="15" y2="12"/><line x1="9" y1="16" x2="15" y2="16"/>',
  video:'<rect x="3" y="6" width="13" height="12" rx="2"/><polygon points="16 10 21 8 21 16 16 14" fill="currentColor" stroke="none"/>',
  gear:'<circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M5 5l2 2M17 17l2 2M19 5l-2 2M7 17l-2 2"/>',
  rocket:'<path d="M12 3c3 2 5 6 5 10l-5 3-5-3c0-4 2-8 5-10z"/><circle cx="12" cy="10" r="1.6"/><path d="M9 17l-2 3M15 17l2 3"/>',
  bulb:'<path d="M9.5 18h5M10.5 21h3"/><path d="M12 3a6 6 0 00-4 10c1 1 1 2 1 3h6c0-1 0-2 1-3a6 6 0 00-4-10z"/>'
};
function iconSVG(name){
  const p = ICONS[name]; if(!p) return '';
  return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${p}</svg>`;
}
function arrowSVG(){
  return `<svg viewBox="0 0 24 14" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><line x1="2" y1="7" x2="20" y2="7"/><polyline points="15 2 21 7 15 12"/></svg>`;
}
const ci = (name,cls)=> name ? `<span class="${cls||'ci'}">${iconSVG(name)}</span>` : '';

// ── render one clean scene template ─────────────────────────────────
function renderScene(b){
  const t = b.template; if(!t) return;
  const t0 = b.t0, t1 = Math.max(b.t0+0.9, b.t1), exit = Math.max(t0+0.7, t1-0.3);
  const sc = document.createElement('div'); sc.className='scene'; gfx.appendChild(sc);
  let items = [];

  if(t==='concept'){
    sc.innerHTML = `<div class="sc-left">`
      + (b.eyebrow?`<div class="eyebrow">${esc(b.eyebrow)}</div>`:'')
      + `<div class="ctitle">${ci(b.icon,'ci')}<span class="title">${esc(b.title)}</span></div>`
      + (b.sub?`<div class="csub">${esc(b.sub)}</div>`:'')
      + `<div class="accentbar"></div></div>`;
  } else if(t==='minimal'){
    sc.innerHTML = `<div class="sc-top">`
      + `<span class="pill solid big it">${ci(b.icon,'ic')}<span>${esc(b.title)}</span></span>`
      + (b.sub?`<span class="pill ghost it">${ci(b.sub_icon,'ic')}<span>${esc(b.sub)}</span></span>`:'')
      + `</div>`;
    items = [...sc.querySelectorAll('.it')];
  } else if(t==='list'){
    const rows = (b.items||[]).map(it =>
      `<div class="lrow it">${it.icon?ci(it.icon,'li'):'<span class="li dot"></span>'}<span>${esc(it.text)}</span></div>`).join('');
    sc.innerHTML = `<div class="sc-left"><div class="lt title">${esc(b.title)}</div>${rows}</div>`;
    items = [...sc.querySelectorAll('.lrow')];
  } else if(t==='flow'){
    const parts = [];
    (b.steps||[]).forEach((s,k)=>{
      if(k>0) parts.push(`<span class="fl-arrow it">`+(b.label?`<span class="fl-lbl">${esc(b.label)}</span>`:'')+`<span class="fl-ar">${arrowSVG()}</span></span>`);
      parts.push(`<span class="pill ${k%2?'solid':'outline'} it">${esc(s)}</span>`);
    });
    sc.innerHTML = `<div class="sc-top"><div class="s-flow">${parts.join('')}</div></div>`;
    items = [...sc.querySelectorAll('.it')];
  } else if(t==='slide'){
    const rows = (b.items||[]).map((s,k)=>`<div class="srow it"><span class="snum">${k+1}</span><span>${esc(s)}</span></div>`).join('');
    sc.innerHTML = `<div class="sc-slide"><div class="stitle title">${esc(b.title)}</div><div class="slist">${rows}</div></div>`;
    items = [...sc.querySelectorAll('.srow')];
  } else if(t==='stat'){
    const v = String(b.value||''); const vs = v.length<=3?150:v.length<=5?112:74;
    sc.innerHTML = `<div class="sc-left"><div class="statval" style="font-size:${vs}px">${esc(b.value)}</div>`
      + (b.label?`<div class="slabel">${esc(b.label)}</div>`:'')+`</div>`;
  } else if(t==='compare'){
    sc.innerHTML = `<div class="sc-leftmid"><span class="chip chipA it">${esc(b.a)}</span>`
      + `<span class="vs it">VS</span><span class="chip chipB it">${esc(b.b)}</span></div>`;
    items = [...sc.querySelectorAll('.it')];
  }

  // animations
  tl.fromTo(sc, {opacity:0}, {opacity:1, duration:0.3}, t0);
  const title = sc.querySelector('.title');
  if(title) tl.fromTo(title, {clipPath:'inset(0 100% 0 0)'}, {clipPath:'inset(0 0% 0 0)', duration:0.5, ease:'power3.out'}, t0+0.1);
  const ab = sc.querySelector('.accentbar');
  if(ab) tl.fromTo(ab, {scaleX:0}, {scaleX:1, duration:0.4, ease:'power2.out'}, t0+0.3);
  const sv = sc.querySelector('.statval');
  if(sv){
    tl.fromTo(sv, {scale:0.6, opacity:0}, {scale:1, opacity:1, duration:0.45, ease:'back.out(2)'}, t0+0.05);
    const m = String(b.value||'').match(/^(\d+)(.*)$/);
    if(m){ const tg=parseInt(m[1],10), suf=m[2]||'', px={v:0};
      tl.to(px, {v:tg, duration:0.55, ease:'power2.out', snap:{v:1}, onUpdate:()=>{sv.textContent=Math.round(px.v)+suf;}}, t0+0.12); }
  }
  items.forEach((it,k)=> tl.fromTo(it, {opacity:0, y:18, scale:0.94}, {opacity:1, y:0, scale:1, duration:0.4, ease:'back.out(1.5)'}, t0+0.25+k*0.28));
  tl.to(sc, {opacity:0, y:-10, duration:0.3, ease:'power2.in'}, exit);
}

BEATS.forEach(b => { if(b.template) renderScene(b); });

tl.set({}, {}, DUR);
window.__tl = tl;

window.__setFace = function(idx) {
  const n = String(idx).padStart(5,'0');
  return new Promise((res) => {
    img.onload = () => res(true);
    img.onerror = () => res(false);
    img.src = FRAMES + n + '.jpg';
  });
};
window.__seek = function(t) { tl.progress(Math.max(0, Math.min(1, t/DUR))); };
"""


def build_page(cw, ch, total_dur, beats, frames_uri):
    """
    Single render page: white/blue clean bg + face-mode timeline + graphics.
    beats: [{t0,t1,face,template,...content}]  face ∈ full|side|hero
    """
    rects = _mode_rects(cw, ch)
    px = {k: {"x": round(v["x"]*cw), "y": round(v["y"]*ch),
              "w": round(v["w"]*cw), "h": round(v["h"]*ch), "r": v["r"]}
          for k, v in rects.items()}
    cfg = {"cw": cw, "ch": ch, "dur": total_dur,
           "beats": beats or [], "modes": px, "frames": frames_uri}
    return (
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        '<script src="https://cdn.jsdelivr.net/npm/gsap@3.12.5/dist/gsap.min.js"></script>'
        '<style>' + _PAGE_CSS + '</style></head><body>'
        '<div id="stage"><div id="bg"></div>'
        '<div id="face-wrapper"><img id="face-img" src="" alt=""></div>'
        '<div id="gfx"></div></div>'
        '<script>window.__CFG=' + json.dumps(cfg) + ';</script>'
        '<script>' + _PAGE_JS + '</script>'
        '</body></html>'
    )


# ── main render ───────────────────────────────────────────────────────
def render_recut(video, beats, workdir, out_video,
                 srt_path=None, fps=25, canvas=(1280, 720), trim=None,
                 captions=True, log=print):
    """Render the recut. captions: burn subtitles at the bottom (small)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log("[recut] playwright not installed")
        return False

    cw, ch = canvas
    info = _probe(video)
    dur = min(info["dur"], trim) if trim else info["dur"]

    fdir = os.path.join(workdir, "frames")
    odir = os.path.join(workdir, "out")
    os.makedirs(odir, exist_ok=True)

    log(f"[recut] extracting face frames @ {fps}fps {cw}x{ch}…")
    n_frames = _extract_face_frames(video, fdir, fps, cw, ch, trim=trim)
    log(f"[recut] {n_frames} frames extracted")

    frames_uri = "file://" + os.path.join(fdir, "f")
    html = build_page(cw, ch, dur, beats or [], frames_uri)
    page_path = os.path.join(workdir, "page.html")
    with open(page_path, "w", encoding="utf-8") as f:
        f.write(html)

    log("[recut] rendering frames via playwright…")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": cw, "height": ch},
                                device_scale_factor=1)
        page.goto("file://" + page_path, wait_until="networkidle")

        for i in range(1, n_frames + 1):
            t = (i - 1) / fps
            page.evaluate("(idx) => window.__setFace(idx)", i)
            page.evaluate("(t) => window.__seek(t)", t)
            page.screenshot(path=os.path.join(odir, f"f{i:05d}.png"))
            if i % 25 == 0 or i == n_frames:
                log(f"[recut]   frame {i}/{n_frames}")

        browser.close()

    log("[recut] assembling video…")
    burn = bool(captions and srt_path and os.path.exists(srt_path))
    assembled = os.path.join(workdir, "assembled.mp4") if burn else out_video
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-framerate", str(fps), "-i", os.path.join(odir, "f%05d.png")]
    if _has_audio(video):
        av = ["-t", str(dur)] if trim else []
        cmd += av + ["-i", video, "-map", "0:v", "-map", "1:a:0", "-c:a", "aac"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-pix_fmt", "yuv420p", "-shortest", assembled]
    subprocess.run(cmd, check=True)

    if burn:
        log("[recut] burning captions…")
        # small, not bold, solid black box at 90% opacity.
        # BorderStyle=3 (opaque box) needs Outline>0 for the box to have padding —
        # Outline=0 renders NO box at all.
        fs_size = max(13, int(min(cw, ch) * 0.020))
        mv = max(18, int(ch * 0.040))
        outline = max(2, int(fs_size * 0.16))   # tight box hugging the text
        # BackColour alpha &H33 = 51 → ~80% opaque black
        style = (f"FontName=Arial,FontSize={fs_size},PrimaryColour=&H00FFFFFF,"
                 f"BackColour=&H33000000,BorderStyle=3,Outline={outline},Shadow=0,"
                 f"MarginV={mv},Alignment=2,Bold=0")
        safe = srt_path.replace("'", "\\'")
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-i", assembled,
             "-vf", f"subtitles='{safe}':force_style='{style}'",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
             "-c:a", "copy", out_video], check=True)

    log(f"[recut] done → {out_video}")
    return True
