"""Runs a search query against YouTube and stores/updates videos and channels."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Channel, SearchQuery, Video, VideoMetricSnapshot
from app.services.scoring_service import recompute_all_scores
from app.services.youtube_client import YouTubeClient, parse_iso8601_duration


def run_all_enabled_queries(db: Session) -> dict:
    """Runs discovery for every enabled search query. Used by the scheduler and
    the manual 'search now' button. One failing query does not abort the rest."""
    queries = db.scalars(
        select(SearchQuery).where(SearchQuery.enabled.is_(True)).order_by(SearchQuery.priority.desc())
    ).all()
    totals = {"queries": 0, "found": 0, "created": 0, "updated": 0, "skipped": 0, "errors": 0, "error_sample": None}
    for query in queries:
        totals["queries"] += 1
        try:
            result = run_search_for_query(db, query)
        except Exception as exc:
            db.rollback()
            totals["errors"] += 1
            if totals["error_sample"] is None:
                totals["error_sample"] = str(exc)
            continue
        totals["found"] += result["found"]
        totals["created"] += result["created"]
        totals["updated"] += result["updated"]
        totals["skipped"] += result.get("skipped", 0)
    return totals


def run_search_for_query(db: Session, query: SearchQuery) -> dict:
    settings = get_settings()
    client = YouTubeClient()
    video_ids = client.search_video_ids(query.query_text, query.language)
    videos_raw = client.get_videos(video_ids)

    channel_ids = [v["snippet"]["channelId"] for v in videos_raw if v.get("snippet")]
    channels_raw = {c["id"]: c for c in client.get_channels(channel_ids)}

    created = 0
    updated = 0
    skipped = 0

    for item in videos_raw:
        snippet = item.get("snippet") or {}
        content_details = item.get("contentDetails") or {}
        if not item.get("id") or not snippet.get("channelId"):
            continue

        duration_seconds = parse_iso8601_duration(content_details.get("duration"))
        if not _is_eligible(duration_seconds, snippet, settings):
            skipped += 1
            continue

        outcome = store_video(
            db, item, source_type="topic_search",
            channels_raw=channels_raw, discovered_by_query=query,
        )
        created += outcome == "created"
        updated += outcome == "updated"

    db.commit()
    recompute_all_scores(db)
    return {"found": len(videos_raw), "created": created, "updated": updated, "skipped": skipped}


def store_video(db: Session, item: dict, source_type: str, channels_raw: dict | None = None,
                discovered_by_query: SearchQuery | None = None) -> str:
    """Upserts one YouTube video item (+ its channel and a metric snapshot).
    Shared by topic search, watchlist sync and manual import."""
    snippet = item.get("snippet") or {}
    statistics = item.get("statistics") or {}
    content_details = item.get("contentDetails") or {}
    external_video_id = item["id"]
    external_channel_id = snippet["channelId"]

    channel = _upsert_channel(
        db, external_channel_id, snippet, (channels_raw or {}).get(external_channel_id, {})
    )

    view_count = _safe_int(statistics.get("viewCount")) or 0
    like_count = _safe_int(statistics.get("likeCount")) or 0
    comment_count = _safe_int(statistics.get("commentCount")) or 0
    duration_seconds = parse_iso8601_duration(content_details.get("duration"))
    thumbnail_url = _pick_thumbnail(snippet.get("thumbnails"))

    video = db.scalar(select(Video).where(Video.external_video_id == external_video_id))
    if video is None:
        video = Video(
            external_video_id=external_video_id,
            channel=channel,
            discovered_by_query=discovered_by_query,
            source_type=source_type,
            title=snippet.get("title") or "Без названия",
            description=snippet.get("description"),
            url=f"https://www.youtube.com/watch?v={external_video_id}",
            thumbnail_url=thumbnail_url,
            published_at=_parse_datetime(snippet.get("publishedAt")),
            duration_seconds=duration_seconds,
            view_count=view_count,
            like_count=like_count,
            comment_count=comment_count,
        )
        db.add(video)
        outcome = "created"
    else:
        video.view_count = view_count
        video.like_count = like_count
        video.comment_count = comment_count
        video.thumbnail_url = thumbnail_url or video.thumbnail_url
        video.duration_seconds = duration_seconds or video.duration_seconds
        outcome = "updated"

    video.metric_snapshots.append(
        VideoMetricSnapshot(view_count=view_count, like_count=like_count, comment_count=comment_count)
    )
    return outcome


def _is_eligible(duration_seconds: int | None, snippet: dict, settings) -> bool:
    if duration_seconds is not None and duration_seconds < settings.min_duration_seconds:
        return False
    allowed = settings.allowed_language_set
    if not allowed:
        return True
    declared = snippet.get("defaultAudioLanguage") or snippet.get("defaultLanguage")
    text = f"{snippet.get('title', '')} {snippet.get('description', '')}"
    return _language_allowed(declared, text, allowed)


def _language_allowed(declared: str | None, text: str, allowed: set[str]) -> bool:
    """Keep the video if its language is allowed. Prefer the declared metadata
    language; otherwise detect from title+description. When detection is
    uncertain we keep the video (avoid false negatives on good content)."""
    if declared:
        return declared.split("-")[0].lower() in allowed
    detected = _detect_language(text)
    if detected is None:
        return True
    return detected in allowed


def _detect_language(text: str) -> str | None:
    text = (text or "").strip()
    if len(text) < 15:
        return None
    try:
        from langdetect import DetectorFactory, detect_langs

        DetectorFactory.seed = 0
        ranked = detect_langs(text[:800])
    except Exception:
        return None
    if not ranked:
        return None
    top = ranked[0]
    # Only trust a confident guess, so we don't wrongly drop good videos.
    if top.prob < 0.85:
        return None
    return top.lang.split("-")[0].lower()


def cleanup_ineligible_videos(db: Session) -> int:
    """Removes already-stored videos that would be filtered out today (Shorts /
    foreign language). Cascades to their snapshots, transcripts and packs."""
    settings = get_settings()
    allowed = settings.allowed_language_set
    removed = 0
    for video in db.scalars(select(Video)).all():
        if video.source_type == "manual_import":
            continue  # manually added videos are a deliberate choice — never auto-remove
        too_short = (
            video.duration_seconds is not None
            and video.duration_seconds < settings.min_duration_seconds
        )
        foreign = bool(allowed) and not _language_allowed(
            None, f"{video.title} {video.description or ''}", allowed
        )
        if too_short or foreign:
            db.delete(video)
            removed += 1
    db.commit()
    if removed:
        recompute_all_scores(db)
    return removed


def _upsert_channel(db: Session, external_channel_id: str, video_snippet: dict, channel_raw: dict) -> Channel:
    channel = db.scalar(select(Channel).where(Channel.external_channel_id == external_channel_id))
    channel_stats = channel_raw.get("statistics") or {}
    channel_snippet = channel_raw.get("snippet") or {}
    subscriber_count = _safe_int(channel_stats.get("subscriberCount"))

    if channel is None:
        channel = Channel(
            external_channel_id=external_channel_id,
            title=channel_snippet.get("title") or video_snippet.get("channelTitle") or "Без названия",
            url=f"https://www.youtube.com/channel/{external_channel_id}",
            subscriber_count=subscriber_count,
        )
        db.add(channel)
        db.flush()
    elif subscriber_count is not None:
        channel.subscriber_count = subscriber_count
    return channel


def _safe_int(value) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _pick_thumbnail(thumbnails: dict | None) -> str | None:
    if not thumbnails:
        return None
    for key in ("medium", "high", "default"):
        if key in thumbnails:
            return thumbnails[key].get("url")
    return None
