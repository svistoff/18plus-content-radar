"""Re-samples metrics for videos already in the database.

Discovery only sees whatever the search returns on a given day, so growth of a
specific video is measured here: we re-fetch its current view/like/comment
counts, append a metric snapshot, and let scoring turn the gap between snapshots
into a growth signal. This is the job that makes "views per hour between
snapshots" and anomaly detection possible over time.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Video, VideoMetricSnapshot
from app.services.scoring_service import recompute_all_scores
from app.services.youtube_client import YouTubeClient


def refresh_all_metrics(db: Session) -> dict:
    videos = db.scalars(select(Video)).all()
    by_external = {v.external_video_id: v for v in videos}
    if not by_external:
        return {"tracked": 0, "refreshed": 0}

    client = YouTubeClient()
    fresh = client.get_videos(list(by_external.keys()))

    refreshed = 0
    for item in fresh:
        video = by_external.get(item.get("id"))
        if video is None:
            continue
        statistics = item.get("statistics") or {}
        view_count = _safe_int(statistics.get("viewCount")) or 0
        like_count = _safe_int(statistics.get("likeCount")) or 0
        comment_count = _safe_int(statistics.get("commentCount")) or 0

        video.view_count = view_count
        video.like_count = like_count
        video.comment_count = comment_count
        video.metric_snapshots.append(
            VideoMetricSnapshot(
                view_count=view_count,
                like_count=like_count,
                comment_count=comment_count,
            )
        )
        refreshed += 1

    db.commit()
    recompute_all_scores(db)
    return {"tracked": len(by_external), "refreshed": refreshed}


def _safe_int(value) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
