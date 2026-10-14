# Video2Slides — Hướng dẫn sử dụng trên Windows

Ứng dụng chạy **trên máy tính của bạn** (không cần Replit, không cần tên miền). Video, bản ghi lời nói
và tệp PowerPoint nằm trong thư mục `data\` trên máy. Chỉ có hai dịch vụ bên ngoài:

| Bước | Chạy ở đâu | Ghi chú |
|---|---|---|
| Tách âm thanh, khung hình (FFmpeg) | Container CPU riêng trên Modal (hoặc máy của bạn với `MEDIA_PROCESSING=local`) | Video tải lên kho riêng, xóa sau khi xong |
| Nhận dạng giọng nói (faster-whisper) | GPU L4 trên Modal (hoặc CPU máy của bạn) | Không cần cài Whisper trên máy |
| Phân tích nội dung, viết slide và ghi chú | **Gemini** nếu có khóa; nếu không có khóa hoặc Gemini lỗi thì **Ollama `qwen3:4b` trên GPU Modal** | Tự động chuyển, không cần thao tác |
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
3. Mặc định **không cần** cài Ollama, FFmpeg hay tải model AI về máy: quản trị viên triển khai các worker trên
   Modal theo [MODAL_CLOUD_SETUP_VI.md](MODAL_CLOUD_SETUP_VI.md) và điền `MODAL_LLM_WORKER_URL`,
   `MODAL_MEDIA_WORKER_URL`, `MODAL_PROXY_TOKEN_*` vào `.env`. Chỉ khi chọn `LLM_PROVIDER=ollama` script mới
   hỏi cài Ollama trên máy ([OLLAMA_SETUP_VI.md](OLLAMA_SETUP_VI.md)).

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
6. Khi xong: **Mở trong Google Slides**, **Chế độ đọc**, **Tải PowerPoint (.pptx)**, **Tải cho Figma (SVG)**, **Tải PDF**, xem bản ghi lời
   nói, hoặc **Tạo lại** từng ảnh minh họa AI chưa ưng ý.

Nếu một bước lỗi (Ollama chưa mở, thiếu RAM, mạng, GPU…), bấm **Thử lại**: các bước đã xong (bản ghi lời
nói, dàn ý, ảnh đã tạo) được dùng lại, không chạy lại từ đầu.

Tệp PowerPoint hoàn toàn chỉnh sửa được: tiêu đề, nội dung, sơ đồ (quy trình, dòng thời gian, thẻ,
so sánh…) là đối tượng PowerPoint thật; ghi chú cho người trình bày nằm trong phần *Notes*; màu và
font của phong cách được lưu vào slide master, nên slide bạn tự thêm cũng cùng phong cách.

**Đưa slide vào Figma**: Figma Design không mở được tệp PDF, nên app xuất **SVG**, định dạng Figma nhận
thành layer sửa được (chữ là text thật, hình là vector, ảnh giữ nguyên).
- **Tải cho Figma (SVG)**: tệp .zip, mỗi slide một tệp `slide-01.svg`, `slide-02.svg`… Giải nén, chọn tất
  cả tệp SVG rồi kéo thả vào Figma: mỗi slide thành một frame 960×540.
- **Sao chép slide cho Figma** (ở khung Xem trước): sao chép slide đang xem, mở Figma và bấm Ctrl+V.
- **Sao chép tất cả slide**: sao chép cả bộ, xếp thành lưới 4 cột, mỗi slide một nhóm `Slide-01`, `Slide-02`…
  Dán vào Figma bằng Ctrl+V. Muốn mỗi slide thành frame riêng: chọn các nhóm rồi bấm Ctrl+Alt+G (*Frame selection*).
- Nếu máy chưa cài font của chủ đề (Calibri, Cambria, Segoe UI), Figma sẽ báo thiếu font và cho chọn font
  thay thế. Muốn làm cả bộ trong **Figma Slides**, có thể nhập thẳng tệp .pptx (Figma đổi font sang Inter).
- Lần đầu mất vài giây để xuất; các lần sau dùng lại bản đã xuất cho tới khi slide được sửa.

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
* **Chế độ đọc**: slide hiển thị toàn khổ ở trên; phần giải thích nằm **bên dưới slide**: ý chính, giải
  thích chi tiết, ví dụ, điều cần nhớ, thời điểm trong video gốc và trích lời giảng. Chuyển slide bằng nút
  ‹ ›, ảnh nhỏ, phím ← → / Home / End, vuốt trên điện thoại; **F** = toàn màn hình. Phần giải thích đổi
  ngay theo slide. Trên slide không có khung giải thích — nội dung dài nằm trong ghi chú.
* **Kiểm tra đồng bộ** (trong Chế độ đọc): cho biết bạn đã xóa/thêm/đổi thứ tự slide hay sửa ghi chú trong
  Google Slides. Chế độ đọc vẫn hiển thị nội dung gốc do AI tạo; chỉnh sửa trong Google Slides không tự
  cập nhật ngược về ứng dụng.
* **Bài đã tạo** (góc trên): mở lại các bài trước ở Chế độ đọc, Google Slides hoặc tải lại PowerPoint.

## 3c. Trích dẫn, Studio học tập và sửa slide bằng câu lệnh

Cả ba tính năng **không làm chậm việc tạo bài**: chúng chỉ chạy khi bạn dùng, trên luồng riêng của máy chủ
(không nằm trong hàng đợi tạo bài). Khi một bài khác đang ở bước AI, tác vụ phụ tự chờ để bài đó được ưu tiên.

* **Trích dẫn bấm được** (Chế độ đọc): mục *Nội dung trên slide & nguồn* gắn từng ý trên slide với thời điểm
  được nói trong video (so khớp với bản ghi lời nói, không dùng AI). Ý nào không tìm được đoạn khớp đủ chắc
  thì không gắn mốc, thay vì gắn sai. Bấm vào mốc thời gian:
  * video tải lên / Facebook: phát ngay trong ứng dụng đúng đoạn đó. Video gốc được giữ
    `KEEP_SOURCE_VIDEO_HOURS` giờ (mặc định 24) sau khi xong rồi tự xóa; `0` = xóa ngay như trước;
  * video YouTube: mở YouTube đúng giây đó.
* **Studio học tập** (nút *Studio* trong Chế độ đọc hoặc *Studio học tập* ở màn hình kết quả):
  * *Sơ đồ tư duy* và *Dòng thời gian*: có ngay, dựng từ dàn ý, không gọi AI;
  * *Bản tóm tắt*, *Hỏi đáp (FAQ)*, *Câu hỏi trắc nghiệm*, *Thẻ ghi nhớ*: bấm **Tạo** thì AI mới viết (thường
    dưới 1 phút, lâu hơn khi GPU đang khởi động). Mỗi mục có chip *Slide n · mm:ss* để mở đúng slide. Khi bài
    được sửa sau đó, nội dung cũ được đánh dấu và có nút **Tạo lại theo bản mới**.
* **Sửa slide bằng câu lệnh** (cuối phần giải thích của mỗi slide): gõ yêu cầu (ví dụ "tách thành 2 ý", "thêm
  ví dụ trong video", "đổi sang dạng quy trình"), bấm **Thêm vào danh sách sửa**, làm tiếp với các slide khác
  (tối đa 6 slide mỗi lần), rồi bấm **Áp dụng** một lần để chỉ chờ GPU khởi động một lần. AI viết lại đúng các
  slide đó với cùng quy tắc bám nội dung video như lúc tạo bài; tệp PowerPoint được dựng lại (không chạy lại
  FFmpeg, Whisper hay ảnh AI). **Hoàn tác lần sửa gần nhất** trả lại bản trước (giữ tối đa 10 bản).
  Bản Google Slides đã tạo không bao giờ bị ghi đè: nó được đánh dấu là cũ, bạn có thể tạo bản mới.

## 4. AI viết nội dung: mặc định chỉ Ollama trên Modal (`LLM_PROVIDER=modal_ollama`); `auto` = Gemini trước, Ollama dự phòng

* Có `GEMINI_API_KEY`: dùng Gemini trước. Khi Gemini không dùng được (khóa sai, hết hạn mức, quá tải, quá
  thời gian, trả kết quả lỗi sau 2 lần sửa), ứng dụng **tự chuyển** sang Ollama `qwen3:4b` trên GPU của Modal
  và hiện: *“Gemini không khả dụng, đang chuyển sang Ollama trên Modal.”*
* Không có khóa: dùng ngay Ollama trên Modal. Lần đầu có thể thấy *“Đang khởi động mô hình trên GPU.”*
* Nếu Gemini từ chối vì lý do an toàn, ứng dụng dừng và báo lỗi (không dùng mô hình khác để vượt qua).
* Kết quả ghi rõ mô hình đã dùng và lý do chuyển. Google Slides không phụ thuộc vào khóa Gemini.
* Chất lượng Qwen3 4B so với Gemini chưa được đánh giá; hãy xem lại nội dung trước khi trình bày.

### Tùy chọn: Ollama trên máy (`LLM_PROVIDER=ollama`)

Model `qwen3:4b` chạy bằng Ollama ngay trên máy: dùng CPU/RAM (khoảng 3,7 GB RAM khi model đang nạp).
Góc phải trên hiện *AI: qwen3:4b (Ollama, trên máy)*; nếu *chưa sẵn sàng*, rê chuột để xem lý do.

* Ollama tự giải phóng RAM sau 5 phút không dùng (`OLLAMA_KEEP_ALIVE`).
* Mỗi bài cần vài phút đến vài chục phút cho bước AI, tùy CPU và RAM trống (xem
  [OLLAMA_TEST_REPORT_VI.md](OLLAMA_TEST_REPORT_VI.md)). Đóng bớt ứng dụng nặng sẽ nhanh hơn.
* Chỉ dùng Gemini (không dự phòng): `LLM_PROVIDER=gemini`.

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
| `LLM_PROVIDER` | `modal_ollama` | `modal_ollama` (chỉ Ollama trên Modal, mặc định), `auto` (Gemini trước, Ollama trên Modal dự phòng), `gemini`, `modal_ollama`, `ollama` (trên máy, chọn rõ ràng), `offline` |
| `MEDIA_PROCESSING` | `auto` | `auto` (Modal khi có `MODAL_MEDIA_WORKER_URL`), `modal`, `local` |
| `OLLAMA_MODEL` | `qwen3:4b` | Model Ollama (không tự nâng lên 8B/14B) |
| `OLLAMA_NUM_CTX` | `8192` | Cửa sổ ngữ cảnh; tăng = tốn thêm RAM |
| `OLLAMA_KEEP_ALIVE` | `5m` | Thời gian giữ model trong RAM sau lần dùng cuối |
| `OLLAMA_REQUEST_TIMEOUT_SECONDS` | `600` | Thời gian chờ tối đa mỗi yêu cầu |
| `GEMINI_API_KEY` | trống | Có khóa: Gemini được dùng trước; trống: dùng Ollama trên Modal |
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
| *Không kết nối được Ollama* | Mở ứng dụng Ollama (biểu tượng lạc đà ở khay hệ thống) rồi bấm **Thử lại** |
| *Ollama chưa có model 'qwen3:4b'* | Chạy `ollama pull qwen3:4b` hoặc chạy lại `install.bat` |
| *Không đủ RAM để chạy model* / bước AI rất chậm | Đóng bớt ứng dụng nặng (trình duyệt nhiều tab, Android Studio…), rồi **Thử lại** |
| *Ollama không trả lời trong 600 giây* | Máy quá tải; đóng bớt ứng dụng, hoặc tăng `OLLAMA_REQUEST_TIMEOUT_SECONDS` |
| *…chưa tạo được … hợp lệ sau 3 lần thử* | Bấm **Thử lại** (phần đã xong được giữ); hoặc giảm số slide |
| *GEMINI_API_KEY không hợp lệ* (chỉ khi `LLM_PROVIDER=gemini`) | Tạo khóa mới ở AI Studio |
| *Token truy cập GPU worker không hợp lệ* | Token Modal sai/đã xóa; xem mục 5, chạy `check_worker_auth.py` |
| *Quá thời gian chờ GPU tạo ảnh* | Bấm **Thử lại** (ảnh đã xong được giữ), hoặc tắt ảnh minh họa AI |
| *Đã đạt giới hạn… / hết ngân sách GPU* | Chờ sang ngày/tháng mới hoặc tăng giới hạn trong `.env` |
| *Video không có âm thanh* | Ứng dụng cần lời nói để tạo nội dung |
| *Không phát hiện được lời nói* | Âm thanh quá nhỏ/nhiễu hoặc sai ngôn ngữ; thử `Tự động nhận diện` |
| Phần xem trước báo không tạo được ảnh | Cần Microsoft PowerPoint (hoặc LibreOffice + `pypdfium2`); tệp .pptx vẫn tải được |
| Không tải được PDF / SVG cho Figma | Cũng cần PowerPoint hoặc LibreOffice như ảnh xem trước (SVG cần thêm PyMuPDF) |
| Nút *Sao chép slide cho Figma* báo bị chặn | Trình duyệt không cho ghi clipboard; dùng *Tải cho Figma (SVG)* rồi kéo thả |
| Nhận dạng sai nhiều từ | Dùng `WHISPER_MODEL=small` (cần tải model một lần) |
| Ứng dụng bị tắt giữa chừng | Mở lại, mở công việc và bấm **Thử lại** |

Nhật ký chi tiết hiện trong cửa sổ đen của `start.bat`. Khóa API không bao giờ được ghi vào nhật ký.

## 8. Giới hạn hiện tại

* Chỉ nhận MP4; mỗi lần xử lý một video (các video khác xếp hàng, tối đa 3).
* Nhận dạng chạy trên CPU: video 10 phút với model `base` mất khoảng 2–4 phút.
* Tên riêng, thuật ngữ và số đọc nhanh có thể bị nghe sai; model AI chỉ sửa khi lỗi rõ ràng.
* `qwen3:4b` là model nhỏ: JSON hợp lệ không có nghĩa nội dung luôn chính xác — hãy đọc lại slide trước khi dùng.
* Ảnh AI không chứa chữ/số chính xác; số liệu, bảng và sơ đồ luôn là đối tượng PowerPoint chỉnh sửa được.
* Ảnh xem trước cần PowerPoint (hoặc LibreOffice) trên máy.
* Sổ theo dõi chi phí GPU nằm trên máy và là ước tính; luôn đặt thêm giới hạn chi tiêu trên Modal.
