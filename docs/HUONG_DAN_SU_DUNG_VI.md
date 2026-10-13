# Video2Slides — Hướng dẫn sử dụng trên Windows

Ứng dụng chạy **trên máy tính của bạn** (không cần Replit, không cần tên miền). Video, bản ghi lời nói
và tệp PowerPoint nằm trong thư mục `data\` trên máy. Chỉ có hai dịch vụ bên ngoài:

| Bước | Chạy ở đâu | Ghi chú |
|---|---|---|
| Tách âm thanh, khung hình (FFmpeg) | Máy của bạn | |
| Nhận dạng giọng nói (faster-whisper) | Máy của bạn (CPU) | Model tải một lần vào `.cache\models` |
| Phân tích nội dung, viết dàn ý | Google Gemini (API) | Gửi **bản ghi lời nói** (văn bản), không gửi video |
| Ảnh minh họa AI (tùy chọn) | GPU worker riêng trên Modal | Chỉ gửi mô tả ảnh bằng tiếng Anh, không gửi bản ghi |
| Dựng PowerPoint, ảnh xem trước | Máy của bạn | Xem trước dùng PowerPoint đã cài trên máy |

---

## 1. Cài đặt (một lần)

Yêu cầu: Windows 10/11, kết nối mạng lần đầu, khoảng 2 GB trống.

1. Cài **Python 3.11** từ <https://www.python.org/downloads/> (tích *Add python.exe to PATH*),
   hoặc cài `uv`. Nếu máy đã có `.venv` trong thư mục dự án thì bỏ qua.
2. Nhấp đúp **`install.bat`**. Script sẽ:
   * tạo môi trường `.venv` và cài thư viện (chỉ cài phần còn thiếu),
   * kiểm tra FFmpeg, hỏi để cài bằng `winget install Gyan.FFmpeg` nếu chưa có,
   * tạo `.env` từ `.env.example` nếu chưa có (không ghi đè `.env` đang dùng),
   * tải model nhận dạng giọng nói `base` (~150 MB, chỉ lần đầu).
3. Mở `.env` bằng Notepad và điền `GEMINI_API_KEY` (mục 4).

Chạy lại `install.bat` bất cứ lúc nào cũng an toàn: không xóa dữ liệu, không tải lại model.

## 2. Mở và tắt ứng dụng

* **Mở:** nhấp đúp **`start.bat`**. Cửa sổ đen kiểm tra cấu hình, khởi động máy chủ và tự mở trình
  duyệt tại <http://127.0.0.1:8000>. Giữ cửa sổ này mở trong lúc dùng.
* **Tắt:** đóng cửa sổ đen (hoặc nhấn `Ctrl+C`), hoặc nhấp đúp **`stop.bat`**.
* Nếu ứng dụng đang chạy sẵn, `start.bat` chỉ mở lại trình duyệt.
* Ứng dụng chỉ nghe ở `127.0.0.1`, máy khác trong mạng không truy cập được.
* Cổng 8000 bận? Thêm dòng `APP_PORT=8010` vào `.env`.

## 3. Tạo bài trình chiếu

1. Kéo thả tệp **MP4** (≤ 80 MB, ≤ 10 phút — chỉnh được trong `.env`) vào ô tải lên, hoặc chọn
   tab **Dán URL** và dán liên kết YouTube / Facebook.
2. Chọn ngôn ngữ video, số slide (hoặc *Tự động đề xuất*), phong cách:
   **Professional** (doanh nghiệp), **Academic** (giáo dục, nghiên cứu), **Creative** (marketing).
3. **Bố cục slide**: mọi slide đều dùng toàn bộ khổ 16:9, không có khung giải thích trên slide.
   Phần giải thích chi tiết, ý chính cần nhớ và thời điểm trong video luôn nằm trong
   **ghi chú thuyết trình (Speaker Notes)** và trong **Chế độ đọc** trên web.
4. Bật/tắt **Google Slides** (cần kết nối Google một lần — xem mục 3b) và **Ảnh minh họa AI**.
5. Bấm **Tạo bài trình chiếu** và theo dõi từng bước. Thanh tiến độ chỉ hiện số đo thật
   (số giây âm thanh đã nhận dạng, số ảnh đã tạo xong). Có thể bấm **Hủy xử lý** bất cứ lúc nào.
6. Khi xong: **Mở trong Google Slides**, **Chế độ đọc**, **Tải PowerPoint (.pptx)**, xem bản ghi lời
   nói, hoặc **Tạo lại** từng ảnh minh họa AI chưa ưng ý.

Nếu một bước lỗi (mạng, hết hạn mức Gemini, GPU…), bấm **Thử lại**: các bước đã xong (bản ghi lời
nói, dàn ý, ảnh đã tạo) được dùng lại, không chạy lại từ đầu.

Tệp PowerPoint hoàn toàn chỉnh sửa được: tiêu đề, nội dung, sơ đồ (quy trình, dòng thời gian, thẻ,
so sánh…) là đối tượng PowerPoint thật; ghi chú cho người trình bày nằm trong phần *Notes*; màu và
font của phong cách được lưu vào slide master, nên slide bạn tự thêm cũng cùng phong cách.

### Dán liên kết YouTube / Facebook

1. Chọn tab **Dán URL**, dán liên kết (video, Shorts, youtu.be, Facebook video/Watch/Reels, fb.watch),
   bấm **Kiểm tra URL** (hoặc Enter). Ứng dụng hiện tiêu đề, kênh, thời lượng, ảnh đại diện.
2. Đánh dấu ô xác nhận bạn sở hữu hoặc được phép xử lý video, rồi bấm **Tạo bài trình chiếu**.
   Video được tải bản độ phân giải thấp (≤ 480p, đủ cho nhận dạng giọng nói và khung hình) rồi xử lý
   như tệp tải lên; tệp video được xóa sau khi xong.

Cấu hình trong `.env`: `YOUTUBE_RETRIEVAL=ytdlp` và `FACEBOOK_RETRIEVAL=ytdlp` (đã bật sẵn; yt-dlp được
cài cùng ứng dụng). Đặt `off` nếu không muốn tải từ liên kết. Facebook còn hỗ trợ `graph_api` (chỉ video
mà `FACEBOOK_ACCESS_TOKEN` có quyền đọc, ví dụ Page của bạn).

Lưu ý: điều khoản của YouTube/Facebook có thể không cho phép tải video bằng công cụ bên thứ ba — chỉ dùng
cho nội dung bạn sở hữu hoặc được phép. Ứng dụng không vượt qua đăng nhập, video riêng tư, giới hạn độ
tuổi, livestream hay DRM; những video đó sẽ báo lỗi kèm gợi ý tải tệp MP4 lên thay thế.

## 3b. Google Slides và Chế độ đọc

* **Google Slides**: làm theo [GOOGLE_SLIDES_SETUP_VI.md](GOOGLE_SLIDES_SETUP_VI.md) một lần, rồi bấm
  **Kết nối Google** ở góc trên. Mỗi bài được tạo thành tệp Google Slides gốc (chỉnh sửa được) trong Drive
  của bạn, riêng tư, kèm Speaker Notes đã được kiểm tra lại từng slide. Bài cũ: bấm **Tạo trên Google
  Slides** ở màn hình kết quả (dùng lại ảnh AI có sẵn, không tốn GPU). Ứng dụng **không bao giờ ghi đè**
  một bài Google Slides đã có: khi bài được dựng lại (ví dụ tạo lại ảnh), bấm **Tạo bản Google Slides
  mới** nếu muốn.
* **Chế độ đọc**: slide bên trái (~65%), phần giải thích bên phải (~35%): ý chính, giải thích chi tiết,
  ví dụ, điều cần nhớ, thời điểm trong video gốc và trích lời giảng. Chuyển slide bằng nút ‹ ›, ảnh nhỏ,
  phím ← → / Home / End, vuốt trên điện thoại; **F** = toàn màn hình. Phần giải thích đổi ngay theo slide.
  Trên điện thoại, slide nằm trên, phần giải thích nằm dưới.
* **Kiểm tra đồng bộ** (trong Chế độ đọc): cho biết bạn đã xóa/thêm/đổi thứ tự slide hay sửa ghi chú trong
  Google Slides. Chế độ đọc vẫn hiển thị nội dung gốc do AI tạo; chỉnh sửa trong Google Slides không tự
  cập nhật ngược về ứng dụng.
* **Bài đã tạo** (góc trên): mở lại các bài trước ở Chế độ đọc, Google Slides hoặc tải lại PowerPoint.

## 4. Lấy khóa Gemini (bắt buộc để có dàn ý chất lượng)

1. Mở <https://aistudio.google.com/apikey>, đăng nhập Google, bấm **Create API key**.
2. Mở `.env`, điền: `GEMINI_API_KEY=khóa_của_bạn` (không dấu cách, không ngoặc kép).
3. Tắt và mở lại `start.bat`. Góc phải trên đổi từ *AI: offline* sang *AI: Gemini*.

Mỗi video dùng 1 yêu cầu Gemini (tối đa thêm 2 lần sửa lỗi/thử lại). Không có khóa, ứng dụng vẫn
chạy nhưng dàn ý chỉ là trích câu từ bản ghi (chế độ offline).

## 5. Ảnh minh họa AI (Modal GPU)

GPU worker SDXL đã được triển khai sẵn trên Modal (`video2slides-image-worker`). Cách cài đặt từ đầu,
đặt giới hạn chi tiêu và kill switch: [AI_ILLUSTRATIONS_SETUP_VI.md](AI_ILLUSTRATIONS_SETUP_VI.md)
(các bước "Replit Secrets" ở đó tương ứng với tệp `.env` khi chạy trên máy).

Các biến trong `.env`: `AI_IMAGES_ENABLED=true`, `IMAGE_PROVIDER=modal_sdxl`,
`MODAL_IMAGE_WORKER_URL`, `MODAL_PROXY_TOKEN_ID`, `MODAL_PROXY_TOKEN_SECRET`.

Kiểm tra kết nối **không tốn GPU**:

```
.venv\Scripts\python scripts\check_worker_auth.py
```

### Chi phí tham khảo (L4, giá niêm yết ~0,000222 USD/giây — kiểm tra lại tại modal.com/pricing)

| Tình huống | Thời gian GPU đo được | Ước tính |
|---|---|---|
| 1 ảnh, GPU đang nguội (cold start) | ~77 s + 120 s chờ tắt | ~0,04 USD |
| 3 ảnh, GPU đang ấm | ~43 s + 120 s chờ tắt | ~0,04 USD |
| 1 bài 8 ảnh (cold start) | ~77 s + 7 × ~12 s + 120 s | ~0,06 USD |

Đây là ước tính của ứng dụng; số tiền thực tế xem tại Modal dashboard → **Usage**. Ứng dụng giới hạn
tối đa 8 ảnh/bài, 40 ảnh/ngày, 300 ảnh/tháng, 5 USD/tháng (đổi trong `.env`); GPU tự tắt sau 2 phút
không dùng và không bao giờ chạy quá 1 GPU cùng lúc.

### Đổi (xoay vòng) proxy token khi nghi bị lộ

Proxy token chỉ gọi được web endpoint của worker (không deploy, không chạy code), nhưng ai có nó vẫn có
thể dùng GPU của bạn. Cách đổi an toàn, không gián đoạn:

1. Modal dashboard → **Settings → Proxy Auth Tokens → New token**. Sao chép Token ID (`wk-…`) và
   Secret (`ws-…`) — Secret chỉ hiện một lần.
2. Mở `.env`: đổi tên hai dòng cũ thành `OLD_MODAL_PROXY_TOKEN_ID=…` và
   `OLD_MODAL_PROXY_TOKEN_SECRET=…`, rồi thêm hai dòng `MODAL_PROXY_TOKEN_ID` /
   `MODAL_PROXY_TOKEN_SECRET` với giá trị mới.
3. Chạy `scripts\check_worker_auth.py` → token hiện tại phải là `HTTP 200`.
4. Trên dashboard, **xóa token cũ**.
5. Chạy lại script → token cũ phải là `HTTP 401`. Sau đó xóa hai dòng `OLD_…` khỏi `.env`.
6. Khởi động lại `start.bat`.

Không dán khóa/token vào chat, email hay tài liệu. `.env` đã nằm trong `.gitignore`.

## 6. Biến cấu hình thường dùng (`.env`)

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `GEMINI_API_KEY` | trống | Khóa Gemini (mục 4) |
| `GEMINI_MODEL` | `gemini-flash-latest` | Model Gemini |
| `WHISPER_MODEL` | `base` | `tiny` nhanh hơn, `small` chính xác hơn (chậm ~2,5 lần) |
| `DEFAULT_LANGUAGE` | `vi` | `vi`, `en`, `auto` |
| `AUDIO_NORMALIZE` | `true` | Chuẩn hóa âm lượng trước khi nhận dạng |
| `MAX_UPLOAD_MB` / `MAX_DURATION_SEC` | `80` / `600` | Giới hạn video |
| `APP_PORT` / `APP_HOST` | `8000` / `127.0.0.1` | Cổng và địa chỉ của ứng dụng |
| `PREVIEW_ENGINE` | tự động | `off` để tắt ảnh xem trước |
| `JOB_TTL_HOURS` | `24` | Tự xóa công việc cũ sau số giờ này |
| `AI_IMAGES_ENABLED` | `false` | Bật ảnh minh họa AI |
| `IMAGE_MAX_PER_PRESENTATION` | `8` | Số ảnh AI tối đa mỗi bài |
| `IMAGE_MONTHLY_BUDGET_USD` | `5` | Ngân sách GPU ước tính mỗi tháng |
| `IMAGE_ON_FAILURE` | `fail` | `skip` = vẫn xuất bài khi một ảnh lỗi (kèm thông báo) |
| `APP_PASSWORD` | trống | Mật khẩu chung (chỉ cần nếu mở cho người khác dùng) |

Danh sách đầy đủ có chú thích: `.env.example`.

## 7. Xử lý sự cố

| Hiện tượng | Cách xử lý |
|---|---|
| `start.bat` báo *Chưa cài đặt* | Chạy `install.bat` |
| *Không tìm thấy FFmpeg* | `winget install Gyan.FFmpeg`, đóng và mở lại cửa sổ |
| *Cổng 8000 đang bị chương trình khác dùng* | Thêm `APP_PORT=8010` vào `.env`, hoặc tắt chương trình đó |
| Góc phải hiện *AI: offline* | Chưa có/sai `GEMINI_API_KEY` trong `.env`; sửa rồi mở lại `start.bat` |
| *GEMINI_API_KEY không hợp lệ* | Tạo khóa mới ở AI Studio |
| *Gemini tạm thời không khả dụng (HTTP 429)* | Hết hạn mức miễn phí; chờ rồi bấm **Thử lại** |
| *Token truy cập GPU worker không hợp lệ* | Token Modal sai/đã xóa; xem mục 5, chạy `check_worker_auth.py` |
| *Quá thời gian chờ GPU tạo ảnh* | Bấm **Thử lại** (ảnh đã xong được giữ), hoặc tắt ảnh minh họa AI |
| *Đã đạt giới hạn… / hết ngân sách GPU* | Chờ sang ngày/tháng mới hoặc tăng giới hạn trong `.env` |
| *Video không có âm thanh* | Ứng dụng cần lời nói để tạo nội dung |
| *Không phát hiện được lời nói* | Âm thanh quá nhỏ/nhiễu hoặc sai ngôn ngữ; thử `Tự động nhận diện` |
| Phần xem trước báo không tạo được ảnh | Cần Microsoft PowerPoint (hoặc LibreOffice + `pypdfium2`); tệp .pptx vẫn tải được |
| Nhận dạng sai nhiều từ | Dùng `WHISPER_MODEL=small` (cần tải model một lần) |
| Ứng dụng bị tắt giữa chừng | Mở lại, mở công việc và bấm **Thử lại** |

Nhật ký chi tiết hiện trong cửa sổ đen của `start.bat`. Khóa API không bao giờ được ghi vào nhật ký.

## 8. Giới hạn hiện tại

* Chỉ nhận MP4; mỗi lần xử lý một video (các video khác xếp hàng, tối đa 3).
* Nhận dạng chạy trên CPU: video 10 phút với model `base` mất khoảng 2–4 phút.
* Tên riêng, thuật ngữ và số đọc nhanh có thể bị nghe sai; Gemini chỉ sửa khi lỗi rõ ràng.
* Ảnh AI không chứa chữ/số chính xác; số liệu, bảng và sơ đồ luôn là đối tượng PowerPoint chỉnh sửa được.
* Ảnh xem trước cần PowerPoint (hoặc LibreOffice) trên máy.
* Sổ theo dõi chi phí GPU nằm trên máy và là ước tính; luôn đặt thêm giới hạn chi tiêu trên Modal.
