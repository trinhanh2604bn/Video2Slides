# Xử lý trên Modal: video (FFmpeg + Whisper GPU) và Ollama dự phòng cho Gemini

Tài liệu cho quản trị viên. Người dùng Windows **không cần** cài FFmpeg, Whisper, Ollama hay tải trọng số mô hình
khi dùng đường xử lý trên đám mây.

```
Video (máy Windows: giao diện + điều phối)
  → Modal media worker: FFmpeg trên container CPU (tách âm thanh, khung hình)
                        faster-whisper trên GPU L4 (float16)
  → Bộ định tuyến LLM: Gemini trước → tự động chuyển sang Ollama (qwen3:4b) trên GPU L4 của Modal
  → Worker SDXL hiện có trên Modal (không đổi) → PPTX / Google Slides → xem trước + ghi chú dưới slide
```

Có ba app Modal riêng biệt, mỗi app một phần GPU riêng (chi phí ghi nhận riêng trong `modal billing report`):

| App | File | Tài nguyên | Giới hạn |
|---|---|---|---|
| `video2slides-media-worker` | `infrastructure/modal_media_worker.py` | CPU (FFmpeg) + L4 (Whisper) | 1 container mỗi loại, 1 việc/container, về 0 khi rảnh |
| `video2slides-llm-worker` | `infrastructure/modal_llm_worker.py` | L4 (Ollama) | 1 container, 1 yêu cầu/container, về 0 khi rảnh |
| `video2slides-image-worker` | `infrastructure/modal_image_worker.py` | L4 (SDXL) | **không thay đổi** |

## 1. Bảo mật

- Mọi API của worker dùng **Modal proxy auth** (`requires_proxy_auth=True`): yêu cầu thiếu `Modal-Key`/`Modal-Secret`
  bị Modal trả 401. Backend dùng cùng `MODAL_PROXY_TOKEN_ID/SECRET` với worker SDXL; token này chỉ gọi được web
  endpoint, không deploy được code.
- Ollama trong container chỉ lắng nghe `127.0.0.1:11434`; cổng 11434 không bao giờ được mở ra ngoài.
- Khóa Gemini và token Modal chỉ nằm ở backend (header), không vào trình duyệt, log, URL hay bài trình chiếu.
- Video được tải lên Volume riêng `video2slides-media` theo từng phần (id do server tạo, không nhận đường dẫn từ
  client), kiểm tra kích thước + sha256; không có URL công khai. Tệp tạm bị xóa sau khi xong / lỗi / hủy, và
  tệp quá 24 giờ bị dọn khi có upload mới.
- Chỉ gửi transcript có mốc thời gian (hoặc từng đoạn) tới LLM, không gửi video.

## 2. Triển khai (một lần)

Cần Modal CLI đã đăng nhập (`.venv\Scripts\modal.exe token new` nếu chưa có).

```bash
.venv/Scripts/modal.exe deploy infrastructure/modal_llm_worker.py
```

```bash
.venv/Scripts/modal.exe run infrastructure/modal_llm_worker.py::setup_model
```

`setup_model` tải `qwen3:4b` (**khoảng 2,5 GB**, Q4_K_M) bằng Ollama 0.40.3 vào Volume `video2slides-ollama-models`
trên container **CPU** (không tính tiền GPU). Chạy lại an toàn: tệp đã đủ được dùng lại; marker "sẵn sàng" chỉ ghi
sau khi Ollama báo `success`, nên tệp tải dở không bao giờ được coi là sẵn sàng. Không có model nào khác được tải.

```bash
.venv/Scripts/modal.exe deploy infrastructure/modal_media_worker.py
```

```bash
.venv/Scripts/modal.exe run infrastructure/modal_media_worker.py::setup_whisper
```

Sau đó ghi các URL `api` mà lệnh deploy in ra vào `.env`:

```
MODAL_LLM_WORKER_URL=https://<workspace>--video2slides-llm-worker-api.modal.run
MODAL_MEDIA_WORKER_URL=https://<workspace>--video2slides-media-worker-api.modal.run
```

Kiểm tra trực tiếp (có tính phí, mỗi lần vài phút GPU):

```bash
.venv/Scripts/python.exe scripts/modal_llm_live_check.py --deck --concurrency
```

```bash
.venv/Scripts/python.exe scripts/modal_media_live_check.py data/fixtures/lecture_vi.mp4
```

## 3. Quy tắc định tuyến LLM (`LLM_PROVIDER=auto`)

| Tình huống | Hành động |
|---|---|
| Không có `GEMINI_API_KEY` | Dùng Ollama trên Modal ngay, không gọi Gemini |
| Gemini thành công | Không gọi Modal |
| Khóa sai / không có quyền (401, 403, 400 API_KEY_INVALID) | Chuyển ngay, không thử lại khóa sai |
| Model/endpoint không dùng được (404, vùng không hỗ trợ) | Chuyển + cảnh báo cấu hình |
| Hết quota (theo ngày / hạn mức 0) | Chuyển ngay |
| Giới hạn tốc độ ngắn, lỗi 5xx, mạng, timeout | Tối đa 2 lần gọi; Retry-After > 20 s hoặc vượt ngân sách thời gian → chuyển ngay |
| JSON không hợp lệ | Tối đa 2 lần sửa có mục tiêu rồi chuyển |
| Gemini từ chối vì an toàn | **Dừng**, không dùng mô hình khác để vượt qua |
| Yêu cầu sai do lỗi ứng dụng (400 khác) | Dừng và báo lỗi, không che bằng fallback |
| Người dùng hủy | Dừng, không fallback |
| Cả hai thất bại | Lỗi tiếng Việt nêu trạng thái an toàn của cả hai; transcript và checkpoint được giữ |

- Ngân sách mỗi bài: ≤ 2 lần gọi + ≤ 2 lần sửa, trong `GEMINI_TOTAL_BUDGET_SECONDS` (mặc định 300 s).
- Sau khi một bài đã chuyển sang Modal, lần "Thử lại" của bài đó vẫn dùng Modal (`llm_route.json`).
- Circuit breaker (`data/llm_circuit.json`, chỉ lưu hash của khóa+model): 2 lỗi liên tiếp, hoặc ngay lập tức với khóa
  sai/hết quota/model sai → bỏ qua Gemini trong 600 s. Đổi khóa hoặc model → đánh giá lại ngay.
- Với Gemini, đơn vị nội dung là cả bài (một câu trả lời đã kiểm tra). Ollama viết theo từng bước (tóm tắt đoạn →
  dàn ý → từng nhóm slide) và lưu checkpoint từng bước; một bài luôn do **một** provider viết và được ghi lại
  trong `outline_meta.json` (provider, model, phiên bản prompt, khóa transcript).
- Khóa cache gồm: dữ liệu vào, provider thực tế, model, phiên bản Ollama, phiên bản prompt, schema và tham số sinh.

## 4. Thực tế về GPU

- **FFmpeg** chạy trên container **CPU** của Modal: tách âm thanh 16 kHz mono (+ chuẩn hóa âm lượng) và lấy khung hình
  bằng seek từng ảnh. Các thao tác này không dùng GPU và không cần NVDEC/NVENC; ứng dụng không chuyển mã video.
- **Tải lên**: mặc định chỉ gửi luồng âm thanh (tách trên máy bằng PyAV, không mã hóa lại) và vài khung hình
  (`MODAL_UPLOAD_AUDIO_ONLY=true`); video gốc không rời máy.
- **Whisper**: `WhisperModel(MODAL_WHISPER_MODEL="small", device="cuda", compute_type="float16")` trên L4, giải mã theo
  lô 16 + beam 5, mốc thời gian theo câu dựng từ mốc theo từ. Số đo và kế hoạch 90 phút:
  [KE_HOACH_VIDEO_90_PHUT_VI.md](KE_HOACH_VIDEO_90_PHUT_VI.md). Không có CUDA → lỗi
  `cuda_unavailable` (không bao giờ chạy CPU); backend còn từ chối kết quả nếu `device` khác `cuda`.
  Bằng chứng ghi kèm job: số thiết bị CUDA của CTranslate2, tên GPU, VRAM model chiếm.
- **Ollama**: worker từ chối nếu `/api/ps` cho thấy model không nằm hoàn toàn trên GPU (`size_vram/size < 0,95`).
- GPU không làm Qwen3 viết tốt hơn; chất lượng Qwen3 4B so với Gemini **chưa được đánh giá**.

## 5. Chi phí và giới hạn

Giá niêm yết (`modal billing rates`, 13/10/2026): L4 $0,80/giờ (≈ $0,000222/s), CPU $0,0473/lõi/giờ,
RAM $0,008/GiB/giờ, Volume $0,09/GiB/tháng.

Ước tính (chưa đo thực tế) cho một video 10 phút: Whisper GPU ~2–3 phút gồm khởi động và 60 s chờ tắt
(≈ $0,03), Ollama GPU ~4–6 phút khi Gemini không dùng được (≈ $0,05–0,08), FFmpeg CPU ≈ $0,001. Lưu trữ model
≈ 2,7 GB ≈ $0,25/tháng. Đây là **ước tính**, không phải số tiền bị tính; xem `modal billing report`.

- `scaledown_window=60`: container GPU ở lại 60 s sau yêu cầu cuối (có tính tiền), rồi về 0.
- `OLLAMA_KEEP_ALIVE` chỉ giữ model trong RAM/VRAM **bên trong** container đang chạy; không giữ GPU sống.
- Không có GPU chạy thường trực (`min_containers=0`). Health check chạy trên CPU, không khởi động GPU.
- Hãy đặt **giới hạn chi tiêu** của workspace trong Modal Dashboard → Settings → Usage & Billing. CLI không xem
  được giá trị này; ứng dụng không thay đổi nó.

## 6. Xử lý sự cố

| Thông báo | Cách xử lý |
|---|---|
| "Model chưa được tải vào Volume" | Chạy `setup_model` |
| "Token truy cập Modal không hợp lệ" | Kiểm tra `MODAL_PROXY_TOKEN_*` (Dashboard → Settings → Proxy Auth Tokens) |
| "Chưa deploy LLM worker" | `modal deploy infrastructure/modal_llm_worker.py`, kiểm tra URL |
| `cuda_unavailable` | Xem log `modal app logs video2slides-media-worker`; kiểm tra phiên bản ctranslate2/cuDNN |
| `gpu_not_used` | Ollama không nạp model lên GPU: xem log `video2slides-llm-worker` |
| Lần đầu rất chậm | Cold start: khởi động container + nạp model (đo bằng script live check) |

Chuyển về xử lý trên máy: `MEDIA_PROCESSING=local` (cần FFmpeg). Dùng Ollama trên máy: `LLM_PROVIDER=ollama`
(xem `docs/OLLAMA_SETUP_VI.md`).
