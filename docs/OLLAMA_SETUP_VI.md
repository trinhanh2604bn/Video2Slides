# Ollama + Qwen3 4B — cài đặt, cấu hình và giới hạn phần cứng

Ứng dụng dùng **Ollama** để chạy model **`qwen3:4b`** ngay trên máy. Model này phân tích bản ghi lời nói
(transcript) và tự động tạo: tiêu đề, dàn ý, nội dung từng slide, bố cục, ghi chú đọc (giải thích chi tiết,
ví dụ, điều cần nhớ), Speaker Notes, mô tả hình minh họa, prompt tiếng Anh cho ảnh SDXL và mốc thời gian
nguồn. Không cần Gemini, khóa OpenAI, ChatGPT hay dán JSON thủ công.

```
Video → FFmpeg → Faster-Whisper → Ollama qwen3:4b → (Modal SDXL, tùy chọn) → PPTX / Google Slides → Xem trước & tải về
```

* Google Slides vẫn cần đăng nhập Google (OAuth) — đó là quyền ghi vào Drive của bạn, **không liên quan** đến
  Gemini API.
* Ảnh minh họa vẫn do **GPU worker SDXL trên Modal** đang có tạo ra (tính phí theo Modal). Ollama chỉ viết
  prompt cho ảnh; GPU của Modal không tăng tốc Ollama trên máy bạn.

## 1. Cài đặt (một lần)

`install.bat` làm các bước dưới đây và **hỏi trước** khi cài/tải:

1. Tìm Ollama (`ollama` trên PATH hoặc `%LOCALAPPDATA%\Programs\Ollama\ollama.exe`). Nếu chưa có, đề nghị
   `winget install --id Ollama.Ollama`. Có thể tự cài tại <https://ollama.com/download>.
2. Nếu Ollama chưa chạy, khởi động `ollama serve` ẩn, **chỉ nghe ở 127.0.0.1**. Nếu Ollama đã chạy (ví dụ
   ứng dụng Ollama ở khay hệ thống) thì dùng lại, không mở tiến trình thứ hai.
3. Nếu chưa có model, hiện thông báo *"Sẽ tải model qwen3:4b (khoảng 2,5 GB)"* và hỏi trước khi chạy
   `ollama pull qwen3:4b`. Chỉ tải đúng model đã cấu hình; không tự tải 8B/14B.

Làm thủ công:

```bash
ollama pull qwen3:4b
```

```bash
ollama list
```

`start.bat` kiểm tra lại mỗi lần mở: Ollama có phản hồi không, đã có model chưa, Google đã ủy quyền chưa,
Modal đã cấu hình chưa (không in giá trị bí mật). Trong ứng dụng: `GET /api/llm/status`.

## 2. Cấu hình (`.env`)

| Biến | Mặc định | Gửi tới Ollama dưới dạng |
|---|---|---|
| `LLM_PROVIDER` | `ollama` | — (`gemini` chỉ khi bạn chọn rõ; `auto` cũng = `ollama`) |
| `OLLAMA_BASE_URL` | `http://127.0.0.1:11434` | địa chỉ API gốc |
| `OLLAMA_MODEL` | `qwen3:4b` | `model` |
| `OLLAMA_NUM_CTX` | `8192` | `options.num_ctx` |
| `OLLAMA_TEMPERATURE` | `0.2` | `options.temperature` |
| `OLLAMA_KEEP_ALIVE` | `5m` | `keep_alive` (giải phóng RAM sau thời gian rảnh) |
| `OLLAMA_REQUEST_TIMEOUT_SECONDS` | `600` | thời gian chờ phía ứng dụng cho mỗi yêu cầu |
| `OLLAMA_THINK` | `auto` | `think: false` (auto/off) hoặc `true` (on) — chỉ gửi khi model báo có khả năng *thinking* |
| `OLLAMA_SLIDES_PER_BATCH` | `3` | số slide viết trong một yêu cầu |
| `OLLAMA_MAX_REPAIRS` | `2` | số lần sửa tối đa sau lần thử đầu |
| `RELEASE_WHISPER_BEFORE_LLM` | `true` | giải phóng Whisper trước khi chạy LLM |

Ollama **không tự đọc** các tên biến trên; ứng dụng chuyển chúng thành các trường của `/api/chat`.
Số token đầu ra tối đa (`options.num_predict`) được ứng dụng tự tính cho từng yêu cầu để câu trả lời không
vượt quá `num_ctx`.

### Thinking của Qwen3

Đã kiểm tra trên Ollama 0.40.3 với `qwen3:4b` (digest `359d7dd4bcda`, Q4_K_M, khả năng
`completion, tools, thinking`): với `think: true`, model dùng hết 1500 token đầu ra cho phần suy luận và
**không trả về JSON**; với `think: false` model trả JSON hợp lệ ngay. Vì vậy mặc định là tắt. Phần
suy luận (nếu bật) luôn được tách riêng và **không bao giờ** được đọc như JSON kết quả. Model khác có thể
xử lý `think` khác — hãy kiểm tra lại khi đổi model.

## 3. Cách tạo nội dung (vì sao có nhiều bước)

Model 4B với ngữ cảnh 8192 token không thể viết cả bài kèm ghi chú chi tiết trong một câu trả lời, nên:

1. **Transcript dài** được chia tại các khoảng lặng thành nhiều phần (không cắt bỏ câu nào), mỗi phần được
   tóm tắt thành các ý kèm mốc thời gian. Rất dài thì tóm tắt tiếp các bản tóm tắt.
2. **Dàn ý**: tiêu đề bài, danh sách slide (tiêu đề, bố cục, mục đích, mốc thời gian nguồn).
3. **Viết slide** theo lô (mặc định 3 slide/lô) với đoạn transcript liên quan: chữ trên slide (ngắn),
   Speaker Notes, ghi chú đọc, mô tả hình, prompt ảnh tiếng Anh (chỉ cho slide được chọn).
4. **Kiểm tra** mọi câu trả lời: đúng schema JSON (Ollama `format` + Pydantic), đúng `slide_id` và thứ tự,
   đủ số slide (hoặc trong khoảng khi chọn *Tự động*), có nội dung và ghi chú, mốc thời gian nằm trong
   thời lượng video, không bị cắt cụt. Lỗi → sửa có mục tiêu (chỉ yêu cầu lại slide hỏng), tối đa 2 lần;
   lô bị cắt cụt → chia đôi.
5. Mỗi bước hợp lệ được **lưu cache** trong thư mục công việc (`llm_cache/`), khóa gồm: model, phiên bản
   prompt/schema, thiết lập sinh (num_ctx, temperature, think, num_predict) và toàn bộ đầu vào. Bấm
   **Thử lại** sau lỗi sẽ không chạy lại FFmpeg/Whisper và không viết lại các phần đã xong.

Transcript được đặt trong thẻ `<transcript>` và được coi là **dữ liệu**, không phải lệnh: câu kiểu "bỏ qua
hướng dẫn" trong video không thể ghi đè prompt của ứng dụng.

## 4. Phần cứng và giới hạn đã biết

Máy đích đã kiểm tra: Intel Core i9-13900H (14 nhân / 20 luồng), RAM 16 GB, GPU tích hợp Intel Iris Xe
(Ollama **không** dùng GPU này → chạy hoàn toàn bằng CPU). Đo được:

* Model nạp vào RAM: ~3,7 GB với `num_ctx=8192` (`/api/ps`, `size_vram = 0`).
* Tốc độ sinh khoảng 5 token/giây khi máy chỉ còn < 1 GB RAM trống (đang mở Android Studio, Chrome…);
  hệ thống phải dùng bộ nhớ ảo nên chậm. Đóng bớt ứng dụng nặng sẽ nhanh hơn (chưa đo trong điều kiện đó).
* Số liệu đầy đủ: [OLLAMA_TEST_REPORT_VI.md](OLLAMA_TEST_REPORT_VI.md).

Khuyến nghị cho máy 16 GB:

* Giữ `OLLAMA_NUM_CTX=8192` (mức khởi đầu thận trọng). Tăng lên sẽ tốn thêm RAM.
* Mỗi lúc chỉ một yêu cầu LLM; Whisper luôn chạy xong trước khi LLM bắt đầu và được giải phóng khỏi RAM.
* Không chạy song song nhiều model trong Ollama (ứng dụng cảnh báo trong `/api/llm/status` nếu có).
* Không tự nâng lên 8B/14B: cần nhiều RAM hơn và chậm hơn; chỉ đổi `OLLAMA_MODEL` khi đã thử nghiệm.

## 5. Xử lý lỗi

| Thông báo | Nguyên nhân / cách xử lý |
|---|---|
| *Không kết nối được Ollama tại …* | Ollama chưa chạy: mở ứng dụng Ollama hoặc `ollama serve`, rồi **Thử lại** |
| *Ollama chưa có model 'qwen3:4b'* | `ollama pull qwen3:4b` |
| *Không đủ RAM để chạy model* | Đóng bớt ứng dụng; giảm `OLLAMA_NUM_CTX` (ví dụ 6144) |
| *Ollama không nạp được model* | Khởi động lại Ollama; nếu vẫn lỗi, tải lại model |
| *Ollama không trả lời trong … giây* | Máy quá tải; đóng bớt ứng dụng hoặc tăng `OLLAMA_REQUEST_TIMEOUT_SECONDS` |
| *Nội dung vượt cửa sổ ngữ cảnh* | Giảm `OLLAMA_SLIDES_PER_BATCH` hoặc tăng `OLLAMA_NUM_CTX` (tốn RAM hơn) |
| *…chưa tạo được … hợp lệ sau 3 lần thử* | **Thử lại** (các phần đã xong được giữ); hoặc chọn ít slide hơn |

Mọi lỗi LLM chỉ làm hỏng bước AI: bản ghi lời nói được giữ, **Thử lại** không chạy lại FFmpeg/Whisper.
