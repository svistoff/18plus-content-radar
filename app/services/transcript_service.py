"""Fetches a video transcript, resilient to YouTube blocking a server IP.

Order of attempts:
1. Apify (if configured) — fetches captions on Apify's infrastructure, which
   bypasses the HTTP 429 that YouTube returns to datacenter IPs, and costs a
   fraction of a cent per transcript.
2. yt-dlp captions — direct caption download; works when the IP isn't blocked.
3. Audio STT (OpenAI-compatible, e.g. Groq whisper-large-v3-turbo) — for videos
   with no captions at all.

The transcript is a research source only.
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import tempfile

import httpx
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Transcript, Video

PREFERRED_LANGUAGES = ["ru", "en"]
PREFERRED_FORMATS = ["json3", "srv3", "srv1", "vtt"]


class TranscriptUnavailable(RuntimeError):
    """No usable transcript could be obtained for the video."""


def fetch_and_store_transcript(db: Session, video: Video) -> Transcript:
    settings = get_settings()
    languages = list(dict.fromkeys([settings.youtube_default_language, *PREFERRED_LANGUAGES]))

    segments = language = provider = None
    caption_error = None

    if settings.apify_token:
        try:
            segments, language = _from_apify(video.external_video_id)
            provider = "apify"
        except TranscriptUnavailable as exc:
            caption_error = exc
    else:
        try:
            segments, language = _fetch_segments(video.external_video_id, languages)
            provider = "yt_dlp_captions"
        except TranscriptUnavailable as exc:
            caption_error = exc

    if segments is None:
        transcribe_key, _ = settings.transcribe_credentials()
        if settings.whisper_fallback and transcribe_key:
            try:
                segments, language = _transcribe_with_whisper(video.external_video_id)
                provider = "audio_stt"
            except TranscriptUnavailable as whisper_exc:
                raise TranscriptUnavailable(
                    f"Субтитры недоступны ({caption_error}). "
                    f"Распознавание аудио тоже не удалось ({whisper_exc})."
                ) from whisper_exc
        else:
            raise caption_error or TranscriptUnavailable("Транскрипт недоступен.")

    raw_text = " ".join(seg["text"].strip() for seg in segments if seg.get("text")).strip()
    if not raw_text:
        raise TranscriptUnavailable("Транскрипт получен, но оказался пустым.")

    transcript = video.transcript
    if transcript is None:
        transcript = Transcript(video=video)
        db.add(transcript)

    transcript.provider = provider
    transcript.language = language
    transcript.raw_text = raw_text
    transcript.segments = json.dumps(segments, ensure_ascii=False)
    transcript.status = "ready"

    video.workflow_status = "transcript_ready"
    db.commit()
    return transcript


def _from_apify(video_id: str) -> tuple[list[dict], str | None]:
    """Runs an Apify YouTube-transcript actor server-side (bypasses the 429 that
    YouTube returns to our IP) and parses whatever shape it returns."""
    settings = get_settings()
    endpoint = (
        f"https://api.apify.com/v2/acts/{settings.apify_actor}"
        f"/run-sync-get-dataset-items?token={settings.apify_token}"
    )
    payload = {"videoUrl": f"https://www.youtube.com/watch?v={video_id}"}
    try:
        response = httpx.post(endpoint, json=payload, timeout=180.0)
    except httpx.HTTPError as exc:
        raise TranscriptUnavailable(f"Apify недоступен: {exc}") from exc
    if response.status_code not in (200, 201):
        raise TranscriptUnavailable(
            f"Apify вернул {response.status_code}: {response.text[:200]}"
        )
    try:
        data = response.json()
    except ValueError as exc:
        raise TranscriptUnavailable("Apify вернул не-JSON ответ") from exc

    segments = _parse_apify(data)
    if not segments:
        raise TranscriptUnavailable(
            "Apify не вернул субтитры (у видео их нет или изменился формат актора). "
            f"Ответ: {str(data)[:200]}"
        )
    return segments, None


def _parse_apify(data) -> list[dict]:
    segments: list[dict] = []
    items = data if isinstance(data, list) else [data]
    for item in items:
        if isinstance(item, str):
            _push(segments, item)
            continue
        if not isinstance(item, dict):
            continue
        for key in ("data", "segments", "captions", "transcript", "subtitles"):
            value = item.get(key)
            if isinstance(value, list):
                _push_segments(segments, value)
        if isinstance(item.get("transcript"), str):
            _push(segments, item["transcript"])
        text = item.get("text")
        if isinstance(text, str):
            if any(k in item for k in ("start", "offset", "startMs", "dur", "duration")):
                _push_segments(segments, [item])
            elif not segments:
                _push(segments, text)
    return segments


def _push_segments(segments: list[dict], seglist) -> None:
    for seg in seglist:
        if isinstance(seg, str):
            _push(segments, seg)
            continue
        if not isinstance(seg, dict):
            continue
        start = seg.get("start", seg.get("offset"))
        if start is None and seg.get("startMs") is not None:
            start = _f(seg["startMs"]) / 1000
        dur = seg.get("dur", seg.get("duration"))
        if dur is None and seg.get("durationMs") is not None:
            dur = _f(seg["durationMs"]) / 1000
        _push(segments, seg.get("text") or seg.get("utf8"), start, dur)


def _push(segments: list[dict], text, start=0, dur=0) -> None:
    clean = (text or "").strip()
    if not clean:
        return
    start_s = _f(start)
    segments.append(
        {"start_seconds": round(start_s, 2), "end_seconds": round(start_s + _f(dur), 2), "text": clean}
    )


def _f(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _transcribe_with_whisper(video_id: str) -> tuple[list[dict], str | None]:
    """Downloads a low-bitrate audio track (via a CDN path that is not the
    rate-limited caption endpoint) and transcribes it with an OpenAI-compatible
    STT model (OpenAI Whisper or, cheaper, Groq whisper-large-v3-turbo)."""
    settings = get_settings()
    try:
        import yt_dlp
    except ImportError as exc:  # pragma: no cover
        raise TranscriptUnavailable("Библиотека yt-dlp не установлена") from exc

    tmpdir = tempfile.mkdtemp(prefix="audio_")
    try:
        opts = {
            "format": "139/bestaudio[abr<=70]/bestaudio/best",
            "outtmpl": os.path.join(tmpdir, "%(id)s.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            "retries": 5,
            "extractor_retries": 3,
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([f"https://www.youtube.com/watch?v={video_id}"])
        except Exception as exc:
            raise TranscriptUnavailable(f"не удалось скачать аудио: {str(exc)[:160]}") from exc

        files = [p for p in glob.glob(os.path.join(tmpdir, f"{video_id}.*"))]
        if not files:
            raise TranscriptUnavailable("аудио не скачалось")
        audio_path = files[0]
        if os.path.getsize(audio_path) > 24 * 1024 * 1024:
            raise TranscriptUnavailable(
                "ролик слишком длинный для распознавания одним запросом (аудио > 24 МБ)"
            )

        from openai import OpenAI

        api_key, base_url = settings.transcribe_credentials()
        client_kwargs = {"api_key": api_key}
        if base_url:
            client_kwargs["base_url"] = base_url
        client = OpenAI(**client_kwargs)

        try:
            with open(audio_path, "rb") as fh:
                result = client.audio.transcriptions.create(
                    model=settings.whisper_model,
                    file=fh,
                    response_format="verbose_json",
                )
        except Exception as exc:
            raise TranscriptUnavailable(f"ошибка Whisper: {str(exc)[:160]}") from exc

        segments = _whisper_segments(result)
        language = getattr(result, "language", None)
        if not segments:
            raise TranscriptUnavailable("Whisper вернул пустой результат")
        return segments, language
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _whisper_segments(result) -> list[dict]:
    segments = []
    for seg in getattr(result, "segments", None) or []:
        text = (getattr(seg, "text", None) or (seg.get("text") if isinstance(seg, dict) else "")).strip()
        if not text:
            continue
        start = getattr(seg, "start", None) if not isinstance(seg, dict) else seg.get("start", 0)
        end = getattr(seg, "end", None) if not isinstance(seg, dict) else seg.get("end", 0)
        segments.append(
            {"start_seconds": round(float(start or 0), 2), "end_seconds": round(float(end or 0), 2), "text": text}
        )
    if not segments:
        text = (getattr(result, "text", "") or "").strip()
        if text:
            segments = [{"start_seconds": 0.0, "end_seconds": 0.0, "text": text}]
    return segments


def _fetch_segments(video_id: str, languages: list[str]) -> tuple[list[dict], str | None]:
    try:
        import yt_dlp
    except ImportError as exc:  # pragma: no cover
        raise TranscriptUnavailable("Библиотека yt-dlp не установлена") from exc

    tmpdir = tempfile.mkdtemp(prefix="subs_")
    try:
        opts = {
            "skip_download": True,
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": languages,
            "subtitlesformat": "json3/srv3/vtt/best",
            "outtmpl": os.path.join(tmpdir, "%(id)s.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            "retries": 5,
            "extractor_retries": 3,
            "sleep_interval_subtitles": 1,
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([f"https://www.youtube.com/watch?v={video_id}"])
        except Exception as exc:
            raise TranscriptUnavailable(
                f"Не удалось скачать субтитры: {str(exc)[:200]}"
            ) from exc

        path, language, fmt = _pick_file(glob.glob(os.path.join(tmpdir, f"{video_id}.*")), languages)
        if not path:
            raise TranscriptUnavailable("У видео нет субтитров на русском или английском.")

        with open(path, encoding="utf-8") as fh:
            content = fh.read()
        segments = _parse(content, fmt)
        if not segments:
            raise TranscriptUnavailable("Субтитры получены, но оказались пустыми.")
        return segments, language
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _pick_file(files: list[str], languages: list[str]) -> tuple[str | None, str | None, str | None]:
    entries = []
    for path in files:
        parts = os.path.basename(path).split(".")
        if len(parts) >= 3:
            entries.append((parts[-2], parts[-1], path))  # (lang, ext, path)
    if not entries:
        return None, None, None

    for want in languages:
        candidates = [e for e in entries if e[0].split("-")[0].lower() == want.lower()]
        if not candidates:
            continue
        for fmt in PREFERRED_FORMATS:
            for lang, ext, path in candidates:
                if ext == fmt:
                    return path, lang, ext
        lang, ext, path = candidates[0]
        return path, lang, ext

    lang, ext, path = entries[0]
    return path, lang, ext


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
    for block in content.split("\n\n"):
        lines = [ln for ln in block.splitlines() if ln.strip()]
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
