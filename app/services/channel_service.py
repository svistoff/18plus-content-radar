"""Watchlist channels: add channels by URL and monitor their new uploads.

Uploads are fetched via the channel's uploads playlist (cheap: ~1-2 quota units
per channel) rather than search (100 units), so daily monitoring is inexpensive.
Watchlist videos are stored regardless of virality; Shorts are still skipped
(poor article material) but the language filter is not applied — the channel is
a deliberate user choice.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Channel
from app.services.discovery_service import _safe_int, _upsert_channel, store_video
from app.services.scoring_service import recompute_all_scores
from app.services.youtube_client import YouTubeAPIError, YouTubeClient, parse_iso8601_duration
from app.services.youtube_urls import parse_channel_ref


class ChannelImportError(RuntimeError):
    pass


def add_watchlist_channel(db: Session, url: str) -> Channel:
    ref = parse_channel_ref(url)
    if not ref:
        raise ChannelImportError(
            "Не распознал ссылку на канал. Вставьте ссылку вида "
            "youtube.com/@handle или youtube.com/channel/UC..."
        )

    client = YouTubeClient()
    raw = client.resolve_channel(ref.get("external_id"), ref.get("handle"), ref.get("username"))
    if raw is None and ref.get("search"):
        raw = client.search_channel(ref["search"])
    if raw is None:
        raise ChannelImportError("Канал не найден на YouTube по этой ссылке.")

    external_channel_id = raw["id"]
    channel = db.scalar(select(Channel).where(Channel.external_channel_id == external_channel_id))
    snippet = raw.get("snippet") or {}
    subs = _safe_int((raw.get("statistics") or {}).get("subscriberCount"))
    if channel is None:
        channel = Channel(
            external_channel_id=external_channel_id,
            title=snippet.get("title") or "Без названия",
            url=f"https://www.youtube.com/channel/{external_channel_id}",
            subscriber_count=subs,
        )
        db.add(channel)
    channel.status = "watchlist"
    if subs is not None:
        channel.subscriber_count = subs
    db.commit()
    return channel


def remove_watchlist_channel(db: Session, channel_id) -> None:
    channel = db.get(Channel, channel_id)
    if channel:
        channel.status = "discovered"
        db.commit()


def list_watchlist(db: Session) -> list[Channel]:
    return db.scalars(
        select(Channel).where(Channel.status == "watchlist").order_by(Channel.title)
    ).all()


def sync_watchlist(db: Session) -> dict:
    settings = get_settings()
    client = YouTubeClient()
    channels = list_watchlist(db)
    totals = {"channels": 0, "created": 0, "updated": 0, "skipped": 0, "errors": 0}

    for channel in channels:
        totals["channels"] += 1
        try:
            _sync_one_channel(db, client, channel, settings, totals)
        except Exception:
            db.rollback()
            totals["errors"] += 1

    db.commit()
    recompute_all_scores(db)
    return totals


def _sync_one_channel(db, client, channel, settings, totals) -> None:
    raw = client.resolve_channel(channel.external_channel_id, None, None)
    if raw is None:
        return
    uploads = (
        raw.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
    )
    if not uploads:
        return
    subs = _safe_int((raw.get("statistics") or {}).get("subscriberCount"))
    if subs is not None:
        channel.subscriber_count = subs

    video_ids = client.get_recent_video_ids(uploads, max_results=15)
    items = client.get_videos(video_ids)
    channel_ids = [v.get("snippet", {}).get("channelId") for v in items]
    channels_raw = {c["id"]: c for c in client.get_channels([c for c in channel_ids if c])}

    for item in items:
        duration = parse_iso8601_duration((item.get("contentDetails") or {}).get("duration"))
        if duration is not None and duration < settings.min_duration_seconds:
            totals["skipped"] += 1  # Shorts skipped even for watchlist
            continue
        outcome = store_video(db, item, source_type="watchlist_sync", channels_raw=channels_raw)
        totals["created"] += outcome == "created"
        totals["updated"] += outcome == "updated"
