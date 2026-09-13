"""Fetches a video transcript from YouTube captions via yt-dlp.

yt-dlp is used instead of youtube-transcript-api because the latter scrapes
YouTube's timedtext endpoint, which returns empty responses from datacenter /
VPS IPs. yt-dlp uses the innertube player API with client fallbacks and is far
more reliable from a server. The transcript is a research source only; if a
video has no accessible captions we mark it so the UI can explain why (a
Whisper/STT fallback can be layered on later).
"""

from __future__ import annotations

import json

import httpx
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Transcript, Video

PREFERRED_LANGUAGES = ["ru", "en"]
PREFERRED_FORMATS = ["json3", "srv3", "srv1", "vtt"]
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)


class TranscriptUnavailable(RuntimeError):
    """No usable transcript could be obtained for the video."""


def fetch_and_store_transcript(db: Session, video: Video) -> Transcript:
    settings = get_settings()
    languages = list(dict.fromkeys([settings.youtube_default_language, *PREFERRED_LANGUAGES]))

    segments, language = _fetch_segments(video.external_video_id, languages)
    raw_text = " ".join(seg["text"].strip() for seg in segments if seg.get("text")).strip()
    if not raw_text:
        raise TranscriptUnavailable(
            "У видео нет доступных субтитров. Позже можно добавить распознавание "
            "аудио (Whisper) для таких роликов."
        )

    transcript = video.transcript
    if transcript is None:
        transcript = Transcript(video=video)
        db.add(transcript)

    transcript.provider = "yt_dlp_captions"
    transcript.language = language
    transcript.raw_text = raw_text
    transcript.segments = json.dumps(segments, ensure_ascii=False)
    transcript.status = "ready"

    video.workflow_status = "transcript_ready"
    db.commit()
    return transcript


def _fetch_segments(video_id: str, languages: list[str]) -> tuple[list[dict], str | None]:
    tracks_by_lang, is_auto = _extract_caption_tracks(video_id)
    if not tracks_by_lang:
        raise TranscriptUnavailable("У видео нет доступных субтитров.")

    chosen_lang = _pick_language(list(tracks_by_lang.keys()), languages)
    track_url, fmt = _pick_track(tracks_by_lang[chosen_lang])
    if not track_url:
        raise TranscriptUnavailable("У видео нет субтитров в поддерживаемом формате.")

    content = _download(track_url)
    segments = _parse(content, fmt)
    if not segments:
        raise TranscriptUnavailable("Субтитры получены, но оказались пустыми.")
    return segments, chosen_lang


def _extract_caption_tracks(video_id: str) -> tuple[dict, bool]:
    try:
        import yt_dlp
    except ImportError as exc:  # pragma: no cover
        raise TranscriptUnavailable("Библиотека yt-dlp не установлена") from exc

    opts = {
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(
                f"https://www.youtube.com/watch?v={video_id}", download=False
            )
    except Exception as exc:
        raise TranscriptUnavailable(
            f"Не удалось получить данные видео для субтитров: {str(exc)[:200]}"
        ) from exc

    manual = info.get("subtitles") or {}
    if manual:
        return manual, False
    return (info.get("automatic_captions") or {}), True


def _pick_language(available: list[str], preferred: list[str]) -> str:
    normalized = {lang.split("-")[0]: lang for lang in available}
    for want in preferred:
        if want in normalized:
            return normalized[want]
    return available[0]


def _pick_track(tracks: list[dict]) -> tuple[str | None, str | None]:
    for fmt in PREFERRED_FORMATS:
        for track in tracks:
            if track.get("ext") == fmt and track.get("url"):
                return track["url"], fmt
    for track in tracks:
        if track.get("url"):
            return track["url"], track.get("ext")
    return None, None


def _download(url: str) -> str:
    try:
        response = httpx.get(url, headers={"User-Agent": USER_AGENT}, timeout=25.0)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise TranscriptUnavailable(f"Не удалось скачать субтитры: {exc}") from exc
    return response.text


def _parse(content: str, fmt: str | None) -> list[dict]:
    stripped = content.lstrip()
    if stripped.startswith("{"):
        return _parse_json3(content)
    if stripped.startswith("<"):
        return _parse_xml(content)
    return _parse_vtt(content)


def _parse_json3(content: str) -> list[dict]:
    data = json.loads(content)
    segments: list[dict] = []
    for event in data.get("events", []):
        segs = event.get("segs") or []
        text = "".join(s.get("utf8", "") for s in segs).strip()
        if not text:
            continue
        start = event.get("tStartMs", 0) / 1000
        duration = event.get("dDurationMs", 0) / 1000
        segments.append(
            {"start_seconds": round(start, 2), "end_seconds": round(start + duration, 2), "text": text}
        )
    return segments


def _parse_xml(content: str) -> list[dict]:
    import xml.etree.ElementTree as ET

    segments: list[dict] = []
    root = ET.fromstring(content)
    for node in root.iter("text"):
        text = "".join(node.itertext()).strip()
        if not text:
            continue
        start = float(node.get("start", 0.0))
        duration = float(node.get("dur", 0.0))
        segments.append(
            {"start_seconds": round(start, 2), "end_seconds": round(start + duration, 2), "text": text}
        )
    return segments


def _parse_vtt(content: str) -> list[dict]:
    segments: list[dict] = []
    blocks = content.split("\n\n")
    for block in blocks:
        lines = [ln for ln in block.splitlines() if ln.strip()]
        if not lines:
            continue
        timing = next((ln for ln in lines if "-->" in ln), None)
        if not timing:
            continue
        idx = lines.index(timing)
        text = " ".join(lines[idx + 1 :]).strip()
        if not text:
            continue
        start = _vtt_time(timing.split("-->")[0].strip())
        end = _vtt_time(timing.split("-->")[1].strip().split(" ")[0])
        segments.append({"start_seconds": start, "end_seconds": end, "text": text})
    return segments


def _vtt_time(value: str) -> float:
    value = value.replace(",", ".")
    parts = value.split(":")
    try:
        if len(parts) == 3:
            h, m, s = parts
            return round(int(h) * 3600 + int(m) * 60 + float(s), 2)
        if len(parts) == 2:
            m, s = parts
            return round(int(m) * 60 + float(s), 2)
    except ValueError:
        return 0.0
    return 0.0
