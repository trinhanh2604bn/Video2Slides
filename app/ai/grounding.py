"""Checks that slides stay grounded in the transcript (no invented filler slides or contact details).

Valid JSON does not prove faithfulness. Small models padding a long deck add slides such as "Acknowledgements",
"Contact Information", "Q&A" or "References" and invent e-mail addresses or URLs (observed with qwen3:4b on an
89-minute test video). Three layers use the helpers here:

1. outline validation (staged route, ``app.ai.ollama``): such slides are rejected and repaired;
2. slide validation: invented e-mails / URLs / phone numbers are rejected and repaired;
3. a final safety net for every provider (``sanitize``, called by the pipeline before the outline is saved):
   offending lines are removed, a slide left with nothing grounded is dropped, slides are renumbered, and the
   user gets a notice. Nothing is silently invented to replace them.

Matching is deliberately conservative (whole categories of boilerplate, explicit contact data); ordinary content
slides are never judged by wording alone.
"""

from __future__ import annotations

import re
import unicodedata

# category -> (title pattern, words in the transcript that would make such a slide legitimate)
FILLER = {
    "contact": (r"\bcontact\b|\bliên hệ\b|\be-?mail\b|\bphone\b|điện thoại|\bwebsite\b|follow us|theo dõi chúng tôi",
                r"\bcontact\b|liên hệ|e-?mail|phone|điện thoại|website|@"),
    "acknowledgements": (r"acknowledg|\bthank(s| you)\b|lời cảm ơn|\bcảm ơn\b|\bcredits?\b",
                         r"thank|acknowledg|cảm ơn|credit"),
    "questions": (r"\bq\s*&\s*a\b|\bq\s*and\s*a\b|^questions?\b|any questions|hỏi\s*(và|&)\s*đáp|câu hỏi\s*\??$",
                  r"question|hỏi"),
    "references": (r"\breferences?\b|bibliograph|further reading|tài liệu tham khảo|đọc thêm|\bsources\b",
                   r"reference|book|paper|source|tài liệu|sách"),
    "about": (r"about (the )?(author|speaker|presenter|me|us)\b|giới thiệu (tác giả|diễn giả|bản thân|người nói)",
              r"\bmy name\b|i am|tôi là|tên tôi|author|tác giả"),
}
OVERVIEW_LAYOUTS = ("title", "agenda", "section", "summary", "conclusion")

EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
URL = re.compile(r"\b(?:https?://|www\.)\S+|\b[\w-]+\.(?:com|org|net|edu|vn|io|gov)\b(?:/\S*)?", re.I)
# International (+84 ...), "(028) 1234 5678" and 10-11 digit local numbers; never year ranges like 1914-1918.
PHONE = re.compile(r"\+\d{1,3}[ .-]?\(?\d{1,4}\)?(?:[ .-]?\d{2,4}){2,4}|\(\d{2,4}\)\s?\d{3,4}[ .-]?\d{3,4}"
                   r"|(?<![\d-])0\d{2,3}[ .]?\d{3}[ .]?\d{3,4}(?![\d-])")


def _norm(text: str) -> str:
    return unicodedata.normalize("NFC", (text or "").lower())


def filler_category(title: str, source_text: str) -> str | None:
    """The boilerplate category of an unsupported slide title, or None when it is fine."""
    t = _norm(title).strip()
    src = _norm(source_text)
    for category, (pattern, support) in FILLER.items():
        if re.search(pattern, t) and not re.search(support, src):
            return category
    return None


def invented_contacts(text: str, source_text: str) -> list[str]:
    """E-mails, URLs and phone numbers in ``text`` that do not occur in the source."""
    src = _norm(source_text).replace(" ", "")
    found = []
    for rx in (EMAIL, URL, PHONE):
        for m in rx.findall(text or ""):
            token = _norm(m).strip(".,;:()").replace(" ", "")
            if token and token not in src:
                found.append(m.strip())
    return found


def outline_problems(slides: list[dict], source_text: str) -> tuple[list[str], list[int]]:
    """Problems for the outline repair prompt + indexes of slides to drop if the last attempt still has them."""
    problems, drop = [], []
    for i, s in enumerate(slides):
        title = str(s.get("title") or "")
        category = filler_category(title, source_text)
        if category and i > 0:
            problems.append(f"slide {i + 1} '{title}' is {category} boilerplate that the source does not contain: "
                            "replace it with a slide about content from the transcript (or remove it)")
            drop.append(i)
        layout = s.get("layout_type")
        if i > 0 and layout not in OVERVIEW_LAYOUTS and not (s.get("source_ranges") or []):
            problems.append(f"slide {i + 1} '{title}' ({layout}) has no source_ranges: every content slide must "
                            "cite the transcript times it is based on")
    return problems, drop


def slide_text(slide) -> str:
    parts = [slide.title, slide.subtitle, *slide.key_points, slide.left_title, *slide.left_points, slide.right_title,
             *slide.right_points, *[f"{i.heading} {i.text}" for i in slide.items]]
    return "\n".join(p for p in parts if p)


def slide_problems(slide, source_text: str) -> list[str]:
    problems = []
    category = filler_category(slide.title, source_text)
    if category and slide.slide_number > 1:
        problems.append(f"'{slide.title}' is {category} boilerplate not present in the source")
    bad = invented_contacts(slide_text(slide) + "\n" + slide.speaker_notes, source_text)
    if bad:
        problems.append(f"contact details not in the source: {', '.join(bad[:3])} (remove them)")
    return problems


def sanitize(pres, source_text: str) -> list[str]:
    """Final safety net (any provider). Mutates ``pres``; returns Vietnamese notices for the user."""
    notices: list[str] = []
    kept = []
    for s in pres.slides:
        if s.slide_number > 1 and filler_category(s.title, source_text):
            notices.append(f"Đã bỏ slide “{s.title}”: nội dung loại này không có trong video.")
            continue
        removed = 0

        def clean(lines: list[str]) -> list[str]:
            nonlocal removed
            out = [x for x in lines if not invented_contacts(x, source_text)]
            removed += len(lines) - len(out)
            return out

        s.key_points = clean(s.key_points)
        s.left_points = clean(s.left_points)
        s.right_points = clean(s.right_points)
        items = [i for i in s.items if not invented_contacts(f"{i.heading} {i.text}", source_text)]
        removed += len(s.items) - len(items)
        s.items = items
        for bad in invented_contacts(s.speaker_notes, source_text):
            s.speaker_notes = s.speaker_notes.replace(bad, "")
            removed += 1
        if removed:
            notices.append(f"Slide “{s.title}”: đã xóa {removed} thông tin liên hệ/đường dẫn không có trong video.")
        if s.slide_number > 1 and not s.has_body():
            notices.append(f"Đã bỏ slide “{s.title}”: sau khi xóa thông tin không có nguồn thì slide trống.")
            continue
        kept.append(s)
    if len(kept) >= 2 and len(kept) != len(pres.slides):
        for n, s in enumerate(kept, start=1):
            s.slide_number = n
        pres.slides = kept
    return notices
