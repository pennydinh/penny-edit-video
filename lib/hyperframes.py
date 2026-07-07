#!/usr/bin/env python3
"""
HyperFramers v2 — HTML-rendered cards overlaid on video.

Cards are rendered as HTML/CSS via playwright (headless Chromium), then
composited onto video using ffmpeg. Falls back to PIL if playwright is
unavailable.
"""
import os, io, html as _html, json, subprocess, tempfile, urllib.request
from PIL import Image, ImageDraw, ImageFont

# Try to import playwright — available after `playwright install chromium`
try:
    from playwright.sync_api import sync_playwright as _sync_playwright
    _PLAYWRIGHT_OK = True
except ImportError:
    _PLAYWRIGHT_OK = False

# ── card rendering ────────────────────────────────────────────────────

def _fonts(scale):
    title_pt = max(14, int(28 * scale))
    sub_pt   = max(10, int(18 * scale))
    for bp, rp in [
        ("/System/Library/Fonts/Supplemental/Arial Bold.ttf",
         "/System/Library/Fonts/Supplemental/Arial.ttf"),
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]:
        if os.path.exists(bp) and os.path.exists(rp):
            return ImageFont.truetype(bp, title_pt), ImageFont.truetype(rp, sub_pt)
    d = ImageFont.load_default()
    return d, d


def make_card(title, subtitle, scale=1.0):
    """White rounded-rect card with drop shadow → RGBA PIL Image."""
    sh_off = max(3, int(5 * scale))   # shadow offset (right + down)
    px, py = int(28*scale), int(20*scale)
    r, gap = int(14*scale), int(7*scale)
    ft, fs = _fonts(scale)
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    tb = probe.textbbox((0,0), title,    font=ft)
    sb = probe.textbbox((0,0), subtitle, font=fs) if subtitle else (0,0,0,0)
    tw, th = tb[2]-tb[0], tb[3]-tb[1]
    sw, sh = sb[2]-sb[0], sb[3]-sb[1]
    cw = max(tw, sw, int(100*scale)) + px*2
    ch = th + (sh + gap if subtitle else 0) + py*2
    # Canvas includes space for shadow
    img = Image.new("RGBA", (cw+4+sh_off, ch+4+sh_off), (0,0,0,0))
    d = ImageDraw.Draw(img)
    # Drop shadow (semi-transparent dark rect, offset)
    d.rounded_rectangle([2+sh_off, 2+sh_off, cw+2+sh_off, ch+2+sh_off],
                        radius=r, fill=(0, 0, 0, 90))
    # White card with dark visible border
    d.rounded_rectangle([2, 2, cw+2, ch+2], radius=r,
                        fill=(255,255,255,248), outline=(50,50,50,210), width=2)
    # Left accent stripe (blue)
    stripe = max(3, int(5*scale))
    d.rectangle([2, r, 2+stripe, ch+2-r], fill=(37, 99, 235, 230))
    d.text((px+2+stripe//2, py+2), title, font=ft, fill=(18,18,18,255))
    if subtitle:
        d.text((px+2+stripe//2, py+th+gap+2), subtitle, font=fs, fill=(70,70,70,230))
    return img


# ── HTML card renderer (HyperFramers-style) ──────────────────────────

def _card_html(title, subtitle, style, scale):
    """Return self-contained HTML string for a card."""
    t = _html.escape(title)
    s = _html.escape(subtitle) if subtitle else ""
    sub_el = f'<div class="sub">{s}</div>' if s else ""

    if style == "large":
        tp = max(16, int(24 * scale))
        sp = max(11, int(15 * scale))
        pv = max(14, int(18 * scale))
        ph = max(16, int(22 * scale))
        mw = max(200, int(300 * scale))
        r  = max(10, int(14 * scale))
        bar = max(3, int(4 * scale))
        gap = max(3, int(6 * scale))
        return f"""<!DOCTYPE html><html><head><style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:transparent;font-family:-apple-system,'Segoe UI',Arial,sans-serif}}
.card{{display:inline-block;background:linear-gradient(135deg,#1e293b 0%,#0f172a 100%);
  border-radius:{r}px;box-shadow:0 8px 32px rgba(0,0,0,.45),0 2px 8px rgba(0,0,0,.3);
  overflow:hidden;min-width:{mw}px;max-width:{mw*2}px}}
.bar{{height:{bar}px;background:linear-gradient(90deg,#3b82f6,#8b5cf6)}}
.inner{{padding:{pv}px {ph}px}}
.title{{font-size:{tp}px;font-weight:800;color:#f8fafc;line-height:1.25;word-break:break-word}}
.sub{{font-size:{sp}px;color:#94a3b8;margin-top:{gap}px;word-break:break-word}}
</style></head><body>
<div class="card" id="card"><div class="bar"></div><div class="inner">
<div class="title">{t}</div>{sub_el}</div></div></body></html>"""
    else:
        tp = max(12, int(17 * scale))
        sp = max(10, int(13 * scale))
        pv = max(9,  int(12 * scale))
        ph = max(12, int(16 * scale))
        mw = max(130, int(190 * scale))
        r  = max(7, int(10 * scale))
        sw = max(3, int(5 * scale))
        gap = max(2, int(4 * scale))
        return f"""<!DOCTYPE html><html><head><style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:transparent;font-family:-apple-system,'Segoe UI',Arial,sans-serif}}
.card{{display:inline-flex;align-items:stretch;background:rgba(255,255,255,.97);
  border-radius:{r}px;box-shadow:0 4px 18px rgba(0,0,0,.22),0 1px 4px rgba(0,0,0,.12);
  border:1.5px solid rgba(0,0,0,.09);overflow:hidden;min-width:{mw}px;max-width:{mw*2}px}}
.accent{{width:{sw}px;background:#2563eb;flex-shrink:0}}
.inner{{padding:{pv}px {ph}px}}
.title{{font-size:{tp}px;font-weight:700;color:#111827;line-height:1.25;word-break:break-word}}
.sub{{font-size:{sp}px;color:#4b5563;margin-top:{gap}px;word-break:break-word}}
</style></head><body>
<div class="card" id="card"><div class="accent"></div><div class="inner">
<div class="title">{t}</div>{sub_el}</div></div></body></html>"""


def _make_card_html(title, subtitle, style="small", scale=1.0) -> Image.Image:
    """Render card via playwright → PIL RGBA Image."""
    if not _PLAYWRIGHT_OK:
        raise RuntimeError("playwright not available")
    html_src = _card_html(title, subtitle, style, scale)
    with tempfile.NamedTemporaryFile(suffix=".html", mode="w",
                                     delete=False, encoding="utf-8") as f:
        f.write(html_src)
        tmp = f.name
    try:
        with _sync_playwright() as pw:
            browser = pw.chromium.launch(args=["--no-sandbox"])
            page = browser.new_page(device_scale_factor=2)
            page.goto(f"file://{tmp}", wait_until="domcontentloaded")
            el = page.query_selector("#card")
            png = el.screenshot(type="png", omit_background=True)
            browser.close()
    finally:
        os.unlink(tmp)
    img = Image.open(io.BytesIO(png)).convert("RGBA")
    return img


def _make_card_img(title, subtitle, style="small", scale=1.0) -> Image.Image:
    """Return card as PIL RGBA Image — HTML/playwright preferred, PIL fallback."""
    try:
        return _make_card_html(title, subtitle, style=style, scale=scale)
    except Exception as e:
        print(f"[hyperframes] HTML card failed ({type(e).__name__}: {e}), using PIL")
        return make_card(title, subtitle, scale=scale)


def _make_card_frames(title, subtitle, scale, workdir, idx,
                      fps=25, hold_dur=3.5, anim_dur=0.20, style="small"):
    """Pop-in (ease-out) → hold → pop-out (ease-in) as PNG frame sequence."""
    img = _make_card_img(title, subtitle, style=style, scale=scale)
    iw, ih = img.size
    fdir = os.path.join(workdir, f"fr_{idx:03d}")
    os.makedirs(fdir, exist_ok=True)
    n = 0

    def _frame(ratio):
        ratio = max(0.02, min(1.0, ratio))
        nw, nh = max(1, int(iw*ratio)), max(1, int(ih*ratio))
        f = Image.new("RGBA", (iw, ih), (0,0,0,0))
        s = img.resize((nw, nh), Image.LANCZOS)
        f.paste(s, ((iw-nw)//2, (ih-nh)//2), s)
        return f

    def save(fr):
        nonlocal n
        fr.save(os.path.join(fdir, f"f{n:05d}.png"), "PNG"); n += 1

    in_n = max(3, int(fps * anim_dur))
    for i in range(in_n):
        t = (i+1)/in_n
        save(_frame(1-(1-t)**3))          # ease-out cubic

    hold_n = max(1, int(fps * hold_dur))
    for _ in range(hold_n):
        save(img.copy())

    out_n = max(3, int(fps * anim_dur))
    for i in range(out_n):
        t = (i+1)/out_n
        save(_frame((1-t)**3))             # ease-in cubic (shrinks to 0)

    return fdir, (iw, ih), n / fps


# ── video helpers ─────────────────────────────────────────────────────

def video_dims(video):
    try:
        out = subprocess.check_output(
            ["ffprobe","-v","error","-select_streams","v:0",
             "-show_entries","stream=width,height","-of","csv=p=0:s=x",video],
            stderr=subprocess.DEVNULL).decode().strip()
        w, h = out.split("x"); return int(w), int(h)
    except: return 1920, 1080


def _has_audio(video):
    try:
        out = subprocess.check_output(
            ["ffprobe","-v","error","-select_streams","a",
             "-show_entries","stream=index","-of","csv=p=0",video],
            stderr=subprocess.DEVNULL).decode().strip()
        return bool(out)
    except: return False


def _video_dur(video):
    try:
        return float(subprocess.check_output(
            ["ffprobe","-v","error","-show_entries","format=duration",
             "-of","default=nw=1:nk=1",video]).strip())
    except: return 60.0


# ── LLM: scene extraction ─────────────────────────────────────────────

def _auto_n_scenes(total_dur):
    if total_dur < 60:   return 2
    if total_dur < 120:  return 3
    if total_dur < 300:  return 4
    if total_dur < 600:  return 5
    return min(7, int(total_dur / 120))


def extract_scenes(cfg, chunks, n_scenes=None):
    """
    Ask LLM to produce N scenes, each with 2-3 simultaneous cards.
    Returns: [{"time": sec, "duration": sec, "cards": [{"title":..., "subtitle":...}]}]
    """
    if not chunks:
        return []
    total = max(float(c.get("end",0)) for c in chunks)
    if n_scenes is None:
        n_scenes = _auto_n_scenes(total)

    brief = [{"i": c["i"], "start": round(float(c["start"]),1),
               "end": round(float(c["end"]),1),
               "text": (c.get("en") or c.get("vi",""))[:200]}
             for c in chunks]

    prompt = (
        f"Video {total:.0f}s dài. Tạo ĐÚNG {n_scenes} SCENE để overlay lên video.\n"
        f"Mỗi SCENE: 2-3 thẻ trắng hiện CÙNG LÚC (không phải lần lượt).\n"
        f"Transcript:\n{json.dumps(brief, ensure_ascii=False)}\n\n"
        f"Trả về JSON:\n"
        f'{{\"scenes\":[{{'
        f'\"time\":<giây bắt đầu>,'
        f'\"duration\":5.0,'
        f'\"cards\":[{{\"title\":\"<2-3 từ>\",\"subtitle\":\"<3-6 từ>\"}},...(2-3 cards)]'
        f'}}, ...]}}\n\n'
        f"Quy tắc:\n"
        f"- Phân bổ đều từ đầu đến cuối video\n"
        f"- time >= 2.0 và gap giữa các scene >= 4s\n"
        f"- time <= {max(3, total-6):.0f}\n"
        f"- title: 2-3 từ ngắn, KHÔNG dùng markdown (**/*/##)\n"
        f"- subtitle: 3-6 từ mô tả ngắn, KHÔNG dùng markdown\n"
        f"- CHỈ JSON thuần, không giải thích, không markdown"
    )

    if cfg.get("groq_key"):
        url = "https://api.groq.com/openai/v1/chat/completions"
        key = cfg["groq_key"]
    else:
        url = cfg.get("kyma_base","https://api.kymaapi.com") + "/v1/chat/completions"
        key = cfg["kyma_key"]

    body = {"model":"llama-3.3-70b-versatile","temperature":0.25,
            "response_format":{"type":"json_object"},
            "messages":[{"role":"user","content":prompt}]}
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Authorization":"Bearer "+key,
                                          "Content-Type":"application/json",
                                          "User-Agent":"kyma-dub/2.0"})
    try:
        resp = json.load(urllib.request.urlopen(req, timeout=30))
        raw = resp["choices"][0]["message"]["content"]
        if raw.startswith("```"): raw = raw.split("```")[1].lstrip("json").strip()
        data = json.loads(raw)
        scenes = data.get("scenes", [])
        # strip markdown bold/italic from card text
        import re as _re
        for sc in scenes:
            for card in sc.get("cards", []):
                for k in ("title", "subtitle"):
                    if k in card:
                        card[k] = _re.sub(r'\*+', '', card[k]).strip()
        return sorted(scenes, key=lambda s: float(s.get("time",0)))
    except Exception as ex:
        print(f"[hyperframes] scene extraction failed: {ex}")
        return []


def analyze_transcript(cfg, raw_segs):
    """
    Single cheap LLM call (llama-3.1-8b-instant):
    1. Corrects Vietnamese whisper transcription errors (keeps original timestamps)
    2. Extracts N timed info-cards aligned to whisper segment timestamps

    raw_segs: [{start, end, text}] from whisper
    Returns: (corrected_segs, cards)
      corrected_segs: [{start, end, text}]
      cards: [{time, duration, style, title, subtitle}]
        style: 'small' | 'large'
    """
    if not raw_segs:
        return [], []

    import re as _re

    total    = max(float(s.get("end", 0)) for s in raw_segs)
    n_cards  = _auto_n_scenes(total)

    segs_in  = [{"i": i,
                 "t0": round(float(s.get("start", 0)), 2),
                 "t1": round(float(s.get("end",   0)), 2),
                 "tx": (s.get("text","") or s.get("vi","")).strip()[:120]}
                for i, s in enumerate(raw_segs)]

    prompt = (
        f"Đây là transcript {total:.0f}s tiếng Việt từ Whisper STT (có thể nhận sai từ).\n\n"
        f"Làm 2 việc:\n"
        f"A) Sửa lỗi từng segment (sai chính tả, nghe nhầm): giữ NGUYÊN t0/t1, chỉ sửa tx.\n"
        f"B) Chọn ĐÚNG {n_cards} thời điểm quan trọng để hiện card minh hoạ trên video.\n\n"
        f"Segments:\n{json.dumps(segs_in, ensure_ascii=False)}\n\n"
        f"Trả về JSON (ví dụ minh hoạ — THAY BẰNG NỘI DUNG THẬT từ transcript):\n"
        f'{{"corrected":[{{"t0":1.2,"t1":3.8,"tx":"nội dung đã sửa"}},...],\n'
        f'"cards":[{{"time":6.1,"dur":4.5,"style":"small","title":"Tiêu đề thật","sub":"Mô tả thật"}},...]}}\n\n'
        f"Quy tắc bắt buộc:\n"
        f"- corrected: phải có đúng {len(segs_in)} mục, t0/t1 KHÔNG thay đổi, chỉ sửa tx\n"
        f"- cards.time phải là t0 của một segment trong danh sách\n"
        f"- cards: cách nhau ít nhất 6s; mỗi lúc chỉ 1 card\n"
        f"- style='large' chỉ dùng cho điểm nhấn quan trọng nhất (tối đa 1 per 60s)\n"
        f"- title: TỐI ĐA 3 từ, tóm tắt ý chính THẬT từ transcript\n"
        f"- sub: TỐI ĐA 5 từ, giải thích thêm THẬT từ transcript\n"
        f"- KHÔNG dùng **, ##, *italic*, placeholder, hay ví dụ mẫu\n"
        f"- CHỈ trả về JSON thuần, không giải thích"
    )

    if cfg.get("groq_key"):
        url = "https://api.groq.com/openai/v1/chat/completions"
        key = cfg["groq_key"]
    else:
        url = cfg.get("kyma_base", "https://api.kymaapi.com") + "/v1/chat/completions"
        key = cfg.get("kyma_key", "")

    body = {"model": "llama-3.1-8b-instant",   # fast + cheap
            "temperature": 0.15,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "user", "content": prompt}]}
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + key,
                 "Content-Type": "application/json",
                 "User-Agent": "kyma-dub/2.0"})

    def _clean(t): return _re.sub(r'\*+', '', str(t)).strip()

    try:
        resp = json.load(urllib.request.urlopen(req, timeout=45))
        raw  = resp["choices"][0]["message"]["content"]
        if raw.startswith("```"):
            raw = raw.split("```")[1].lstrip("json").strip()
        data = json.loads(raw)

        corrected = [{"start": float(s.get("t0", 0)),
                      "end":   float(s.get("t1", 0)),
                      "text":  _clean(s.get("tx", ""))}
                     for s in data.get("corrected", [])
                     if str(s.get("tx","")).strip()]

        cards = []
        for c in data.get("cards", []):
            title = _clean(c.get("title", ""))
            sub   = _clean(c.get("sub",   ""))
            if not title:
                continue
            cards.append({"time":     max(0.5, float(c.get("time", 0))),
                          "duration": max(3.0, float(c.get("dur",  4.5))),
                          "style":    str(c.get("style", "small")),
                          "title":    title,
                          "subtitle": sub})
        cards.sort(key=lambda c: c["time"])
        return corrected, cards

    except Exception as ex:
        print(f"[hyperframes] analyze_transcript failed: {ex}")
        import traceback; traceback.print_exc()
        fallback = [{"start": float(s.get("start", 0)),
                     "end":   float(s.get("end",   0)),
                     "text":  s.get("text","") or s.get("vi","")}
                    for s in raw_segs]
        return fallback, []


# keep backward-compat alias
def extract_highlights(cfg, chunks, n=None):
    scenes = extract_scenes(cfg, chunks, n_scenes=n)
    # flatten to individual card list for old callers
    cards = []
    for s in scenes:
        for c in s.get("cards",[]):
            cards.append({**c, "time": s["time"], "duration": s.get("duration",4.0)})
    return cards


# ── PIP mask ─────────────────────────────────────────────────────────

def _pip_mask_img(w, h, radius):
    m = Image.new("L", (w, h), 0)
    ImageDraw.Draw(m).rounded_rectangle([0,0,w-1,h-1], radius=radius, fill=255)
    return m


# ── main overlay function ─────────────────────────────────────────────

def overlay_scenes(video, scenes, workdir, out_video,
                   srt_path=None, pip=True):
    """
    Render HyperFramers overlay.
    pip=True  → presenter scaled to bottom-left corner, cards fill right/top area.
    pip=False → cards overlaid on full video (column layout on left).

    scenes: [{"time":t, "duration":d, "cards":[{"title":..,"subtitle":..},...]}]
    """
    if not scenes:
        return False

    vw, vh = video_dims(video)
    card_scale = max(0.5, min(vw, vh) / 1080.0)
    has_audio = _has_audio(video)
    total_dur = _video_dur(video)
    margin = max(16, int(min(vw, vh) * 0.025))

    # ── layout geometry ──────────────────────────────────────────────
    if pip:
        # Side-by-side: presenter on LEFT 40% (full height), cards on RIGHT 60%
        pip_w = int(vw * 0.40)
        pip_h = vh
        pip_r = min(32, int(pip_w * 0.025))   # small rounded corner

        # Rounded corner mask (full left panel height)
        mask_img = _pip_mask_img(pip_w, pip_h, pip_r)
        mask_path = os.path.join(workdir, "pip_mask.png")
        mask_img.save(mask_path)

        # Card area: right 58% of screen
        cx0 = pip_w + margin * 2
        cx1 = vw - margin
        cy0 = margin * 2
        cy1 = vh - margin * 2
    else:
        # Overlay mode: cards on LEFT side of full video (don't resize)
        cx0 = int(vw * 0.03)
        cy0 = int(vh * 0.10)
        cx1 = int(vw * 0.38)
        cy1 = int(vh * 0.90)
        mask_path = None

    content_w = cx1 - cx0
    content_h = cy1 - cy0

    # ── Build card frame sequences ───────────────────────────────────
    all_inputs = []   # (itsoffset, fdir, t1, x, y)

    STAGGER = 0.28  # seconds between each card's pop-in within a scene

    for si, scene in enumerate(scenes):
        t0       = max(1.0, float(scene.get("time",1)))
        duration = float(scene.get("duration", 5.0))
        cards_in = scene.get("cards", [])
        if not cards_in: continue

        # Render frames for every card — later cards have shorter hold (end together)
        scene_cards = []
        for ci, card in enumerate(cards_in):
            title = str(card.get("title","")).strip()
            sub   = str(card.get("subtitle","")).strip()
            if not title: continue
            card_offset = ci * STAGGER
            hold = max(0.5, duration - 0.40 - card_offset)
            fdir, (cw, ch), clip_dur = _make_card_frames(
                title, sub, scale=card_scale, workdir=workdir,
                idx=si*10+ci, fps=25, hold_dur=hold, anim_dur=0.20,
                style="small")
            scene_cards.append((fdir, cw, ch, clip_dur, card_offset))

        if not scene_cards: continue

        # Column positions: center-stack in content area
        n = len(scene_cards)
        gap = int(vh * 0.018)
        total_h = sum(ch for _,_,ch,_,_ in scene_cards) + (n-1) * gap
        start_y = cy0 + max(0, (content_h - total_h) // 2)

        y = start_y
        for fdir, cw, ch, clip_dur, card_offset in scene_cards:
            card_t0 = t0 + card_offset          # staggered start
            x = cx0 + max(0, (content_w - cw) // 2)
            x = min(x, vw - cw - margin)
            y = min(y, vh - ch - margin)
            all_inputs.append((card_t0, fdir, card_t0 + clip_dur, x, y))
            y += ch + gap

    if not all_inputs:
        return False

    # ── ffmpeg command ────────────────────────────────────────────────
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]

    # Determine input indices
    # 0: original video
    # 1: pip mask (if pip)
    # 1+ or 2+: card frame sequences
    cmd += ["-i", video]
    if pip:
        cmd += ["-i", mask_path]
    card_in_start = 2 if pip else 1

    for t0, fdir, t1, x, y in all_inputs:
        cmd += ["-itsoffset", f"{t0:.3f}", "-framerate", "25",
                "-i", os.path.join(fdir, "f%05d.png")]

    # ── filter_complex ────────────────────────────────────────────────
    f = []

    if pip:
        # Side-by-side: scale presenter to fill left panel (maintain AR, letterbox if needed)
        f.append(f"[0:v]scale={pip_w}:{pip_h}:force_original_aspect_ratio=decrease,"
                 f"pad={pip_w}:{pip_h}:(ow-iw)/2:(oh-ih)/2:color=0xF7F6F2,"
                 f"format=rgba[pip_raw]")
        f.append("[pip_raw][1:v]alphamerge[pip_masked]")
        f.append(f"color=c=0xF7F6F2:s={vw}x{vh}:r=25:d={total_dur:.2f}[bg]")
        f.append(f"[bg][pip_masked]overlay=0:0:format=auto[base]")
        prev = "base"
    else:
        # Overlay: full video, cards sit on top
        f.append("[0:v]copy[base]")
        prev = "base"

    # Burn subtitles (before cards so cards render on top)
    if srt_path and os.path.exists(srt_path):
        short = min(vw, vh)
        fs_size = max(11, int(short * 0.014))   # ~15px on 1080p — small, 1 line
        mv = max(16, int(vh * 0.025))
        style = (f"FontName=Arial,FontSize={fs_size},PrimaryColour=&H00FFFFFF,"
                 f"BackColour=&H1A000000,BorderStyle=3,Outline=0,Shadow=0,"
                 f"MarginV={mv},Alignment=2,Bold=0")
        safe_srt = srt_path.replace("'", "\\'")
        f.append(f"[{prev}]subtitles='{safe_srt}':force_style='{style}'[vsub]")
        prev = "vsub"

    # Overlay each card clip
    for i, (t0, fdir, t1, x, y) in enumerate(all_inputs):
        cin  = card_in_start + i
        ctag = f"hfc{i}"
        vtag = f"hfv{i}"
        f.append(f"[{cin}:v]format=rgba[{ctag}]")
        f.append(f"[{prev}][{ctag}]overlay={x}:{y}:"
                 f"enable='between(t,{t0:.3f},{t1:.3f})'[{vtag}]")
        prev = vtag

    cmd += ["-filter_complex", ";".join(f)]
    cmd += ["-map", f"[{prev}]"]
    if has_audio:
        cmd += ["-map", "0:a:0", "-c:a", "copy"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-shortest", out_video]

    subprocess.run(cmd, check=True)
    return True


# backward-compat wrapper
def overlay_cards(video, cards, workdir, out_video, srt_path=None):
    """Convert old-style card list to scenes (1 card each) and render."""
    scenes = [{"time": float(c.get("time",0)),
               "duration": float(c.get("duration",4.0)),
               "cards": [c]}
              for c in cards]
    return overlay_scenes(video, scenes, workdir, out_video,
                          srt_path=srt_path, pip=False)


# ── overlay_cards_v2 — individual timed cards with style support ──────

def overlay_cards_v2(video, cards, workdir, out_video, srt_path=None, pip=False):
    """
    Render individually timed cards from analyze_transcript output.
    cards: [{time, duration, style, title, subtitle}]
      style: 'small' (left column) | 'large' (bigger, more centered)
    pip=False: cards overlay on full video (default)
    pip=True:  presenter on left 40%, cards in right 60%
    """
    if not cards:
        return False

    vw, vh    = video_dims(video)
    base_sc   = max(0.5, min(vw, vh) / 1080.0)
    has_audio = _has_audio(video)
    total_dur = _video_dur(video)
    margin    = max(16, int(min(vw, vh) * 0.025))

    # ── content area (where cards are placed) ─────────────────────────
    if pip:
        pip_w  = int(vw * 0.40)
        pip_h  = vh
        pip_r  = min(32, int(pip_w * 0.025))
        mask_img  = _pip_mask_img(pip_w, pip_h, pip_r)
        mask_path = os.path.join(workdir, "pip_mask_v2.png")
        mask_img.save(mask_path)
        area_x = pip_w + margin
        area_w = vw - area_x - margin
    else:
        area_x = 0
        area_w = vw
        mask_path = None

    # Slot positions: (fraction of area_w for x, fraction of vh for y)
    SMALL_SLOTS = [(0.03, 0.10), (0.03, 0.31), (0.03, 0.53), (0.03, 0.72)]
    # Large cards: centered horizontally (x~0.34 centers a ~600px card on 1920px),
    # placed in upper-center and lower-center to avoid blocking the speaker's face too much.
    LARGE_SLOTS = [(0.34, 0.14), (0.32, 0.60)]

    def _pos(fx, fy, cw, ch):
        x = area_x + int(area_w * fx)
        y = int(vh * fy)
        return (min(x, vw - cw - margin), min(y, vh - ch - margin))

    # ── build card frame sequences ────────────────────────────────────
    all_inputs = []   # (t0, fdir, t1, x, y)
    small_idx  = 0
    large_idx  = 0

    for ci, card in enumerate(cards):
        style    = str(card.get("style", "small"))
        t0       = max(0.5, float(card.get("time", 0)))
        duration = float(card.get("duration", 4.5))
        title    = str(card.get("title", "")).strip()
        subtitle = str(card.get("subtitle", "")).strip()
        if not title:
            continue

        is_large   = (style == "large")
        # HTML template already uses larger fonts for 'large' style —
        # don't multiply scale again, or the card becomes screen-filling.
        card_scale = base_sc
        hold       = max(0.5, duration - 0.44)

        fdir, (cw, ch), clip_dur = _make_card_frames(
            title, subtitle, scale=card_scale, workdir=workdir,
            idx=ci, fps=25, hold_dur=hold, anim_dur=0.22, style=style)

        if is_large:
            fx, fy = LARGE_SLOTS[large_idx % len(LARGE_SLOTS)]
            large_idx += 1
        else:
            fx, fy = SMALL_SLOTS[small_idx % len(SMALL_SLOTS)]
            small_idx += 1

        x, y = _pos(fx, fy, cw, ch)
        all_inputs.append((t0, fdir, t0 + clip_dur, x, y))

    if not all_inputs:
        return False

    # ── ffmpeg command ────────────────────────────────────────────────
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    cmd += ["-i", video]
    if pip:
        cmd += ["-i", mask_path]
    card_in_start = 2 if pip else 1

    for t0, fdir, t1, x, y in all_inputs:
        cmd += ["-itsoffset", f"{t0:.3f}", "-framerate", "25",
                "-i", os.path.join(fdir, "f%05d.png")]

    # ── filter_complex ────────────────────────────────────────────────
    f = []
    if pip:
        f.append(f"[0:v]scale={pip_w}:{pip_h}:force_original_aspect_ratio=decrease,"
                 f"pad={pip_w}:{pip_h}:(ow-iw)/2:(oh-ih)/2:color=0xF7F6F2,"
                 f"format=rgba[pip_raw]")
        f.append("[pip_raw][1:v]alphamerge[pip_masked]")
        f.append(f"color=c=0xF7F6F2:s={vw}x{vh}:r=25:d={total_dur:.2f}[bg]")
        f.append("[bg][pip_masked]overlay=0:0:format=auto[base]")
        prev = "base"
    else:
        f.append("[0:v]copy[base]")
        prev = "base"

    if srt_path and os.path.exists(srt_path):
        short   = min(vw, vh)
        fs_size = max(11, int(short * 0.014))
        mv      = max(16, int(vh * 0.025))
        sty     = (f"FontName=Arial,FontSize={fs_size},PrimaryColour=&H00FFFFFF,"
                   f"BackColour=&H1A000000,BorderStyle=3,Outline=0,Shadow=0,"
                   f"MarginV={mv},Alignment=2,Bold=0")
        safe_srt = srt_path.replace("'", "\\'")
        f.append(f"[{prev}]subtitles='{safe_srt}':force_style='{sty}'[vsub]")
        prev = "vsub"

    for i, (t0, fdir, t1, x, y) in enumerate(all_inputs):
        cin  = card_in_start + i
        ctag = f"cv2c{i}"
        vtag = f"cv2v{i}"
        f.append(f"[{cin}:v]format=rgba[{ctag}]")
        f.append(f"[{prev}][{ctag}]overlay={x}:{y}:"
                 f"enable='between(t,{t0:.3f},{t1:.3f})'[{vtag}]")
        prev = vtag

    cmd += ["-filter_complex", ";".join(f)]
    cmd += ["-map", f"[{prev}]"]
    if has_audio:
        cmd += ["-map", "0:a:0", "-c:a", "copy"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-shortest", out_video]

    subprocess.run(cmd, check=True)
    return True
