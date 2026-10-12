"""Hand-written outline exercising every layout (used by tests and demos)."""
from app.ai.schema import Presentation

SAMPLE = {
  "presentation_title": "Học máy cơ bản cho người mới bắt đầu",
  "subtitle": "Tóm tắt từ bài giảng video",
  "language": "vi",
  "slides": [
    {"slide_number": 1, "title": "Học máy cơ bản cho người mới bắt đầu", "subtitle": "Khái niệm, quy trình và ứng dụng thực tế", "layout_type": "title", "speaker_notes": "Giới thiệu.", "visual_type": "video_frame"},
    {"slide_number": 2, "title": "Mục tiêu bài học", "layout_type": "bullets", "key_points": ["Hiểu học máy là gì", "Phân biệt học có giám sát và không giám sát", "Nắm quy trình xây dựng mô hình"], "speaker_notes": "Ba mục tiêu.", "visual_type": "none", "source_timestamps": [12.0]},
    {"slide_number": 3, "title": "Phần 1: Khái niệm nền tảng", "subtitle": "Dữ liệu, mô hình và dự đoán", "layout_type": "section", "speaker_notes": "", "visual_type": "none"},
    {"slide_number": 4, "title": "Học máy là gì?", "layout_type": "image_left", "key_points": ["Máy tính học quy luật từ dữ liệu", "Không cần lập trình từng quy tắc", "Cải thiện khi có thêm dữ liệu"], "speaker_notes": "Định nghĩa.", "visual_type": "video_frame", "source_timestamps": [40.0]},
    {"slide_number": 5, "title": "Có giám sát và không giám sát", "layout_type": "two_column", "left_title": "Có giám sát", "left_points": ["Dữ liệu có nhãn", "Dự đoán giá trị hoặc lớp", "Ví dụ: lọc thư rác"], "right_title": "Không giám sát", "right_points": ["Dữ liệu không nhãn", "Tìm cấu trúc ẩn", "Ví dụ: phân nhóm khách hàng"], "speaker_notes": "So sánh.", "visual_type": "diagram"},
    {"slide_number": 6, "title": "Quy trình xây dựng mô hình", "layout_type": "process", "items": [{"heading": "Thu thập dữ liệu", "text": "Gom dữ liệu từ nhiều nguồn đáng tin cậy"}, {"heading": "Làm sạch", "text": "Xử lý giá trị thiếu và nhiễu"}, {"heading": "Huấn luyện", "text": "Cho mô hình học từ tập huấn luyện"}, {"heading": "Đánh giá", "text": "Kiểm tra trên dữ liệu chưa thấy"}, {"heading": "Triển khai", "text": "Đưa mô hình vào sản phẩm"}], "speaker_notes": "Năm bước.", "visual_type": "diagram"},
    {"slide_number": 7, "title": "Lịch sử phát triển", "layout_type": "timeline", "items": [{"heading": "Thập niên 1950", "text": "Khái niệm học máy ra đời"}, {"heading": "Thập niên 1990", "text": "Các thuật toán thống kê phổ biến"}, {"heading": "Thập niên 2010", "text": "Học sâu bùng nổ nhờ GPU"}, {"heading": "Hiện nay", "text": "Mô hình ngôn ngữ lớn"}], "speaker_notes": "Mốc thời gian.", "visual_type": "diagram"},
    {"slide_number": 8, "title": "Ứng dụng tiêu biểu", "layout_type": "cards", "items": [{"heading": "Y tế", "text": "Hỗ trợ chẩn đoán hình ảnh"}, {"heading": "Tài chính", "text": "Phát hiện gian lận giao dịch"}, {"heading": "Thương mại", "text": "Gợi ý sản phẩm cá nhân hóa"}, {"heading": "Giao thông", "text": "Dự báo ùn tắc theo thời gian thực"}, {"heading": "Giáo dục", "text": "Cá nhân hóa lộ trình học"}, {"heading": "Nông nghiệp", "text": "Dự báo năng suất cây trồng"}], "speaker_notes": "Sáu lĩnh vực.", "visual_type": "diagram"},
    {"slide_number": 9, "title": "Số liệu được nhắc trong bài", "layout_type": "stats", "items": [{"heading": "80%", "text": "thời gian dành cho chuẩn bị dữ liệu"}, {"heading": "3", "text": "loại học máy chính"}, {"heading": "5", "text": "bước trong quy trình"}], "speaker_notes": "Số liệu.", "visual_type": "icon"},
    {"slide_number": 10, "title": "Ví dụ: nhận diện chữ viết tay", "layout_type": "image_right", "key_points": ["Ảnh chữ số làm đầu vào", "Mô hình học đặc trưng nét chữ", "Độ chính xác tăng theo dữ liệu"], "speaker_notes": "Ví dụ.", "visual_type": "video_frame", "source_timestamps": [200.0]},
    {"slide_number": 11, "title": "Những điểm cần nhớ", "layout_type": "summary", "key_points": ["Học máy học từ dữ liệu", "Chọn loại học phù hợp bài toán", "Dữ liệu sạch quyết định chất lượng", "Luôn đánh giá trên dữ liệu mới"], "speaker_notes": "Tổng kết.", "visual_type": "none"}
  ]
}

def sample_presentation() -> Presentation:
    return Presentation.model_validate(SAMPLE)


# Layouts added later; kept separate so slide indexes used by older tests stay stable.
EXTRA_SLIDES = {
  "agenda": {"slide_number": 2, "title": "Nội dung", "layout_type": "agenda", "subtitle": "Lộ trình 30 phút", "key_points": ["Khái niệm nền tảng", "Các loại học máy", "Quy trình xây dựng mô hình", "Ứng dụng thực tế", "Tổng kết"], "speaker_notes": "Lộ trình.", "visual_type": "none"},
  "concept": {"slide_number": 5, "title": "Mô hình (model)", "subtitle": "Một hàm toán học học được từ dữ liệu để biến đầu vào thành dự đoán.", "layout_type": "concept", "items": [{"heading": "Đầu vào", "text": "Đặc trưng mô tả từng mẫu dữ liệu"}, {"heading": "Tham số", "text": "Giá trị được điều chỉnh trong lúc huấn luyện"}, {"heading": "Đầu ra", "text": "Dự đoán: một nhãn hoặc một con số"}], "speaker_notes": "Khái niệm mô hình.", "visual_type": "diagram", "image_prompt": "a glass sculpture transforming scattered marbles into neatly sorted rows"},
  "key_insight": {"slide_number": 10, "title": "Bài học quan trọng nhất", "subtitle": "Chất lượng dữ liệu quan trọng hơn độ phức tạp của thuật toán.", "layout_type": "key_insight", "key_points": ["Dữ liệu bẩn cho kết quả sai", "Mô hình đơn giản thường đủ tốt", "Luôn kiểm tra dữ liệu trước"], "speaker_notes": "Nhấn mạnh.", "visual_type": "none"},
  "conclusion": {"slide_number": 15, "title": "Bắt đầu từ một bài toán nhỏ", "subtitle": "Học máy là kỹ năng thực hành", "layout_type": "conclusion", "key_points": ["Chọn một tập dữ liệu quen thuộc", "Thử một mô hình đơn giản", "Đánh giá và cải tiến dần"], "speaker_notes": "Kết thúc.", "visual_type": "none"},
}


def full_presentation() -> Presentation:
    """Every layout in a realistic order (15 slides)."""
    import json
    data = json.loads(json.dumps(SAMPLE))
    slides = data["slides"]
    slides.insert(1, EXTRA_SLIDES["agenda"])
    slides.insert(4, EXTRA_SLIDES["concept"])
    slides.insert(10, EXTRA_SLIDES["key_insight"])
    slides.append(EXTRA_SLIDES["conclusion"])
    return Presentation.model_validate(data)
