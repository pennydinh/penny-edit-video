#!/bin/bash
# Penny Studio — khởi động 1 lệnh
# API key đọc từ file .env (xem .env.example). KHÔNG nhúng key vào đây.
cd "$(dirname "$0")"

pkill -f "web_ui_v2.py" 2>/dev/null
sleep 1

python3 web_ui_v2.py &> /tmp/penny-studio.log &
sleep 2

PORT="${PORT_V2:-7861}"
echo ""
echo "  ✅ Penny Studio đang chạy!"
echo ""
echo "  Trang chủ:  http://localhost:$PORT"
echo "  Dub:        http://localhost:$PORT/dub"
echo "  Recut:      http://localhost:$PORT/recut"
echo ""
echo "  Log: /tmp/penny-studio.log   ·   Tắt: ./stop.sh"
