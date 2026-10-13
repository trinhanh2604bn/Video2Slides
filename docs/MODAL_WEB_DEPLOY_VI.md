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
- Google Slides: xem mục bên dưới.

## Google Slides trên Modal

Đăng nhập Google chỉ chạy được tại `http://127.0.0.1` (client OAuth loại "Desktop app"), nên bản Modal dùng
lại kết nối đã đăng nhập trên máy: `client_secret.json` + `token.json` được chép vào Volume (thư mục
`/google`). Mọi người dùng web app trên Modal đều xuất slide vào **Google Drive của tài khoản đó** (quyền
`drive.file`: app chỉ thấy các file do chính nó tạo).

Khi web app báo mất kết nối Google (token bị thu hồi, hoặc hết hạn sau 7 ngày nếu OAuth consent screen đang
ở chế độ "Testing"): đăng nhập lại trên máy (`start.bat` → Kết nối Google), rồi chép lên lại:

```bash
export MSYS_NO_PATHCONV=1   # Git Bash: tránh đổi /google thành C:/Program Files/Git/google
for f in client_secret.json token.json state.json; do .venv/Scripts/modal.exe volume put -f video2slides-web-data data/google/$f /google/$f; done
.venv/Scripts/modal.exe deploy infrastructure/modal_web_app.py   # khởi động lại container để đọc file mới
```
- Job đang chạy sẽ bị đánh dấu lỗi nếu container khởi động lại (deploy lại giữa chừng): bấm "Thử lại".
