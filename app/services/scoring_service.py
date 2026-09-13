"""Computes a 0-100 Viral Score per video with a human-readable explanation.

The score follows the simplified MVP formula from the spec, but only over the
signals we can actually measure at a given moment. Missing components (e.g. view
growth before a second metric snapshot exists, or channel out-performance for a
channel with a single known video) are dropped and the remaining weights are
renormalised, so an early score is never artificially deflated.

Signals are normalised by percentile rank within the current dataset, which
makes the score a relative ranking ("faster-growing than X% of tracked videos")
that is stable and easy to explain.
"""

from __future__ import annotations

import json
import statistics
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.models import Video

SCORING_VERSION = "mvp-1"

WEIGHTS = {
    "views_per_hour": 0.35,
    "view_growth": 0.20,
    "views_to_subscribers": 0.15,
    "comments_per_view": 0.10,
    "likes_per_view": 0.10,
    "channel_outperformance": 0.10,
}


def recompute_all_scores(db: Session) -> int:
    videos = db.scalars(
        select(Video).options(
            joinedload(Video.channel), joinedload(Video.metric_snapshots)
        )
    ).unique().all()
    if not videos:
        return 0

    now = datetime.now(timezone.utc)
    raw = {video.id: _raw_signals(video, now) for video in videos}

    ranks = {
        key: _percentile_ranks([raw[v.id][key] for v in videos])
        for key in WEIGHTS
    }

    for index, video in enumerate(videos):
        components: dict[str, float] = {}
        for key in WEIGHTS:
            if raw[video.id][key] is not None:
                components[key] = ranks[key][index]

        if components:
            weight_sum = sum(WEIGHTS[k] for k in components)
            score = sum(WEIGHTS[k] * components[k] for k in components) / weight_sum
            video.viral_score = round(score * 100)
        else:
            video.viral_score = 0

        video.score_explanation = json.dumps(
            _explain(video, raw[video.id], components, now), ensure_ascii=False
        )
        video.scored_at = now

    db.commit()
    return len(videos)


def _raw_signals(video: Video, now: datetime) -> dict[str, float | None]:
    age_hours = _age_hours(video, now)
    views = max(video.view_count, 0)

    views_per_hour = views / age_hours if age_hours else None
    likes_per_view = video.like_count / views if views else None
    comments_per_view = video.comment_count / views if views else None

    subs = video.channel.subscriber_count if video.channel else None
    views_to_subscribers = views / subs if subs else None

    return {
        "views_per_hour": views_per_hour,
        "view_growth": _view_growth_per_hour(video),
        "views_to_subscribers": views_to_subscribers,
        "comments_per_view": comments_per_view,
        "likes_per_view": likes_per_view,
        "channel_outperformance": _channel_outperformance(video),
    }


def _age_hours(video: Video, now: datetime) -> float | None:
    if not video.published_at:
        return None
    published = video.published_at
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    hours = (now - published).total_seconds() / 3600
    return max(hours, 1.0)


def _view_growth_per_hour(video: Video) -> float | None:
    snapshots = sorted(video.metric_snapshots, key=lambda s: s.captured_at)
    if len(snapshots) < 2:
        return None
    first, last = snapshots[0], snapshots[-1]
    hours = (last.captured_at - first.captured_at).total_seconds() / 3600
    if hours <= 0:
        return None
    return (last.view_count - first.view_count) / hours


def _channel_outperformance(video: Video) -> float | None:
    if not video.channel:
        return None
    peers = [v.view_count for v in video.channel.videos if v.view_count > 0]
    if len(peers) < 2:
        return None
    median = statistics.median(peers)
    if median <= 0:
        return None
    return video.view_count / median


def _percentile_ranks(values: list[float | None]) -> list[float]:
    present = [v for v in values if v is not None]
    if not present:
        return [0.0 for _ in values]
    ordered = sorted(present)
    n = len(ordered)
    ranks: list[float] = []
    for value in values:
        if value is None:
            ranks.append(0.0)
            continue
        below = sum(1 for x in ordered if x < value)
        equal = sum(1 for x in ordered if x == value)
        ranks.append((below + equal / 2) / n)
    return ranks


def _explain(
    video: Video,
    raw: dict[str, float | None],
    components: dict[str, float],
    now: datetime,
) -> list[str]:
    lines: list[str] = []

    age_hours = _age_hours(video, now)
    if age_hours is not None:
        if age_hours < 48:
            lines.append(f"Опубликовано {round(age_hours)} ч назад — свежее")
        else:
            lines.append(f"Опубликовано {round(age_hours / 24)} дн назад")

    if raw["views_per_hour"] is not None:
        lines.append(f"~{round(raw['views_per_hour']):,} просмотров/час".replace(",", " "))

    if raw["view_growth"] is not None:
        lines.append(f"Прирост ~{round(raw['view_growth']):,} просмотров/час между замерами".replace(",", " "))

    if raw["channel_outperformance"] is not None:
        lines.append(f"{raw['channel_outperformance']:.1f}× к медиане просмотров канала")

    if raw["views_to_subscribers"] is not None:
        lines.append(f"{raw['views_to_subscribers']:.1f}× к числу подписчиков канала")

    engagement = (raw["likes_per_view"] or 0) + (raw["comments_per_view"] or 0)
    if raw["likes_per_view"] is not None or raw["comments_per_view"] is not None:
        lines.append(f"Вовлечённость {engagement * 100:.1f}% (лайки+комментарии к просмотрам)")

    if raw["view_growth"] is None:
        lines.append("Динамику роста уточним со следующим замером метрик")

    return lines
