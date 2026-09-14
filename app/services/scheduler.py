"""Background scheduler: periodic discovery and metric refresh.

Uses APScheduler inside the web process (no separate worker/broker needed for
the MVP). Each job opens its own DB session and swallows/logs errors so a single
failed run never kills the scheduler thread.
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler

from app.config import get_settings
from app.database import SessionLocal
from app.services.channel_service import sync_watchlist
from app.services.discovery_service import run_all_enabled_queries
from app.services.metrics_service import refresh_all_metrics

logger = logging.getLogger("radar.scheduler")


def _discovery_job() -> None:
    try:
        with SessionLocal() as db:
            totals = run_all_enabled_queries(db)
        logger.info("scheduled discovery: %s", totals)
    except Exception:
        logger.exception("scheduled discovery failed")


def _metrics_job() -> None:
    try:
        with SessionLocal() as db:
            totals = refresh_all_metrics(db)
        logger.info("scheduled metrics refresh: %s", totals)
    except Exception:
        logger.exception("scheduled metrics refresh failed")


def _watchlist_job() -> None:
    try:
        with SessionLocal() as db:
            totals = sync_watchlist(db)
        logger.info("scheduled watchlist sync: %s", totals)
    except Exception:
        logger.exception("scheduled watchlist sync failed")


def build_scheduler() -> BackgroundScheduler | None:
    settings = get_settings()
    if not settings.scheduler_enabled:
        logger.info("scheduler disabled via SCHEDULER_ENABLED")
        return None

    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        _discovery_job,
        "interval",
        hours=max(1, settings.search_interval_hours),
        id="discovery",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        _metrics_job,
        "interval",
        hours=max(1, settings.metrics_interval_hours),
        id="metrics",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        _watchlist_job,
        "interval",
        hours=max(1, settings.watchlist_interval_hours),
        id="watchlist",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    logger.info(
        "scheduler started: discovery every %sh, metrics every %sh, watchlist every %sh",
        settings.search_interval_hours,
        settings.metrics_interval_hours,
        settings.watchlist_interval_hours,
    )
    return scheduler
