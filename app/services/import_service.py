"""Manual import: add any YouTube video by URL, bypassing discovery filters."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Video
from app.services.discovery_service import store_video
from app.services.scoring_service import recompute_all_scores
from app.services.youtube_client import YouTubeClient
from app.services.youtube_urls import parse_video_id


class ImportError_(RuntimeError):
    pass


def import_video_by_url(db: Session, url: str) -> Video:
    video_id = parse_video_id(url)
    if not video_id:
        raise ImportError_(
            "Не распознал ссылку на видео. Вставьте ссылку вида "
            "https://www.youtube.com/watch?v=... или https://youtu.be/..."
        )

    client = YouTubeClient()
    items = client.get_videos([video_id])
    if not items:
        raise ImportError_("Видео не найдено или недоступно (приватное/удалённое).")

    item = items[0]
    channel_ids = [item.get("snippet", {}).get("channelId")]
    channels_raw = {c["id"]: c for c in client.get_channels([c for c in channel_ids if c])}

    # Manual import is a deliberate choice: no Shorts / language filtering.
    store_video(db, item, source_type="manual_import", channels_raw=channels_raw)
    db.commit()
    recompute_all_scores(db)

    return db.scalar(select(Video).where(Video.external_video_id == video_id))
