"""Fetches a video transcript from YouTube's own captions (free, no API key).

The transcript is a research source only. If a video has no captions we mark it
so the UI can explain why, rather than silently failing. A Whisper/STT fallback
for caption-less videos can be layered on later.
"""

from __future__ import annotations

import json

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Transcript, Video

PREFERRED_LANGUAGES = ["ru", "en"]


class TranscriptUnavailable(RuntimeError):
    """No usable transcript could be obtained for the video."""


def fetch_and_store_transcript(db: Session, video: Video) -> Transcript:
    settings = get_settings()
    languages = list(dict.fromkeys([settings.youtube_default_language, *PREFERRED_LANGUAGES]))

    segments, language = _fetch_segments(video.external_video_id, languages)
    raw_text = " ".join(seg["text"].strip() for seg in segments if seg.get("text")).strip()
    if not raw_text:
        raise TranscriptUnavailable(
            "У видео нет доступных субтитров. Позже можно будет добавить "
            "распознавание аудио (Whisper) для таких роликов."
        )

    transcript = video.transcript
    if transcript is None:
        transcript = Transcript(video=video)
        db.add(transcript)

    transcript.provider = "youtube_captions"
    transcript.language = language
    transcript.raw_text = raw_text
    transcript.segments = json.dumps(segments, ensure_ascii=False)
    transcript.status = "ready"

    video.workflow_status = "transcript_ready"
    db.commit()
    return transcript


def _fetch_segments(video_id: str, languages: list[str]) -> tuple[list[dict], str | None]:
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError as exc:  # pragma: no cover
        raise TranscriptUnavailable("Библиотека youtube-transcript-api не установлена") from exc

    from youtube_transcript_api._errors import (
        NoTranscriptFound,
        TranscriptsDisabled,
        VideoUnavailable,
    )

    try:
        raw = YouTubeTranscriptApi.get_transcript(video_id, languages=languages)
        language = languages[0]
    except NoTranscriptFound:
        raw, language = _fetch_any_language(video_id)
    except (TranscriptsDisabled, VideoUnavailable) as exc:
        raise TranscriptUnavailable(
            "Для этого видео субтитры отключены или оно недоступно."
        ) from exc
    except Exception as exc:  # network / parsing / unexpected
        raise TranscriptUnavailable(f"Не удалось получить субтитры: {exc}") from exc

    segments = [
        {
            "start_seconds": round(float(item.get("start", 0.0)), 2),
            "end_seconds": round(float(item.get("start", 0.0)) + float(item.get("duration", 0.0)), 2),
            "text": item.get("text", ""),
        }
        for item in raw
    ]
    return segments, language


def _fetch_any_language(video_id: str) -> tuple[list[dict], str | None]:
    from youtube_transcript_api import YouTubeTranscriptApi
    from youtube_transcript_api._errors import NoTranscriptFound

    try:
        listing = YouTubeTranscriptApi.list_transcripts(video_id)
        transcript = next(iter(listing))
        return transcript.fetch(), transcript.language_code
    except NoTranscriptFound as exc:
        raise TranscriptUnavailable("У видео нет доступных субтитров.") from exc
