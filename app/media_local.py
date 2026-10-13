"""Light, FFmpeg-free media work on this computer with PyAV (installed with faster-whisper; bundles FFmpeg libs).

Used by the cloud path so a long video never has to be uploaded whole:
* ``probe`` / ``validate``: container, duration and streams (same rules as ``app.video.validate``);
* ``remux_audio``: copies the audio track into an .m4a WITHOUT re-encoding (measured: 92 s video, 13.7 MB ->
  0.76 MB in 0.05 s; 90 min of 67 kbps AAC is ~45 MB instead of ~0.5-1.5 GB of video);
* ``grab_frames``: a few key frames for image slides (decoded from the nearest key frame, no full decode).
Audio resampling to 16 kHz mono and speech recognition still run on the Modal media worker.
"""

from __future__ import annotations

from pathlib import Path

from .config import settings
from .video import MediaError, MediaInfo


def available() -> bool:
    try:
        import av  # noqa: F401
    except ImportError:
        return False
    return True


def probe(path: Path) -> MediaInfo:
    import av

    try:
        with av.open(str(path)) as c:
            video = next(iter(c.streams.video), None)
            audio = next(iter(c.streams.audio), None)
            duration = float(c.duration / av.time_base) if c.duration else 0.0
            if not duration and video is not None and video.duration and video.time_base:
                duration = float(video.duration * video.time_base)
            return MediaInfo(
                duration=duration,
                width=int(video.codec_context.width) if video is not None else 0,
                height=int(video.codec_context.height) if video is not None else 0,
                video_codec=video.codec_context.name if video is not None else "none",
                audio_codec=audio.codec_context.name if audio is not None else None,
                format_name=str(c.format.name or ""),
                size_bytes=path.stat().st_size,
            )
    except av.error.FFmpegError as exc:
        raise MediaError("Không đọc được tệp video (tệp hỏng hoặc không phải video hợp lệ).") from exc


def validate(path: Path, max_duration_sec: int | None = None) -> MediaInfo:
    size = path.stat().st_size
    if size == 0:
        raise MediaError("Tệp tải lên rỗng.")
    if size > settings.max_upload_bytes:
        raise MediaError(f"Tệp vượt quá {settings.max_upload_mb} MB.")
    info = probe(path)
    limit = max_duration_sec or settings.max_duration_sec
    if "mp4" not in info.format_name:
        raise MediaError("Chỉ hỗ trợ định dạng MP4.")
    if info.video_codec == "none":
        raise MediaError("Tệp không chứa luồng hình ảnh (video stream).")
    if info.duration <= 0:
        raise MediaError("Không xác định được thời lượng video.")
    if info.duration > limit:
        raise MediaError(f"Video dài {info.duration / 60:.1f} phút, vượt giới hạn {limit / 60:.0f} phút.")
    if not info.has_audio:
        raise MediaError("Video không có âm thanh nên không thể chuyển thành văn bản.")
    return info


def remux_audio(video: Path, out: Path) -> Path:
    """Copy the first audio track into an MP4/M4A container (no decoding, no quality loss)."""
    import av

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp.m4a")
    try:
        with av.open(str(video)) as inp, av.open(str(tmp), "w", format="mp4") as dst:
            src = inp.streams.audio[0]
            st = dst.add_stream_from_template(src)
            for pkt in inp.demux(src):
                if pkt.dts is None:
                    continue
                pkt.stream = st
                dst.mux(pkt)
    except (av.error.FFmpegError, IndexError) as exc:
        tmp.unlink(missing_ok=True)
        raise MediaError("Không tách được âm thanh từ video.") from exc
    if not tmp.exists() or tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        raise MediaError("Không tách được âm thanh từ video.")
    tmp.replace(out)
    return out


def grab_frames(video: Path, out_dir: Path, duration: float, candidates: int | None = None) -> list[tuple[Path, float]]:
    """Candidate frames spread over the video (same spacing as app.video.extract_frames)."""
    import av

    candidates = candidates or settings.max_frames * 2
    out_dir.mkdir(parents=True, exist_ok=True)
    margin = min(2.0, duration * 0.05)
    span = max(duration - 2 * margin, 0.1)
    grabbed: list[tuple[Path, float]] = []
    try:
        with av.open(str(video)) as inp:
            stream = inp.streams.video[0]
            stream.thread_type = "AUTO"
            for i in range(candidates):
                ts = margin + span * (i + 0.5) / candidates
                inp.seek(int(ts / stream.time_base), stream=stream, backward=True, any_frame=False)
                for frame in inp.decode(stream):
                    img = frame.to_image()
                    if img.width > settings.frame_width:
                        img = img.resize((settings.frame_width, int(img.height * settings.frame_width / img.width)))
                    target = out_dir / f"cand_{i:02d}.jpg"
                    img.convert("RGB").save(target, "JPEG", quality=88)
                    actual = float(frame.time) if frame.time is not None else ts
                    grabbed.append((target, round(actual, 2)))
                    break
    except (av.error.FFmpegError, IndexError):
        pass  # frames are optional; slides then use other layouts
    return grabbed
