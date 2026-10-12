"""Prompt construction for transcript -> presentation outline."""

from __future__ import annotations

from .base import OutlineRequest

LANG_NAMES = {"vi": "Vietnamese (tiếng Việt)", "en": "English"}

SYSTEM_PROMPT = """You are an expert instructional designer and presentation writer.
You turn a timestamped video transcript into a well-structured slide deck that a
presenter could deliver to explain the same material.

STRICT RULES
1. Faithfulness: use ONLY information present in the transcript. Never invent
   statistics, numbers, quotes, names, dates, references or examples. If the
   transcript is vague, keep the slide general rather than filling gaps.
2. Summarize and restructure. Do NOT copy long transcript sentences. Slide text
   must be short: titles <= 8 words, bullets <= 14 words, max 5 bullets.
3. Fix speech-recognition errors only when the intended word is obvious.
4. Put fuller explanations in speaker_notes (2-5 sentences, natural spoken style).
5. Choose layout_type from the CONTENT, and vary layouts across the deck:
   - title: first slide only (title + subtitle summarizing the video).
   - agenda: second slide for decks of 8+ slides; key_points = the main parts, in order (3-6, short).
   - section: divider before a major part (title + optional subtitle).
   - bullets: a set of related points.
   - concept: one key term or idea explained. title = the term, subtitle = a one-sentence
     definition in plain words, items = 2-4 characteristics/components (heading + short text).
   - key_insight: ONE important message the speaker emphasises. subtitle = that message as a
     short statement (<= 20 words, paraphrased, not a fake quote), key_points = 0-3 supporting points.
   - image_left / image_right: a point best illustrated by a moment of the
     video; set visual_type="video_frame" and source_timestamps to the seconds
     where that moment happens.
   - two_column: a comparison/contrast (left_title/left_points vs right_title/right_points).
   - process: ordered steps (items: heading=step name, text=short detail), 3-6 items.
   - timeline: chronological events (items: heading=time/phase, text=event), 3-6 items.
   - cards: 3-6 parallel concepts (items: heading=concept, text=one-line explanation).
   - stats: ONLY when the transcript states concrete numbers (items: heading=the exact
     number as said, text=what it measures). Never use stats otherwise.
   - summary: key takeaways (3-6 key_points).
   - conclusion: the very last slide when the speaker draws a conclusion or calls to action:
     title = closing message, subtitle optional, key_points = 1-3 final statements.
   Never use the same layout on more than two consecutive slides.
6. visual_type: "video_frame" for image layouts, "diagram" for process/timeline/
   two_column/cards/concept, "icon" for stats, otherwise "none". visual_description briefly
   says what the visual should show.
7. source_timestamps: seconds (numbers) in the transcript that support the slide.
8. Every slide must have content; no empty slides.
"""


def build_user_prompt(req: OutlineRequest) -> str:
    t = req.transcript
    lang = req.language if req.language != "auto" else (t.language or "vi")
    lang_name = LANG_NAMES.get(lang, lang)
    if req.slide_count:
        count_rule = f"Create exactly {req.slide_count} slides (including title and summary)."
    else:
        count_rule = "Choose the number of slides that fits the content (typically 6-15)."

    lines = [f"[{s.start:.1f}] {s.text}" for s in t.segments]
    frames = ", ".join(f"{ts:.1f}" for ts in req.frame_timestamps) or "none"

    return f"""Write all slide text and speaker notes in {lang_name}.
{count_rule}
Presentation style requested: {req.style}.
{"Use this presentation title: " + req.title_hint if req.title_hint else "Derive a clear presentation title from the content."}
Video duration: {t.duration:.0f} seconds.
Screenshots are available from the video at these seconds: {frames}.
Prefer image layouts whose source_timestamps are close to one of those seconds.

{illustration_rule(req.max_illustrations)}
TRANSCRIPT (each line starts with its start time in seconds):
{chr(10).join(lines)}
"""


def illustration_rule(max_images: int) -> str:
    """Extra instructions when AI illustrations are requested (empty otherwise)."""
    if max_images <= 0:
        return ""
    return f"""AI ILLUSTRATIONS: an image model will draw pictures for some slides.
- Choose at most {max_images} slides where a picture genuinely helps understanding or mood:
  the title slide and concept/example slides. Never for stats, timeline, process,
  two_column, cards, agenda, key_insight, summary or conclusion slides.
- On those slides set visual_type="illustration", use layout image_left, image_right or
  concept (or keep layout title for the first slide), and fill image_prompt.
- image_prompt: ALWAYS in English, 15-40 words, one concrete visual scene or metaphor
  (subject, setting, mood) that matches the slide content. Describe only what is seen.
  Never ask for text, letters, words, numbers, labels, logos, charts, tables, user
  interfaces or slide titles inside the picture. No real, identifiable people.
- Leave image_prompt empty on every other slide.
"""


def build_repair_prompt(error: str) -> str:
    return (
        "Your previous JSON did not pass validation:\n"
        f"{error[:1500]}\n"
        "Return the corrected full JSON object only, following every rule above."
    )
