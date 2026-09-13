"""Runs a search query against YouTube and stores/updates videos and channels."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Channel, SearchQuery, Video
from app.services.youtube_client import YouTubeClient, parse_iso8601_duration


def run_search_for_query(db: Session, query: SearchQuery) -> dict:
    client = YouTubeClient()
    video_ids = client.search_video_ids(query.query_text, query.language)
    videos_raw = client.get_videos(video_ids)

    channel_ids = [v["snippet"]["channelId"] for v in videos_raw if v.get("snippet")]
    channels_raw = {c["id"]: c for c in client.get_channels(channel_ids)}

    created = 0
    updated = 0

    for item in videos_raw:
        snippet = item.get("snippet") or {}
        statistics = item.get("statistics") or {}
        content_details = item.get("contentDetails") or {}
        external_video_id = item.get("id")
        external_channel_id = snippet.get("channelId")
        if not external_video_id or not external_channel_id:
            continue

        channel = _upsert_channel(db, external_channel_id, snippet, channels_raw.get(external_channel_id, {}))

        video = db.scalar(select(Video).where(Video.external_video_id == external_video_id))
        view_count = _safe_int(statistics.get("viewCount")) or 0
        published_at = _parse_datetime(snippet.get("publishedAt"))
        duration_seconds = parse_iso8601_duration(content_details.get("duration"))
        thumbnail_url = _pick_thumbnail(snippet.get("thumbnails"))

        if video is None:
            db.add(
                Video(
                    external_video_id=external_video_id,
                    channel=channel,
                    discovered_by_query=query,
                    title=snippet.get("title") or "Без названия",
                    description=snippet.get("description"),
                    url=f"https://www.youtube.com/watch?v={external_video_id}",
                    thumbnail_url=thumbnail_url,
                    published_at=published_at,
                    duration_seconds=duration_seconds,
                    view_count=view_count,
                )
            )
            created += 1
        else:
            video.view_count = view_count
            video.thumbnail_url = thumbnail_url or video.thumbnail_url
            video.duration_seconds = duration_seconds or video.duration_seconds
            updated += 1

    db.commit()
    return {"found": len(videos_raw), "created": created, "updated": updated}


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
