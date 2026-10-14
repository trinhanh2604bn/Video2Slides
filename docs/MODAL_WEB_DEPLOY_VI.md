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

- Modal giới hạn mỗi request web **150 giây**. Vì vậy trình duyệt gửi video theo **từng phần nhỏ**
  (`POST /api/uploads` → nhiều `PUT /api/uploads/{id}?offset=…` → `POST /api/uploads/{id}/finish`); kích thước mỗi
  phần tự điều chỉnh theo tốc độ mạng (~30 giây/phần), phần lỗi được gửi lại và tiếp tục từ byte máy chủ đã nhận.
  Nhờ đó video lớn trên mạng chậm vẫn tải lên được; chỉ cần giữ trang mở tới khi xong. Phiên tải dở bị xóa sau 24 giờ.
- YouTube thường chặn tải video từ IP của máy chủ cloud (lỗi "xác minh chống bot"). Ứng dụng **không** vượt qua
  bước chặn này (không dùng cookie tài khoản hay proxy). Khi bị chặn, thông báo lỗi hướng dẫn chủ video tải MP4
  trong YouTube Studio rồi dùng **Tải tệp lên**, kèm nút chuyển thẳng sang tab tải tệp. Nhập link vẫn hoạt động
  bình thường khi chạy ứng dụng trên máy cá nhân (`start.bat`).
- Google Slides: xem mục bên dưới.

## Google Slides trên Modal

Bấm **Kết nối Google** ngay trên web app. Vì client OAuth loại "Desktop app" chỉ cho Google quay về
`127.0.0.1`, web app dùng luồng **dán địa chỉ**:

1. Tab Google mở ra → đăng nhập, cho phép quyền.
2. Google chuyển sang `http://127.0.0.1:53682/…` và báo "không thể kết nối" — bình thường.
3. Chép toàn bộ địa chỉ trên thanh địa chỉ của tab đó, dán vào hộp thoại trong web app → **Hoàn tất**.

Muốn bỏ bước dán: tạo client OAuth loại **Web application** với Authorized redirect URI
`https://anhngocforwork2604--video2slides-web-web.us-east.modal.run/api/google/oauth/callback`, đặt file JSON
của nó vào `/google/client_secret.json` trên Volume; web app sẽ tự quay về sau khi đăng nhập.

Token lưu trong Volume (thư mục `/google`). Mọi người dùng web app trên Modal đều xuất slide vào **Google
Drive của tài khoản đã kết nối** (quyền `drive.file`: app chỉ thấy các file do chính nó tạo).

Cách cũ vẫn dùng được: đăng nhập trên máy rồi chép `token.json` lên Volume.

Khi web app báo mất kết nối Google (token bị thu hồi, hoặc hết hạn sau 7 ngày nếu OAuth consent screen đang
ở chế độ "Testing"): bấm Kết nối Google lại như trên, hoặc đăng nhập trên máy rồi chép lên:

```bash
export MSYS_NO_PATHCONV=1   # Git Bash: tránh đổi /google thành C:/Program Files/Git/google
for f in client_secret.json token.json state.json; do .venv/Scripts/modal.exe volume put -f video2slides-web-data data/google/$f /google/$f; done
.venv/Scripts/modal.exe deploy infrastructure/modal_web_app.py   # khởi động lại container để đọc file mới
```
- Job đang chạy sẽ bị đánh dấu lỗi nếu container khởi động lại (deploy lại giữa chừng): bấm "Thử lại".
