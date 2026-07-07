# Penny Studio 🎬

**Bộ công cụ video AI chạy local — 2 phiên bản trong một app:**

| | Làm gì | Dùng khi |
|---|---|---|
| 🎤 **Penny Dub** | Dịch & lồng tiếng video sang ngôn ngữ khác bằng giọng AI tự nhiên, khớp thời gian gốc. Có voice-clone + phụ đề. | Muốn video nói ngôn ngữ khác |
| 🎬 **Penny Recut** | Giữ nguyên tiếng gốc, AI **tự thiết kế minh hoạ** (khái niệm, sơ đồ, con số, liệt kê…) theo **từng câu nói**. Duyệt storyboard rồi render. | Muốn "đóng gói" video talking-head cho đẹp, chuyên nghiệp |

Cả hai chạy trên cùng một web app, cùng một cửa sổ trình duyệt.

> Dự án phát triển từ [kyma-dub CLI](README-kyma-cli.md) (phần lồng tiếng), bổ sung engine **Recut** dựng minh hoạ theo giọng nói.

---

## 1. Cài đặt

**Yêu cầu:** Python 3.10+, `ffmpeg`, và Chromium (cho Recut).

```bash
git clone https://github.com/mp1391004/kyma-dub-studio.git
cd kyma-dub-studio

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
| `GROQ_API_KEY` | **Recut** (bóc transcript + AI thiết kế) | https://console.groq.com/keys — **miễn phí** |
| `KYMA_API_KEY` | **Dub** (dịch + lồng tiếng) | https://kymaapi.com — có credit miễn phí |
| `ELEVENLABS_API_KEY` | tuỳ chọn (voice-clone cho Dub) | https://elevenlabs.io |

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

## 6. Chèn link affiliate (kiếm tiền)

Video render là file MP4 (người xem không click trực tiếp), nên affiliate hoạt động thế này —

**Cần chuẩn bị:**
1. **Tài khoản chương trình affiliate** của công cụ bạn nhắc trong video → nhận **link tracking** riêng.
2. **Link rút gọn / tên miền thương hiệu** (Bitly, Dub.co, hoặc `go.tênban.com`) để hiện link ngắn dễ đọc trên màn hình.
3. (tuỳ chọn) **QR code** cho link đó.

**Cách chèn:**
- **Trong video:** thêm 1 câu CTA cuối → ở Storyboard chọn template **Slide** / **Tối giản**, điền link ngắn (vd `go.penny.vn/notebooklm`). Có thể **✂ Tách** thêm 1 beat CTA ở cuối.
- **Mô tả bài đăng** (YouTube/TikTok/Facebook): dán link affiliate đầy đủ — đây là nơi click chính.
- **Ghim comment / bio.**

> Penny Studio hiện chưa tự chèn link affiliate. Có thể thêm **template CTA (ô nhập link + QR)** vào Storyboard nếu cần.

---

## Ghi chú
- Recut render per-frame qua Chromium (GSAP) → chất lượng cao, mất vài phút. Máy khoẻ → nhanh hơn.
- Không commit `.env` hay file media (đã có trong `.gitignore`).
- License: MIT.
