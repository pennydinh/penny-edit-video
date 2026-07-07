# CLAUDE.md — hướng dẫn cho AI agent (Claude Code)

Đọc file này trước khi sửa code. Mô tả kiến trúc để bạn (Claude) hiểu và sửa đúng chỗ.

## Đây là gì
**Penny Studio** — web app Flask (1 file server) chạy local, gồm **2 công cụ video AI**:
- **Dub** — dịch & lồng tiếng video (pipeline ở `lib/pipeline.py`)
- **Recut** — giữ tiếng gốc, AI thiết kế minh hoạ theo từng câu nói, render per-frame (engine ở `lib/recut.py`)

## Chạy
```bash
pip install -r requirements.txt
playwright install chromium          # cho Recut
cp .env.example .env                 # điền GROQ_API_KEY (Recut) và/hoặc KYMA_API_KEY (Dub)
python3 web_ui_v2.py                 # http://localhost:7861
```
Cần: Python 3.10+, `ffmpeg` (system), Chromium (playwright). Server đọc key từ `.env` qua `_load_env()`.

## Cấu trúc
| File | Vai trò |
|---|---|
| `web_ui_v2.py` | **Server chính** (Flask). Chứa: landing `/`, `/dub`, `/recut`; toàn bộ HTML/JS cho 2 UI; API endpoints. Chạy file này. |
| `lib/recut.py` | **Engine Recut** — planner (LLM) + 7 scene template + render per-frame (GSAP + playwright + ffmpeg). |
| `lib/pipeline.py` | Pipeline **Dub** (transcribe/translate/TTS/mux). |
| `lib/hyperframes.py` | Overlay cũ (legacy, fallback cho dub-path). Không dùng cho Recut mới. |
| `web_ui.py` | UI Dub bản v1 cũ (port 7860, không dùng nữa). |

## Kiến trúc Recut (quan trọng nhất)
Luồng 3 bước có duyệt, API dưới `/api/recut/*`:
1. `start` → tách audio + `_transcribe_only` (Whisper qua Groq) → segments
2. `correct` → `correct_transcript()` sửa lỗi chữ (giữ timestamp + dấu tiếng Việt)
3. `storyboard` → `analyze_beats()` = **1 lần gọi LLM** chọn template + nội dung cho từng câu → beats
4. `generate` → `render_recut()` render video

**`render_recut()`** (kiểu "frame adapter" của HyperFramers):
1. ffmpeg tách video → frames JPG
2. Dựng **1 trang HTML** (`build_page`): `#face-img` đổi src mỗi frame + graphics + **1 GSAP timeline** (paused)
3. playwright: mỗi frame → set face src, seek timeline, screenshot PNG
4. ffmpeg ghép PNG + mux audio + burn caption

**7 scene template** (mỗi beat = 1 template, phong cách trắng-xanh sạch):
`concept · minimal · list · flow · slide · stat · compare`. Face mode suy ra từ template (`scene_face`): concept/list/stat → side (mặt phải); minimal/flow/compare → full; slide → hero.

## Muốn sửa template minh hoạ thì đụng những đâu (đồng bộ 5 chỗ)
1. `lib/recut.py` `_PAGE_CSS` — CSS của scene
2. `lib/recut.py` `renderScene()` (JS) — dựng HTML + animation từng template
3. `lib/recut.py` `_valid_scene()` — validate nội dung template (model)
4. `lib/recut.py` `analyze_beats()` prompt — dạy LLM chọn/điền template
5. `web_ui_v2.py` `wireframeSVG()` (preview) + `fieldsHTML()` (ô sửa) — UI storyboard

Roundtrip UI: `beat_to_row()` (beat→form) và `row_to_beat()` (form→beat) phải khớp field.

## Quy ước
- Style: trắng-xanh (`#2563eb`), clean, ít element. "Too cluttered" là lỗi hay gặp — giữ gọn.
- Timing khoá theo segment (voice), không cho LLM tự chế t0/t1.
- Icon: bộ line-art trong `ICONS` (JS) + `_VALID_ICONS`/`_ICON_ALIAS` (Python) phải khớp tên.
- KHÔNG commit `.env`, media (`.gitignore` đã chặn). KHÔNG hardcode API key.

## Test nhanh một render (không qua web)
```python
import sys; sys.path.insert(0,'lib')
from recut import render_recut
beats=[{'t0':0,'t1':4,'template':'concept','eyebrow':'CÔNG CỤ','title':'NotebookLM','sub':'kết nối MCP','icon':'notebook'}]
render_recut('input.mp4', beats, '/tmp/w', '/tmp/out.mp4', fps=25, canvas=(1280,720), trim=4.0, captions=False)
```
