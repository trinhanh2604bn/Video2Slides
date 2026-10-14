"""Per-point citations: the moment of the video each on-slide point comes from.

Computed when Reading Mode asks for a deck, by plain word matching against the transcript (no LLM, a few
milliseconds per slide), so making a deck is never slower. The search stays inside the slide's own source spans
(padded) when it has them. A point without a convincing match gets no timestamp rather than a wrong one; the
slide-level spans are still shown.
"""

from __future__ import annotations

import re
import unicodedata

from .transcribe import Transcript

WORD = re.compile(r"\w+", re.UNICODE)
PAD_SEC = 30.0       # search a little around the slide's spans (the LLM's ranges are approximate)
WINDOW = 3           # consecutive transcript segments matched together (a point often spans a few sentences)
MIN_SCORE = 0.4      # share of the point's content words found in the window
MIN_HITS = 2

# Very common words carry no evidence about where a point comes from.
STOP = set("""
và là của có các những được cho với này trong khi để một không thì đã đang sẽ cũng như từ theo về
ra vào lại nên mà hay hoặc rất nhiều ít bạn chúng ta tôi họ nó đó đây kia gì nào sao vì nếu nhưng
còn đến tại trên dưới sau trước bằng qua thể làm việc người cách điều vậy thế rồi chỉ mỗi mọi
the a an and or of to in on for with is are was were be been this that these those it its as at by from
we you they he she i our your their not no but if so do does did can will would should could about into
""".split())


def _fold(text: str) -> str:
    """Lower case without diacritics: tolerant to speech-recognition accent errors (mời / mọi)."""
    text = unicodedata.normalize("NFD", text.lower()).replace("đ", "d")
    return "".join(c for c in text if unicodedata.category(c) != "Mn")


def content_words(text: str) -> set[str]:
    words = set()
    for w in WORD.findall(text.lower()):
        if len(w) < 2 or w in STOP:
            continue
        words.add(_fold(w))
    return words


class Matcher:
    """Caches the word sets of a transcript's segments for many lookups."""

    def __init__(self, transcript: Transcript | None):
        self.segments = list(transcript.segments) if transcript else []
        self.words = [content_words(s.text) for s in self.segments]

    def locate(self, point: str, spans: list[tuple[float, float]]) -> float | None:
        """Start time (seconds) of the transcript passage that best supports ``point``, or None."""
        want = content_words(point)
        if len(want) < MIN_HITS or not self.segments:
            return None
        idx = [i for i, s in enumerate(self.segments)
               if not spans or any(s.end >= a - PAD_SEC and s.start <= b + PAD_SEC for a, b in spans)]
        best, best_score = None, 0.0
        for k, i in enumerate(idx):
            window = [j for j in idx[k:k + WINDOW] if j - i < WINDOW]
            have = set().union(*(self.words[j] for j in window))
            hits = len(want & have)
            score = hits / len(want)
            if hits >= MIN_HITS and score > best_score:
                # cite the segment of the window that matches the point best (the earliest on a tie)
                top = max(window, key=lambda j: (len(self.words[j] & want), -j))
                best, best_score = self.segments[top].start, score
        return round(best, 1) if best is not None and best_score >= MIN_SCORE else None


def point_sources(points: list[str], spans: list[tuple[float, float]], matcher: Matcher) -> list[dict]:
    """``[{"text", "start"}]`` for the slide's points; ``start`` is None when no passage matches well."""
    return [{"text": p, "start": matcher.locate(p, spans)} for p in points]
