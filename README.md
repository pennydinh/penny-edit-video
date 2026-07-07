# Penny Studio 🎬

**Bộ công cụ video AI chạy local — 2 phiên bản trong một app:**

| | Làm gì | Dùng khi |
|---|---|---|
| 🎤 **Penny Dub** | Dịch & lồng tiếng video sang ngôn ngữ khác bằng giọng AI tự nhiên, khớp thời gian gốc. Có voice-clone + phụ đề. | Muốn video nói ngôn ngữ khác |
| 🎬 **Penny Recut** | Giữ nguyên tiếng gốc, AI **tự thiết kế minh hoạ** (khái niệm, sơ đồ, con số, liệt kê…) theo **từng câu nói**. Duyệt storyboard rồi render. | Muốn "đóng gói" video talking-head cho đẹp, chuyên nghiệp |

Cả hai chạy trên cùng một web app, cùng một cửa sổ trình duyệt.

---

## 1. Cài đặt

**Yêu cầu:** Python 3.10+, `ffmpeg`, và Chromium (cho Recut).

```bash
git clone https://github.com/mp1391004/penny-edit-video.git
cd penny-edit-video

pip install -r requirements.txt
playwright install chromium      # chỉ 1 lần, cho Recut

# ffmpeg (nếu chưa có):  macOS: brew install ffmpeg  ·  Ubuntu: sudo apt install ffmpeg
```

## 2. Chèn API key (bắt buộc)

```bash
cp .env.example .env
```

Mở `.env` và điền:

| Key | Bắt buộc cho | Lấy ở đâu |
|---|---|---|
| `GROQ_API_KEY` | **Recut** (bóc transcript + AI thiết kế) | [console.groq.com/keys](https://console.groq.com/keys) — **miễn phí** |
| `KYMA_API_KEY` | **Dub** (dịch + lồng tiếng) | [kymaapi.com](https://kymaapi.com?aff=offer) — có credit miễn phí |
| `ELEVENLABS_API_KEY` | tuỳ chọn (voice-clone cho Dub) | [elevenlabs.io](https://try.elevenlabs.io/r3v0yleue0l0) |

> 💡 Chỉ dùng Recut → chỉ cần `GROQ_API_KEY`. Chỉ dùng Dub → chỉ cần `KYMA_API_KEY`.
> File `.env` đã bị `.gitignore` chặn — key của bạn **không bao giờ** bị đẩy lên GitHub.

## 3. Chạy

```bash
./start.sh          # hoặc: python3 web_ui_v2.py
```

- **Trang chủ:** http://localhost:7861 → chọn Dub hoặc Recut
- Dub: http://localhost:7861/dub · Recut: http://localhost:7861/recut

Tắt: `./stop.sh`

---

## 4. Penny Dub

1. Upload video → chọn ngôn ngữ đích + giọng đọc
2. (tuỳ chọn) bật voice-clone, phụ đề
3. Submit → nhận video đã lồng tiếng

## 5. Penny Recut (workflow 3 bước có duyệt)

1. **Upload** → tự bóc transcript
2. **Transcript** → sửa lỗi chữ (hoặc bấm "✨ AI sửa"), rồi "Tiếp: Lên khung"
3. **Storyboard** → AI đã tự chọn template cho từng câu; xem **wireframe preview**, sửa nếu muốn (đổi template, chữ, icon, thêm/xoá mục, tách/xoá câu). Tick/bỏ caption.
4. **Gen video** → render (~4–5 phút cho video 2 phút)

**7 template minh hoạ** (AI tự chọn theo nội dung câu, trắng-xanh sạch):
Khái niệm · Tối giản · Liệt kê · Sơ đồ luồng · Slide · Con số · So sánh.

---

## 6. Chạy online 24/7 — deploy VPS (tuỳ chọn)

Muốn Penny chạy liên tục và có link riêng để chia sẻ / dùng mọi nơi, thuê một VPS nhỏ:

1. **VPS** — cài Ubuntu rồi làm lại bước [Cài đặt](#1-cài-đặt). Gói KVM 1–2 CPU là đủ; render nhanh hơn nếu nhiều CPU. Mình dùng [Hostinger VPS](https://hostinger.com/PENNYDEAL10).
2. **Domain** — mua tên miền rồi trỏ về IP VPS (Hostinger có sẵn cả domain).
3. Chạy `./start.sh`, mở cổng `7861` — hoặc đặt **Nginx reverse proxy + HTTPS** để có `https://tênban.com`.

> 💡 Mình xài [Hostinger](https://hostinger.com/PENNYDEAL10) cho cả VPS + domain — mã `PENNYDEAL10` giảm thêm.
