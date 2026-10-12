# Ảnh minh họa AI tự triển khai (SDXL trên Modal) — Hướng dẫn cài đặt

Tài liệu này dành cho **quản trị viên**. Người dùng cuối không cần biết Modal tồn tại: họ chỉ
tải video lên, bấm **Tạo bài trình chiếu**, và nhận file `.pptx` có ảnh minh họa.

```
Video ─► FFmpeg ─► Faster-Whisper ─► Gemini (dàn ý + image_prompt tiếng Anh)
      ─► Illustration Planner ─► ModalSDXLImageProvider ──HTTPS + proxy token──► Modal "api" (CPU)
                                                                                  │ spawn
                                                                                  ▼
                                                                    SDXLWorker (GPU L4, SDXL 1.0)
      ◄── ảnh JPEG (kiểm tra, lưu cache) ◄── poll kết quả ◄────────────────────────┘
      ─► PowerPoint engine (chèn ảnh theo slide_id, chữ vẫn chỉnh sửa được) ─► .pptx
```

## 0. Tóm tắt quyết định kỹ thuật

| Vấn đề | Lựa chọn | Lý do |
|---|---|---|
| Mô hình | `stabilityai/stable-diffusion-xl-base-1.0`, pin commit `46216598…` | Mã nguồn mở, giấy phép CreativeML Open RAIL++-M (xem mục 12) |
| GPU | NVIDIA L4 (24 GB), FP16 | Đủ bộ nhớ cho SDXL 1024px, rẻ nhất trong nhóm phù hợp ($0.000222/giây theo bảng giá Modal) |
| Số GPU | `min_containers=0`, `max_containers=1`, `scaledown_window=120` | Không có GPU nào chạy khi rảnh; tối đa 1 GPU; giữ ấm 2 phút để dùng lại trong cùng một bài |
| Xác thực | Modal **proxy auth token** (`Modal-Key`/`Modal-Secret`) | Token chỉ gọi được web endpoint, **không** deploy hay chạy code được → quyền tối thiểu. Endpoint không có token trả 401 |
| Giao tiếp | Backend gửi job → nhận `call_id` → poll kết quả | Không giữ HTTP request chờ GPU; `call_id` lưu trên đĩa nên khởi động lại không tạo ảnh trùng |
| Trọng số mô hình | Modal Volume `video2slides-hf-cache` | Tải 1 lần (bằng container CPU rẻ), không tải lại mỗi lần cold start |
| Chống trùng lặp | Khóa nội dung (prompt+kích thước+seed+model) + `modal.Dict` theo request_id | Retry/refresh/restart không trả tiền 2 lần cho cùng một ảnh |
| Ảnh lỗi | Mặc định `IMAGE_ON_FAILURE=fail`: job báo lỗi rõ ràng, bấm "Thử lại" sẽ tiếp tục (ảnh đã tạo được giữ lại) | Không bao giờ giả vờ đã tạo ảnh AI |

## 1. Đăng ký Modal

1. Mở <https://modal.com> → **Sign up** (đăng nhập bằng GitHub hoặc Google).
2. Một workspace được tạo tự động (gói **Starter**).
3. Cài Modal CLI trên **máy quản trị** (máy tính của bạn, không phải Replit):
   ```bash
   pip install "modal>=1.6,<1.7"
   modal setup
   ```
   `modal setup` mở trình duyệt để cấp token deploy. Token này lưu ở `~/.modal.toml` trên máy bạn —
   **không** đưa nó lên Replit hay GitHub.

## 2. Kiểm tra tín dụng miễn phí và điều kiện thanh toán

Thông tin dưới đây được kiểm tra trên trang giá của Modal ngày 12/10/2026. Giá có thể thay đổi,
**hãy tự kiểm tra lại** tại <https://modal.com/pricing> và *Settings → Usage & Billing*:

* Gói Starter: $0/tháng + phí tính toán, kèm **$30 tín dụng mỗi tháng**.
* L4: **$0.000222/giây** (~$0.80/giờ). CPU $0.0000131/core/giây, RAM $0.00000222/GiB/giây.
* Tài liệu billing của Modal ghi rằng cần **có phương thức thanh toán** trong tài khoản để sử dụng Modal.
  Hãy xác nhận điều này trong dashboard của bạn.
* **Rủi ro:** tín dụng miễn phí **không đảm bảo** không phát sinh hóa đơn. Phần dùng vượt $30 sẽ bị
  tính vào thẻ. Hãy đặt giới hạn ở bước 2a.

### 2a. Đặt giới hạn chi tiêu phía Modal (bắt buộc trước khi deploy)

1. Dashboard → **Settings → Usage & Billing**.
2. Đặt **Workspace budget** và/hoặc **spend limit** (ví dụ $5 hoặc $0 phí ngoài tín dụng).
   Theo tài liệu Modal, khi chạm spend limit Modal dừng các workload sẽ phát sinh phí ngoài tín dụng.
3. Chụp màn hình cấu hình để lưu lại.

> Giới hạn trong ứng dụng (mục 10) **không thay thế** giới hạn của Modal: chúng chỉ là ước tính và
> sổ ghi nhận nằm trên ổ đĩa Replit (bị reset khi redeploy).

### Ước tính chi phí

| Kịch bản | Thời gian GPU (ước tính) | Chi phí |
|---|---|---|
| 1 ảnh, GPU nguội | ~90 s khởi động + ~15 s suy luận + 120 s chờ tắt | ~$0.05 |
| 1 bài 8 ảnh | ~90 + 8×15 + 120 ≈ 330 s | ~$0.07 |
| 1 bài 4 ảnh (mật độ "Chuẩn") | ≈ 270 s | ~$0.06 |
| Tải trọng số lần đầu (CPU) | vài phút CPU | < $0.01 |

→ $30 tín dụng ≈ **400 bài** 8 ảnh. Số đo thật cần xác nhận ở bước 7 (chưa đo trên GPU thật).

## 3. Tạo thông tin xác thực

**a) Token deploy** (chỉ trên máy quản trị): đã có sau `modal setup`.

**b) Proxy auth token** (cho Replit): Dashboard → **Settings → Proxy Auth Tokens → New token**,
hoặc:
```bash
modal workspace proxy-tokens --help
```
Bạn nhận được một cặp **Token ID** (`wk-…`) và **Token Secret** (`ws-…`). Secret chỉ hiện một lần.

## 4. Deploy GPU worker

Trên máy quản trị, trong thư mục repository:

```bash
# 1) Tải trọng số SDXL vào Volume bằng container CPU (rẻ, chạy một lần, ~7 GB)
modal run infrastructure/modal_image_worker.py::download_model

# 2) Thử một ảnh trực tiếp (bật GPU L4 một lần, lưu smoke_test.jpg)
modal run infrastructure/modal_image_worker.py

# 3) Deploy
modal deploy infrastructure/modal_image_worker.py
```

Lệnh deploy in ra URL của hàm `api`, dạng
`https://<workspace>--video2slides-image-worker-api.modal.run`.
Kiểm tra endpoint đã được bảo vệ (phải trả **401**):
```bash
curl -i https://<workspace>--video2slides-image-worker-api.modal.run/v1/health
```

Tùy chọn khi deploy (biến môi trường trên máy quản trị):
* `V2S_GPU=L4` (mặc định). Nếu L4 không khả dụng: `V2S_GPU=A10` (đắt hơn ~38%).
* `V2S_MODEL_REVISION=<commit>` để đổi phiên bản mô hình (cache ảnh sẽ tự tách theo model).

## 5. Cấu hình Replit Secrets

Replit → **Tools → Secrets**, thêm:

| Secret | Giá trị |
|---|---|
| `AI_IMAGES_ENABLED` | `true` |
| `IMAGE_PROVIDER` | `modal_sdxl` |
| `MODAL_IMAGE_WORKER_URL` | URL ở bước 4 (không có `/` cuối) |
| `MODAL_PROXY_TOKEN_ID` | `wk-…` |
| `MODAL_PROXY_TOKEN_SECRET` | `ws-…` |
| `ADMIN_TOKEN` | chuỗi ngẫu nhiên dài (cho API quản trị) |
| `IMAGE_MONTHLY_BUDGET_USD` | ví dụ `5` |

Không cần cài gói `modal` trên Replit: backend gọi worker qua HTTPS bằng `httpx` có sẵn.
Không có secret nào được gửi xuống trình duyệt (`/api/config` chỉ trả về cờ bật/tắt).

## 6. Bật tính năng

1. Đặt `AI_IMAGES_ENABLED=true`, khởi động lại app (nút **Run**).
2. Trên trang chủ xuất hiện khối **Ảnh minh họa AI** (mặc định bật) với lựa chọn mật độ
   *Chuẩn* (~1 ảnh/3 slide) hoặc *Nhiều* (tối đa 8). Phong cách ảnh đi theo phong cách bài
   trình chiếu (Professional / Academic / Creative).
3. Tắt hoàn toàn: `AI_IMAGES_ENABLED=false` → ứng dụng chạy y như trước, không gọi GPU.

## 7. Kiểm tra một ảnh

Trong Replit Shell:
```bash
python scripts/test_image_worker.py --health     # chỉ kiểm tra kết nối + token, không dùng GPU
python scripts/test_image_worker.py --count 1    # 1 ảnh: đo cold start, thời gian suy luận, VRAM
python scripts/test_image_worker.py --count 3    # 3 ảnh trên cùng một GPU đang ấm
```
Ảnh lưu ở `data/worker_test/`. Ghi lại `wall`, `inference`, `model_load`, `vram` rồi chỉnh
`IMAGE_EST_SECONDS_PER_IMAGE` / `IMAGE_EST_BATCH_OVERHEAD_SEC` cho khớp thực tế.

## 8. Kiểm tra cả bài trình chiếu

1. Mở app, tải một video MP4 có lời nói (1–3 phút), để bật **Ảnh minh họa AI**.
2. Theo dõi các bước: *Lập kế hoạch ảnh minh họa* → *Tạo ảnh minh họa AI (GPU)* (hiển thị số ảnh
   thật đã xong, ví dụ `3/4 ảnh`) → *Dựng bố cục slide* → *Hoàn thiện*.
3. Tải `.pptx`, mở bằng PowerPoint: ảnh nằm cạnh nội dung (không đè chữ), tiêu đề và gạch đầu dòng
   vẫn sửa được, chú thích nhỏ "Ảnh minh họa tạo bởi AI" dưới ảnh.
4. Thử **Tạo lại** một ảnh ở trang kết quả: chỉ ảnh đó được vẽ lại (seed mới), bài được dựng lại.

## 9. Theo dõi sử dụng GPU

* Modal dashboard → app **video2slides-image-worker** → *Usage / Logs* (số giây GPU thật, chi phí).
  Log của worker không chứa prompt (prompt sinh từ transcript người dùng).
* Trong ứng dụng (ước tính):
  ```bash
  curl -H "X-Admin-Token: $ADMIN_TOKEN" https://<app-replit>/api/admin/images/usage
  ```
  Trả về số ảnh hôm nay / tháng này, giây GPU đo được, chi phí ước tính, các giới hạn.
* Mỗi job lưu `illustrations/manifest.json` (trạng thái, `call_id`, thời gian suy luận, VRAM).

## 10. Kiểm soát chi phí

| Cơ chế | Mặc định | Biến |
|---|---|---|
| Ảnh tối đa mỗi bài | 8 | `IMAGE_MAX_PER_PRESENTATION` (tối đa 8) |
| Ảnh tối đa mỗi ngày / tháng | 40 / 300 | `IMAGE_DAILY_LIMIT`, `IMAGE_MONTHLY_LIMIT` |
| Ngân sách ước tính / tháng | $5 | `IMAGE_MONTHLY_BUDGET_USD` |
| Ước tính trước mỗi lô; từ chối nếu vượt | luôn bật | `IMAGE_EST_*`, `IMAGE_GPU_PRICE_PER_SEC` |
| Số lần thử lại mỗi ảnh | 2 lần tổng | `IMAGE_MAX_ATTEMPTS` (≤ 3) |
| Lượt "Tạo lại" mỗi bài | 10 | `IMAGE_MAX_REGENERATIONS_PER_JOB` |
| Hết thời gian lô → hủy job GPU còn lại | 900 s | `IMAGE_BATCH_TIMEOUT_SEC` |
| Bộ nhớ đệm ảnh (không tạo lại ảnh giống hệt) | 7 ngày | `IMAGE_CACHE_TTL_HOURS` |
| GPU tối đa / giữ ấm | 1 / 120 s | trong `modal_image_worker.py` |
| Kích thước / số bước | ≤ ~1 MP, 10–40 bước | kiểm tra cả ở backend và worker |

## 11. Tắt GPU khẩn cấp (kill switch)

Chọn một trong các cách (từ nhanh đến triệt để):
1. API: `curl -X POST -H "X-Admin-Token: $ADMIN_TOKEN" -H "Content-Type: application/json" -d '{"active": true}' https://<app>/api/admin/images/kill-switch`
   (bật lại: `{"active": false}`). Có hiệu lực ngay, không cần khởi động lại.
2. Secret `IMAGE_KILL_SWITCH=true` (hoặc `AI_IMAGES_ENABLED=false`) rồi khởi động lại app.
3. Dừng hẳn worker: `modal app stop video2slides-image-worker`.
4. Thu hồi proxy token trong dashboard Modal.

Khi kill switch bật, giao diện tự vô hiệu hóa ô "Ảnh minh họa AI" và giải thích lý do; API từ chối
(HTTP 409) yêu cầu có ảnh thay vì âm thầm bỏ ảnh.

## 12. Giấy phép mô hình

SDXL 1.0 dùng **CreativeML Open RAIL++-M**
(<https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0/blob/main/LICENSE.md>). Giấy phép
này cho phép sử dụng, kể cả thương mại, **kèm các hạn chế sử dụng** (Attachment A: không dùng để
gây hại, lừa đảo, tạo nội dung sai sự thật về người thật, …) và yêu cầu chuyển tiếp các hạn chế đó
cho người dùng của bạn. Thẻ mô hình cũng ghi mục đích chính là nghiên cứu. **Hãy đọc toàn văn giấy
phép và cân nhắc thêm điều khoản tương ứng vào Điều khoản sử dụng của ứng dụng** — tôi không thể
thay bạn kết luận về mặt pháp lý.

## 13. Xử lý sự cố

| Triệu chứng | Nguyên nhân / Cách xử lý |
|---|---|
| "Chưa cấu hình GPU worker…" | Thiếu `MODAL_IMAGE_WORKER_URL` / `MODAL_PROXY_TOKEN_*` trong Secrets |
| "Token truy cập GPU worker không hợp lệ" | Proxy token sai/bị thu hồi → tạo token mới (bước 3b) |
| `--health` trả 404 | URL sai (phải là URL của hàm `api`), hoặc chưa `modal deploy` |
| Ảnh đầu tiên rất chậm | Cold start (khởi động container + nạp ~7 GB vào GPU). Nếu Volume trống, chạy lại `download_model` |
| `cuda_oom` | Giảm `IMAGE_WIDTH/HEIGHT` (ví dụ 1216×832) hoặc `IMAGE_STEPS`; worker tự dọn bộ nhớ và backend thử lại 1 lần |
| `timeout` | GPU bận/không có L4 → tăng `IMAGE_BATCH_TIMEOUT_SEC` hoặc deploy với `V2S_GPU=A10` |
| "Ảnh trả về bị trống" | VAE fp16 cho ảnh đen; backend tự thử lại. Lặp lại nhiều lần → báo lỗi kèm log Modal |
| "Đã đạt giới hạn…" / "vượt ngân sách" | Giới hạn ứng dụng (mục 10); tăng biến tương ứng nếu có chủ đích |
| Job lỗi ở bước tạo ảnh | Bấm **Thử lại**: transcript, dàn ý và ảnh đã tạo được giữ lại; chỉ ảnh thiếu được gửi lại |
| Muốn bài vẫn hoàn tất khi thiếu ảnh | `IMAGE_ON_FAILURE=skip` (bài có thông báo rõ số ảnh bị thiếu) |

## 14. Chuyển sang nhà cung cấp GPU khác

Mọi logic GPU nằm sau interface `ImageGenerationProvider` (`app/illustrations/base.py`):

```python
class MyProvider(ImageGenerationProvider):
    name = "runpod_sdxl"; model_id = "…"
    def submit(self, req, idempotency_key=None) -> str: ...      # trả về job reference
    def get_job_status(self, job_ref) -> ImageJobStatus: ...     # pending | done | failed | expired
    def cancel(self, job_ref): ...                               # tùy chọn
```

1. Tạo `app/illustrations/<ten>.py` theo mẫu `modal_sdxl.py` (RunPod Serverless, ComfyUI API,
   máy chủ GPU riêng chạy cùng code `SDXLWorker`…).
2. Đăng ký trong `get_image_provider()` (`app/illustrations/service.py`) và đặt `IMAGE_PROVIDER`.
3. Planner, cache, giới hạn chi phí, retry, PowerPoint engine và giao diện **không cần sửa**.

## 15. Trạng thái job

Các trạng thái theo yêu cầu được ánh xạ vào hệ thống stage có sẵn (không thêm job manager mới):

| Yêu cầu | Stage trong app |
|---|---|
| VIDEO_PROCESSING | `extracting_audio` (và `downloading` với URL) |
| TRANSCRIBING | `transcribing` |
| CONTENT_ANALYSIS | `analyzing` |
| ILLUSTRATION_PLANNING | `planning_illustrations` *(mới)* |
| IMAGE_GENERATION | `generating_images` *(mới)* |
| POWERPOINT_RENDERING | `generating` + `finalizing` |
| COMPLETED | `completed` / status `done` |
| FAILED | status `failed` (+ thông báo lỗi) |
