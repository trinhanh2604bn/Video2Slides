# Cấu hình Google Slides (một lần, khoảng 10 phút, miễn phí)

Sau khi cấu hình, mỗi video sẽ được tự động tạo thành **bài Google Slides gốc, chỉnh sửa được** trong
Google Drive của bạn, kèm **ghi chú thuyết trình (Speaker Notes)** cho từng slide. Tệp PowerPoint vẫn
luôn được tạo như trước — Google Slides là tùy chọn thêm.

## Có tốn tiền không?

Không. Google Drive API và Google Slides API **miễn phí**, không cần bật thanh toán (billing) cho cách
dùng này. Chỉ có hạn mức số yêu cầu mỗi phút; ứng dụng tự chờ và thử lại khi chạm hạn mức, và mỗi bài
chỉ gọi khoảng 5–8 yêu cầu (tải lên, đọc lại để kiểm tra, ghi Speaker Notes theo lô).

## Ứng dụng được quyền gì?

Chỉ một quyền: `https://www.googleapis.com/auth/drive.file` — **tạo tệp mới và chỉ truy cập những tệp do
chính ứng dụng tạo**. Ứng dụng không đọc được các tệp khác trong Drive của bạn. Bài trình chiếu được tạo
ở chế độ riêng tư (không chia sẻ công khai).

## Bước 1 — Tạo dự án Google Cloud

1. Mở <https://console.cloud.google.com/> và đăng nhập bằng tài khoản Google sẽ chứa các bài trình chiếu.
2. Ở thanh trên cùng, chọn **Select a project → New project**, đặt tên ví dụ `Video2Slides`, bấm **Create**.

## Bước 2 — Bật 2 API

Trong dự án vừa tạo, vào **APIs & Services → Library**, tìm và bấm **Enable** cho:

- **Google Drive API**
- **Google Slides API**

## Bước 3 — Màn hình xin quyền (OAuth consent screen)

Vào **Google Auth Platform** (hoặc **APIs & Services → OAuth consent screen**):

1. **Branding**: App name `Video2Slides`, User support email và Developer contact = email của bạn → **Save**.
2. **Audience**: User type **External**. Ở mục **Test users** bấm **Add users** và thêm chính email của bạn.
3. **Data access**: **Add or remove scopes** → tích `.../auth/drive.file` (nếu không thấy, dán
   `https://www.googleapis.com/auth/drive.file` vào ô "Manually add scopes") → **Update → Save**.

> Lưu ý về thời hạn đăng nhập: khi ứng dụng ở trạng thái **Testing**, Google cho phiên đăng nhập hết hạn
> sau **7 ngày** — khi đó ứng dụng báo "quyền truy cập đã bị thu hồi hoặc hết hạn" và bạn chỉ cần bấm
> **Kết nối Google** lại. Để không phải kết nối lại mỗi tuần, vào **Audience → Publish app** (chuyển sang
> *In production*). Vì `drive.file` là quyền không nhạy cảm nên dùng cá nhân không cần Google xét duyệt;
> màn hình đăng nhập có thể hiện cảnh báo "Google chưa xác minh ứng dụng này" — bấm **Advanced → Go to
> Video2Slides** để tiếp tục (đây là ứng dụng của chính bạn).

## Bước 4 — Tạo OAuth client loại "Desktop app"

1. Vào **Google Auth Platform → Clients** (hoặc **APIs & Services → Credentials → Create credentials →
   OAuth client ID**).
2. **Application type: Desktop app**, Name: `Video2Slides local` → **Create**.
3. Bấm **Download JSON** (biểu tượng tải xuống).

Không cần khai báo "Authorized redirect URI": client loại *Desktop app* tự cho phép chuyển hướng về
`http://127.0.0.1:<cổng>` / `http://localhost:<cổng>` trên chính máy bạn, đúng với địa chỉ ứng dụng chạy.

## Bước 5 — Đặt tệp JSON vào máy (không gửi cho ai, không dán vào chat)

Đổi tên tệp vừa tải thành `client_secret.json` và chép vào:

```
D:\Notebook1\data\google\client_secret.json
```

(tạo thư mục `google` nếu chưa có). Thư mục `data/` đã được loại khỏi Git nên tệp không bị đưa lên GitHub.
Muốn để ở chỗ khác thì thêm vào `.env`: `GOOGLE_OAUTH_CLIENT_FILE=đường\dẫn\tới\client_secret.json`.

## Bước 6 — Kết nối tài khoản

1. Khởi động lại ứng dụng (`stop.bat` rồi `start.bat`) và mở **http://127.0.0.1:8000** (hoặc
   `http://localhost:8000`) — đăng nhập Google chỉ hoạt động khi mở bằng địa chỉ trên chính máy này.
2. Bấm nút **Kết nối Google** ở góc trên bên phải → chọn tài khoản → **đánh dấu quyền "Xem, chỉnh sửa, tạo
   và xóa chỉ những tệp Google Drive cụ thể mà bạn sử dụng với ứng dụng này"** → **Continue**.
3. Ứng dụng quay lại và nút góc trên hiển thị email của bạn.

Từ giờ ô **"Tự động tạo bản Google Slides gốc"** trên form được bật sẵn. Các bài đã tạo trước đó có thể
xuất sang Google Slides bằng nút **Tạo trên Google Slides** ở màn hình kết quả (dùng lại ảnh AI đã có,
không tốn GPU).

## Ngắt kết nối

Bấm vào email ở góc trên → xác nhận. Ứng dụng thu hồi quyền ở phía Google và xóa token trên máy. Các bài
đã tạo vẫn nằm trong Drive của bạn. Bạn cũng có thể thu hồi tại <https://myaccount.google.com/permissions>.

## Token được lưu ở đâu?

`data/google/token.json` (dạng JSON thường, không mã hóa). Thư mục `data/` đã được loại khỏi Git;
đừng chia sẻ tệp này — ai có nó có thể truy cập các bài do ứng dụng tạo trong Drive của bạn cho đến
khi bạn bấm ngắt kết nối. Token không bao giờ được gửi tới trình duyệt hay ghi vào log.

## Xử lý sự cố

| Thông báo | Cách xử lý |
|---|---|
| "Google chưa cấu hình" ở góc trên | Chưa có `data/google/client_secret.json` hoặc tệp không phải loại *Desktop app* |
| "Đăng nhập Google chỉ hoạt động khi mở ứng dụng tại http://127.0.0.1…" | Mở ứng dụng bằng `http://127.0.0.1:8000` thay vì tên máy / IP mạng LAN. Trên domain công khai (Modal) ứng dụng dùng luồng dán địa chỉ, xem docs/MODAL_WEB_DEPLOY_VI.md |
| "Error 403: access_denied" trên trang Google | Email chưa có trong **Test users** (Bước 3.2) hoặc chưa Publish app |
| "Google Drive API hoặc Google Slides API chưa được bật…" | Làm lại Bước 2 cho đúng dự án, đợi 1–2 phút |
| "Bạn chưa cấp quyền tạo tệp trên Google Drive…" | Kết nối lại và nhớ đánh dấu ô quyền ở Bước 6.2 |
| "Quyền truy cập Google đã bị thu hồi hoặc hết hạn" | Bấm **Kết nối Google** lại (xem lưu ý 7 ngày ở Bước 3) |
| Google đang giới hạn tần suất | Đợi vài phút rồi bấm **Thử lại** trong khung Google Slides |
