# Deploy web app Video2Slides lên Modal

Web app (FastAPI + giao diện) chạy trên Modal tại:
<https://anhngocforwork2604--video2slides-web-web.us-east.modal.run>

Mã deploy: [infrastructure/modal_web_app.py](../infrastructure/modal_web_app.py).

## Kiến trúc

- **1 container CPU** (`max_containers=1`) chạy FastAPI + hàng đợi job. Không cần GPU: FFmpeg/Whisper, Ollama
  và SDXL vẫn chạy trên các worker GPU Modal sẵn có.
- **Dữ liệu** (job, thư viện slide, token Google) lưu trong Modal Volume `video2slides-web-data`, không mất
  khi container tắt hoặc deploy lại.
- **Biến môi trường** lấy từ Modal Secret `video2slides-web-env` (tạo từ `.env`). File `.env` không bao giờ
  được đưa vào image hay GitHub.
- **Mật khẩu bắt buộc** (`APP_PASSWORD`) vì URL công khai. Mật khẩu hiện tại lưu ở `.env.modal-web`
  (máy quản trị, đã bị git bỏ qua).
- Container **tự tắt** sau 20 phút không có request (không tính tiền khi rảnh). Khi trang đang mở, trình
  duyệt hỏi tiến độ liên tục nên job không bị dừng giữa chừng. Muốn luôn bật (tính tiền CPU 24/7):
  `V2S_WEB_MIN_CONTAINERS=1 modal deploy infrastructure/modal_web_app.py`.

## Deploy lại sau khi sửa code

```bash
.venv/Scripts/modal.exe deploy infrastructure/modal_web_app.py
```

## Cập nhật biến môi trường (sau khi sửa `.env`)

Secret phải gồm nội dung `.env` **và** `APP_PASSWORD` trong `.env.modal-web`:

```bash
cat .env .env.modal-web > web.env && .venv/Scripts/modal.exe secret create video2slides-web-env --from-dotenv web.env --force && rm web.env
```

Sau đó chạy lại lệnh deploy để container nhận giá trị mới.

## Giới hạn cần biết

- Modal giới hạn mỗi request web **150 giây**: video tải lên trực tiếp phải upload xong trong thời gian đó
  (mạng chậm + video lớn sẽ lỗi). Video dài nên dùng nhập bằng link YouTube/Facebook.
- YouTube có thể chặn tải từ IP của máy chủ cloud.
- Google Slides: thêm URL `…/api/google/oauth/callback` của Modal vào Redirect URI trong Google Cloud Console
  nếu bật tính năng này.
- Job đang chạy sẽ bị đánh dấu lỗi nếu container khởi động lại (deploy lại giữa chừng): bấm "Thử lại".
