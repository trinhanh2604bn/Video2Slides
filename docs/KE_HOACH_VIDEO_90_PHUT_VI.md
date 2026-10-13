# Chép lời: kết quả đo và kế hoạch mở rộng lên video 90 phút

Đo ngày 13/10/2026 trên Modal NVIDIA L4, faster-whisper 1.2.1, CTranslate2 4.8.2, float16. Video thử:
`data/fixtures/lecture_vi.mp4` (92 s, giọng đọc tiếng Việt tổng hợp) so với văn bản gốc
`scripts/fixtures/lecture_vi.txt`. Chỉ số: tỉ lệ lỗi theo âm tiết (SER, bỏ qua hoa/thường và dấu câu).
**Chỉ một mẫu, giọng máy đọc**: đủ để so sánh tương đối, chưa đủ để kết luận cho bài giảng thật.
Lệnh đo lại: `.venv/Scripts/python.exe scripts/asr_eval.py VIDEO.mp4 REF.txt [cấu hình ...]`.

## 1. Kết quả đo

| Cấu hình | SER | Thời gian giải mã (90 s âm thanh) | Mốc thời gian |
|---|---|---|---|
| `base`, tuần tự (cấu hình cũ) | 22,1% | 2,7 s | theo đoạn |
| `small`, tuần tự, beam 1 | 13,5% | 2,2–3,8 s | theo đoạn |
| `small`, beam 5 / + prompt chung / + condition_on_previous_text | 13,5–13,8% | 2,8–3,3 s | theo đoạn |
| `small`, int8_float16 | 13,2% | 2,9 s | theo đoạn |
| `small`, theo lô 16, không mốc thời gian | 12,4–12,9% | 1,1–1,3 s | ~30 s/đoạn (quá thô) |
| `small`, theo lô 16, có token mốc thời gian | 16,7–17,0% | 1,3–2,2 s | theo đoạn |
| **`small`, theo lô 16, beam 5, mốc theo từ (mặc định mới)** | **12,6%** | **1,4–1,8 s** | **theo câu, ~26 câu** |

- Chế độ theo lô giải mã không cần token mốc thời gian (chính xác hơn), sau đó mốc thời gian lấy theo **từng từ** và
  ghép lại thành câu. Mốc thời gian đã kiểm tra: đúng thứ tự, nằm trong video, đoạn dài nhất 5,1 s.
- Lỗi còn lại chủ yếu là dấu thanh ("ưu tiên" → "yêu tiên", "trì hoãn" → "trì hoán").
- VRAM: `small` ~770 MB. Nạp model 0,7–1,5 s. Container GPU Whisper khởi động lạnh 6–13 s khi image đã có sẵn
  (lần đầu sau mỗi lần deploy image mới: ~110 s).

## 2. Đã áp dụng

| Thay đổi | Trước | Sau (đo trên bài thật, 92 s video) |
|---|---|---|
| Model Modal `small` + theo lô + mốc theo từ | `base`, SER 22% | SER 12,6% |
| Chỉ tải âm thanh (PyAV, không mã hóa lại, không cần FFmpeg) | tải 13,7 MB video | 0,76 MB, tách mất 0,05 s |
| Khởi động trước GPU LLM khi chắc chắn dùng Modal | yêu cầu LLM đầu chờ ~111 s | ~62 s |
| Đọc trước model Ollama từ Volume | — | 2,4 GB/1,8 s (Volume không phải nút thắt) |
| Tổng thời gian một bài (không ảnh AI) | 238 s | **185 s** |
| Giai đoạn xử lý video (tải lên + FFmpeg + Whisper) | 149 s (Job A) | 35–57 s |

Chế độ xử lý trên máy (`MEDIA_PROCESSING=local`) vẫn dùng `WHISPER_MODEL` (mặc định `base`) vì CPU chậm hơn nhiều.

## 3. Hướng nâng độ chính xác tiếp theo (cần duyệt tải model/dữ liệu)

| Ứng viên | Kích thước tải (ước tính) | Ghi chú |
|---|---|---|
| `large-v3-turbo` | ~1,6 GB | Có sẵn cho faster-whisper, nhanh gấp 2–4 lần large-v3 |
| `large-v3` | ~3,1 GB | Báo cáo WER tiếng Việt FLEURS ~8,6% |
| VinAI PhoWhisper-medium / large (BSD-3) | ~3 / ~6 GB trọng số gốc → ~1,5 / ~3 GB sau chuyển CT2 | Tinh chỉnh trên 844 giờ tiếng Việt; báo cáo WER thấp nhất trong các mô hình Whisper |
| Bộ thử FLEURS `vi_vn` (CC-BY-4.0) | vài trăm MB phần test | Đánh giá công bằng giữa các mô hình |

Đề xuất: đánh giá trên FLEURS + 1–2 đoạn bài giảng thật do bạn cung cấp (5 phút, có bản gõ tay), rồi chọn mô hình có
SER thấp nhất mà thời gian chép 90 phút vẫn dưới ~5 phút. Các số liệu WER công bố ở trên chưa được kiểm chứng trên dữ
liệu của bạn.

## 4. Kế hoạch mở rộng lên 90 phút

### Ước tính cho một video 90 phút (dựa trên số đo hiện tại, chưa đo thật)

| Bước | Ước tính | Cơ sở |
|---|---|---|
| Tách âm thanh + khung hình trên máy | 5–15 s | remux đọc cả tệp video từ đĩa |
| Tải âm thanh lên (~45 MB với AAC 67 kbps) | ~40 s ở 10 Mbit/s | đo được 2,6–9,6 Mbit/s, dao động mạnh |
| FFmpeg trên Modal (16 kHz + chuẩn hóa âm lượng) | ~1,5–2 phút | 1,8 s cho 90 s âm thanh |
| Whisper `small` theo lô | ~1,5–2 phút | 50–63 lần thời gian thực khi ấm |
| LLM: Gemini | vài phút | cần chia nhóm slide (mục B) |
| LLM: Qwen3 trên Modal | ~6–10 phút | ~20 yêu cầu × 15–25 s; khởi động lạnh được giấu sau Whisper |
| Ảnh AI + PPTX + Google Slides | 2–4 phút | như hiện tại (tối đa 8 ảnh) |
| **Tổng** | **~12–20 phút** | so với tải nguyên video 1 GB: +15–25 phút chỉ riêng tải lên |

Chi phí Modal ước tính mỗi video 90 phút: Whisper ~$0,03–0,05, Qwen3 (chỉ khi dùng dự phòng) ~$0,10–0,16 gồm khởi động
và 60 s chờ tắt, FFmpeg CPU < $0,01, ảnh SDXL như hiện tại. Gemini tiêu tốn nhiều quota hơn (đầu vào ~20 nghìn token);
hôm nay quota Gemini miễn phí đã hết sau vài lần thử.

### Trạng thái (cập nhật 13/10/2026)

- **A đã làm**: thời lượng tối đa 90 phút (`MAX_DURATION_SEC=5400`), tệp tối đa 4 GB (chỉ lưu trên máy), kiểm tra
  dung lượng đĩa trống (`MIN_FREE_DISK_MB`), URL import 90 phút / 1 GB, timeout worker 30–40 phút
  (preprocess 1800 s, Whisper 2400 s, `MODAL_MEDIA_TIMEOUT_SECONDS=2400`), worker nhận tối đa 2 giờ âm thanh.
- **B đã làm**: số slide gợi ý tăng theo thời lượng (10 phút: 14, 30 phút: 20, 60 phút: 28, 90 phút: 35; tối đa 40;
  ô chọn 5–40). Video từ 15 phút (`LLM_STAGED_MIN_SECONDS=900`): Gemini cũng viết theo từng phần (tóm tắt đoạn →
  dàn ý → nhóm slide), mỗi phần được kiểm tra và lưu checkpoint; Gemini lỗi giữa chừng thì **chỉ các phần còn lại**
  chuyển sang Qwen3 trên Modal, các phần đã xong được giữ, bài ghi rõ cả hai mô hình (`providers_used`).
  Video ngắn hơn giữ cách cũ (Gemini viết cả bài trong một câu trả lời).
- **Chưa làm**: đo `num_ctx` 16384 (`LLM_LONG_NUM_CTX`), chạy song song chép lời và tóm tắt (mục C).

### Các thay đổi cần làm (theo giai đoạn)

**A. Giới hạn và độ bền (bắt buộc)**
1. `MAX_DURATION_SEC` 600 → 5400; `MAX_UPLOAD_MB` theo dung lượng video thực tế (chỉ lưu trên máy; thứ tải lên Modal
   là âm thanh ~45 MB). Giới hạn URL import tương tự. Kiểm tra dung lượng đĩa trống trước khi nhận tệp lớn.
2. Worker: `MAX_DURATION_SEC` 3600 → 5400; timeout preprocess 600 s và transcribe 900 s → 1800 s;
   `MODAL_MEDIA_TIMEOUT_SECONDS` 900 → 1800.
3. FFmpeg chuẩn hóa âm lượng một lượt cho 90 phút: đo; nếu > 2 phút thì chạy song song từng đoạn hoặc tắt.
4. Tải lên trình duyệt → máy chủ cục bộ: tệp 1–3 GB, cần thanh tiến độ và khả năng tiếp tục.

**B. Nội dung LLM cho bài dài**
1. Số slide: nâng trần gợi ý từ 15 lên ~30–40, và giới hạn của form từ 25 lên ~40; chia bài theo chương/section.
2. Gemini: chuyển sang nhiều bước như Ollama (dàn ý → nhóm slide), tránh một câu trả lời quá dài và bị cắt;
   nâng timeout mỗi yêu cầu; mỗi nhóm slide là một đơn vị dự phòng riêng.
3. Qwen3: thử `num_ctx` 16384 (L4 còn dư VRAM; hiện 3,7 GB) để giảm số đoạn tóm tắt; đo chất lượng và thời gian.
4. Ghi chú đọc: giới hạn độ dài để bài 40 slide không quá nặng.

**C. Tốc độ**
1. Đã có: tải chỉ âm thanh, chép lời theo lô, khởi động trước GPU LLM.
2. Có thể thêm: chép lời theo từng đoạn và tóm tắt ngay đoạn đã xong (chồng thời gian Whisper và LLM).

**D. Kiểm thử trước khi mở**
1. 1 video 30 phút, rồi 1 video 90 phút thật (bài giảng có quyền sử dụng).
2. Đo từng bước, kiểm tra slide và ghi chú, kiểm tra hủy giữa chừng và "Thử lại".
3. So sánh chi phí đo được với ước tính.

Đề xuất thứ tự: A → B (bắt buộc cho bài dài) → đánh giá model ở mục 3 → C.

## 5. Kết quả đo thật (13/10/2026)

### Chép lời tiếng Anh (`scripts/asr_benchmark_en.py`, LibriSpeech, 102 phút âm thanh mỗi model, L4)

| Model | WER test-clean | WER test-other | Tốc độ | VRAM |
|---|---|---|---|---|
| small (đa ngôn ngữ) | 1,94% | 5,27% | 104× | 0,8 GB |
| small.en | 3,12% | 6,55% | 127× | 0,6 GB |
| medium.en | 1,92% | 4,92% | 67× | 1,8 GB |
| distil-large-v3.5 | 1,49% | 4,01% | 91× | 1,9 GB |
| **large-v3-turbo (mặc định tiếng Anh)** | **1,33%** | **3,34%** | **86×** | 1,9 GB |
| large-v3 | 1,25% | 2,74% | 44× | 3,6 GB |

Một tệp liền 92 phút: large-v3-turbo 63 s (WER 1,29%), large-v3 118 s (WER 1,18%); không thấy trôi lệch.
LibriSpeech là giọng đọc sách, dễ hơn bài giảng thật; cần thêm mẫu bài giảng thật để xác nhận.

### Bài 89 phút tiếng Anh, chạy đầy đủ qua ứng dụng

| Bước | Thời gian đo |
|---|---|
| Tách âm thanh + khung hình trên máy (PyAV) | 2,3 s |
| Tải âm thanh lên (43 MB) | 59 s |
| FFmpeg trên Modal (chuẩn hóa âm lượng + 16 kHz) | 107 s (riêng âm thanh 93 s) |
| Whisper large-v3-turbo (GPU) | 85 s (giải mã 69 s, 77× thời gian thực) |
| Qwen3 trên Modal: 15 đoạn tóm tắt + dàn ý + 37 slide | ~12–16 phút (lần chạy cuối: 30 yêu cầu, 474 s, một phần dùng checkpoint) |
| PPTX + 37 ảnh xem trước + Google Slides (ghi chú 37/37) | ~13 s |

Sự cố tìm thấy và đã sửa: worker LLM chạy `ollama serve` với stderr là một PIPE không ai đọc; sau ~30 yêu cầu bộ
đệm đầy và Ollama treo (bài ngắn không gặp). Log nay ghi ra tệp; worker còn tự dừng khi không có token mới trong 60 s.

Chất lượng còn hạn chế (Qwen3 4B, video thử ghép từ nhiều chương sách nói): bố cục ít đa dạng (5 kiểu), và 2 slide
cuối ("Acknowledgements", "Contact Information") do mô hình tự thêm, không có trong nguồn. Cần thử với bài giảng thật
và với Gemini (hôm nay Gemini liên tục 503/timeout/hết quota nên không đo được đường Gemini cho bài dài).
