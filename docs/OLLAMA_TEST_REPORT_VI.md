# Báo cáo kiểm thử: Ollama `qwen3:4b` chạy trên máy (phiên tích hợp ngày 13/10/2026)

> **Phạm vi và trạng thái mã.** Báo cáo này ghi lại kết quả của provider Ollama **chạy trên máy**
> (`app/ai/ollama.py`, `ollama_client.py`, `ollama_prompts.py`) tại thời điểm kiểm thử, khi `LLM_PROVIDER`
> mặc định là `ollama`. Sau đó một phiên làm việc khác đã đổi cách định tuyến provider (Gemini trước,
> Ollama trên Modal làm dự phòng) và sửa thêm `pipeline.py`, `ollama.py`, `health.py`, `config.py`.
> Các số liệu dưới đây **không** áp dụng cho cách định tuyến mới đó và cần được kiểm thử lại cho cấu hình hiện tại.

## 1. Môi trường thật

| Hạng mục | Giá trị |
|---|---|
| Máy | Windows 11, Intel Core i9-13900H (14 nhân / 20 luồng), RAM 16 GB, Intel Iris Xe (Ollama không dùng → chạy CPU) |
| RAM trống khi đo | 0,25–3,7 GB (Android Studio, nhiều tab Chrome, Claude đang mở); hệ thống thường xuyên dùng bộ nhớ ảo |
| Ollama | 0.40.3, chỉ nghe tại `127.0.0.1:11434` |
| Model | `qwen3:4b`, digest `359d7dd4bcda`, 4.0B, Q4_K_M, tải 2,5 GB; khả năng `completion, tools, thinking`; ngữ cảnh tối đa 262144 |
| RAM model khi nạp | 3705 MB với `num_ctx=8192` (`/api/ps`, `size_vram = 0`) |
| Whisper | `base`, CPU int8 |

## 2. Thinking của Qwen3 (đo thật, cùng một yêu cầu tóm tắt tiếng Việt, `num_predict=1500`)

| `think` | Thời gian | Token đầu ra | Kết quả |
|---|---|---|---|
| `false` | 123,6 s (gồm 26 s nạp model lần đầu) | 415 | JSON hợp lệ, 5,2 token/s |
| `true` | 312 s | 1500 (hết ngân sách) | Toàn bộ là phần suy luận (5869 ký tự), **không có JSON**, `done_reason=length` |

→ Mặc định `OLLAMA_THINK=auto` gửi `think: false` khi model báo có khả năng thinking.
Tỷ lệ đo được với tiếng Việt: ≈ 3,4–3,5 ký tự/token (ứng dụng ước lượng 3,0 để an toàn).

## 3. E2E thật qua ứng dụng web

Video `data/fixtures/lecture_vi.mp4` (90 giây, giọng đọc TTS vi-VN, tạo bằng `scripts/make_test_video.ps1`);
6 slide, phong cách Professional, ảnh minh họa Modal (mật độ chuẩn → 2 ảnh), xuất Google Slides.
Chuỗi: Video → FFmpeg → Faster-Whisper → Ollama qwen3:4b → Modal SDXL → PPTX + Google Slides → Xem trước & tải về.

| Giai đoạn | Thời gian đo |
|---|---|
| Tách âm thanh & khung hình (FFmpeg) | 5,3 s |
| Nhận dạng giọng nói (Whisper base) | 28,0 s |
| Phân tích + dàn ý (1 yêu cầu LLM, gồm nạp model 10,1 s) | 135,0 s |
| Viết 6 slide + ghi chú (2 yêu cầu × 3 slide) | 622,5 s |
| Ảnh minh họa Modal SDXL (2 ảnh) | 51,3 s (GPU 23,7 s) |
| Dựng PPTX + kiểm tra + ảnh xem trước | ≈ 2,4 s |
| Xuất Google Slides + kiểm tra ghi chú | 7,7 s |
| **Tổng** | **852 s (14 phút 12 giây)** |

* LLM: 3 yêu cầu, **0 lần sửa**, 4136 token vào / 3136 token ra, trung bình 4,97 token/s.
* RAM trống trước bước LLM 3448 MB, sau 1251 MB; tiến trình ứng dụng đỉnh 375 MB.
* Kết quả: 6 slide, 3 ảnh (2 ảnh AI + 1 khung hình video), tải `.pptx` HTTP 200 (363 KB, 16:9).
* PPTX (đọc bằng python-pptx): chữ là text box chỉnh sửa được; mỗi slide có ghi chú đúng nội dung của nó.
* Google Slides: 6/6 ghi chú đã được kiểm tra lại qua Slides API (`verified 6, fixed 0`); ảnh thumbnail
  cho thấy slide dùng toàn khổ, không có khung giải thích; ảnh AI có nhãn "Ảnh minh họa tạo bởi AI".
* Chế độ đọc (kiểm tra trong trình duyệt, cửa sổ 1366 px và ~675 px): slide rộng 1180 px, phần ghi chú nằm
  **bên dưới** slide, cùng chiều rộng; nút Next chuyển slide 5 → 6, bộ đếm "Slide 6 / 6", ghi chú đổi theo.
* Tiến độ trên giao diện chỉ dùng số đếm thật: "Đã viết 0/6 slide" → "3/6" → xong.

### Chi phí Modal

Ước tính của ứng dụng cho lần chạy này: **0,0475 USD** (23,7 s GPU L4 + phí chờ tắt theo mô hình ước tính).
Đây là ước tính, **không phải số tiền bị tính**; số thực tế xem tại Modal dashboard → Usage.
Bước LLM chạy trên máy: không có phí API theo lượt, nhưng dùng CPU/RAM của máy.

## 4. Đánh giá chất lượng nội dung tiếng Việt (bài E2E)

Người kiểm tra đọc toàn bộ 6 slide, ghi chú và so với bản ghi lời nói:

**Đạt**

* Tiếng Việt tự nhiên; tóm tắt và sắp xếp lại, không chép nguyên bản ghi; chữ trên slide ngắn, giải thích
  dài nằm trong ghi chú.
* Giữ đúng số liệu và điều kiện: 200 sinh viên, khoảng 60%, 2 giờ/ngày, 4 bước, 10 phút, 15–30 phút.
* Tự sửa đúng một số lỗi nhận dạng: "ký thuận hộ mò đòn rộ" → *Pomodoro*, "2,15 phút" → *25 phút*.
* Mốc thời gian nguồn nằm trong video và khớp nội dung (ví dụ slide Pomodoro 50–58 s).
* Ghi chú khớp đúng slide (cả trong PPTX, Google Slides và Chế độ đọc).
* Prompt ảnh bằng tiếng Anh; ảnh được ghi rõ là minh họa AI.

**Chưa đạt / cần người dùng đọc lại**

* Một số lỗi nhận dạng bị "sửa" sai nghĩa: "trì hoãn" → *phân tán*; "mục tiêu" → *tiêu chí*;
  "trong tuần" → *trong ngày*. Một lỗi được giữ nguyên ("chảy nước dút") và còn được giải thích sai nghĩa.
* Vài câu giải thích thêm ý không có trong video (ví dụ "đặc biệt phù hợp cho … nghiên cứu").
* Bài 6 slide không có slide tổng kết (đã thêm kiểm tra slide cuối, xem mục 6).
* Slide image_left chỉ có 1 gạch đầu dòng; Speaker Notes thường chỉ 1–2 câu (yêu cầu 2–5).
* Prompt ảnh slide 5 có "timer showing 25 minutes" (quy tắc là không yêu cầu chữ/số trong ảnh).

Nguyên nhân chính là bản ghi `base` của giọng TTS có nhiều lỗi; `WHISPER_MODEL=small` cho bản ghi tốt hơn
(xem README) nhưng chưa được đo lại với Ollama. **JSON hợp lệ không chứng minh nội dung đúng.**

## 5. Transcript dài (model thật, buộc đi đường chia đoạn)

Ghép 2 bản ghi thật (199 s về AI + 90 s về quản lý thời gian = 290,7 s, 63 đoạn, ~4300 ký tự);
`OLLAMA_NUM_CTX=3584` để transcript không vừa một lần; `scripts/ollama_live_check.py … --slides 6`.

* Chia thành 2 phần tại khoảng lặng → 2 bản tóm tắt có mốc thời gian (116 s mỗi phần) → dàn ý.
* 13 yêu cầu, **20 lần dùng lại cache** (các bản tóm tắt dùng lại khi viết slide), 3 lần sửa, 0 lần chia đôi.
* Tổng thời gian LLM 1528,8 s (25,5 phút), nạp model 7,1 s, trung bình 5,45 token/s.
* Kết quả 6 slide hợp lệ, có slide tổng kết; mốc thời gian nằm trong 0–290,7 s.
* Không cắt bớt transcript: mọi đoạn đều được đưa vào đúng một phần tóm tắt (cũng được kiểm bằng test tự động).
* Chất lượng: vì ghép hai chủ đề không liên quan nên bài bị trộn chủ đề (giới hạn của phép thử); lỗi nhận dạng
  tên riêng lan sang slide ("Dorrmitz", "Emma Carty", "Shoppy").
* Sự cố thật được ghi lại: slide image_left trả `key_points: []` ở lần đầu → sửa có mục tiêu thành công ở lần 2.

## 6. Sự cố phát hiện khi chạy thật và cách đã xử lý

| Lần chạy | Sự cố | Xử lý |
|---|---|---|
| Thử provider lần 1 (6 slide) | slide-05 (image_left) 3 lần liền không có gạch đầu dòng → job dừng đúng giới hạn sửa | Prompt nói rõ image_left/right vẫn cần `key_points`; nếu model đặt ý vào cột trái/phải thì gộp lại; ở lần thử cuối dùng tóm tắt/điểm cần nhớ do chính model viết cho slide đó (kèm cảnh báo) |
| E2E | Dàn ý 6 slide không có slide kết | Kiểm tra slide cuối là summary/conclusion (sửa tối đa 2 lần, lần cuối chấp nhận kèm cảnh báo) |
| Transcript dài | Kiểm tra slide kết bị từ chối 2 lần, tốn thêm ~4 phút cho dàn ý; lần 3 model mới đặt slide tổng kết | Ghi nhận: quy tắc này có thể tốn thời gian trên CPU chậm; nên xem xét chỉ cảnh báo |
| E2E | Slide tiêu đề không có phụ đề | Dùng phụ đề của bài (do model viết ở bước dàn ý) khi trống |

## 7. Kiểm thử lỗi với Ollama thật (không mock)

| Tình huống | Kết quả |
|---|---|
| Model không tồn tại (`/api/chat`, `/api/show`) | `model_missing` sau 0,2 s, hướng dẫn `ollama pull` |
| Sai cổng (127.0.0.1:11999) | `ollama_unavailable` sau 2,1 s, hướng dẫn mở Ollama |
| Hết thời gian (giới hạn 1 s) | `ollama_timeout` sau 1,0 s |
| Hủy giữa lúc đang sinh (sau 8 s) | Dừng sau 8,1 s; yêu cầu kế tiếp được phục vụ ngay (prompt eval 0,1 s) → Ollama đã ngừng sinh |
| `/api/llm/status` | `ready`, phiên bản, model, khả năng, RAM trống |

## 8. Kiểm thử tự động (mock — không phải tích hợp thật)

`tests/test_ollama.py` dùng máy chủ Ollama **giả lập** `tests/fake_ollama.py`: ánh xạ thiết lập sang
`/api/chat` (`options.num_ctx`, `temperature`, `num_predict`, `keep_alive`, `think`, `format`, `stream`),
thinking tách riêng, transcript là dữ liệu (chống chèn lệnh), số slide cố định/tự động, prompt ảnh chỉ cho slide
được chọn, tiến độ là số đếm thật, chia transcript không mất đoạn, JSON lỗi → sửa, sửa có giới hạn (tối đa 3
yêu cầu), sửa có mục tiêu chỉ slide thiếu, thiếu ghi chú → `missing_notes`, câu trả lời bị cắt → chia đôi,
mốc thời gian ngoài video, cache dùng lại/vô hiệu khi đổi model, phiên bản prompt hoặc đầu vào, checkpoint giữa
chừng, Ollama không chạy, thiếu model, timeout, thiếu RAM, hủy, trạng thái sức khỏe.
`tests/test_failures.py`: lỗi Ollama ở cấp pipeline → `error_code`, thử lại **không** chạy lại Whisper,
tiếp tục từ checkpoint (chỉ viết lại lô bị lỗi), có giai đoạn `writing_slides`.
`tests/test_api.py`: ứng dụng khởi động không cần khóa Gemini. `tests/test_frontend.py`: ghi chú nằm dưới slide.

Lần chạy toàn bộ cuối cùng trong phiên (trước khi phiên khác sửa mã): 192 test pass (+ các test thêm sau đó
pass riêng lẻ). Bộ test **chưa được chạy lại** trên mã hiện tại sau các thay đổi của phiên khác.

## 9. Chưa kiểm thử / giới hạn

* `install.bat` / `start.bat` chưa chạy trọn vẹn trên máy sạch (chỉ kiểm cú pháp PowerShell và chạy thử các hàm
  kiểm tra Ollama trên máy này).
* Chưa đo tốc độ khi máy có nhiều RAM trống; các số ở trên đo khi máy đang thiếu RAM.
* Chưa đo `WHISPER_MODEL=small` kết hợp Ollama; chưa thử video tiếng Việt thật (người nói thật) dài 10 phút.
* So sánh với Gemini: **chưa đo**, không có kết luận nhanh hơn hay tốt hơn.
