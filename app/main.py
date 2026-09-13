import json
import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, joinedload

from app.config import get_settings
from app.database import Base, SessionLocal, engine, get_db
from app.models import Channel, SearchQuery, Video
from app.seed import seed_search_queries
from app.services.discovery_service import run_all_enabled_queries, run_search_for_query
from app.services.metrics_service import refresh_all_metrics
from app.services.scheduler import build_scheduler
from app.services.scoring_service import recompute_all_scores
from app.services.youtube_client import YouTubeAPIError

logging.getLogger("radar").setLevel(logging.INFO)
if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.INFO)

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# Lightweight, idempotent column additions for existing Postgres deployments.
# Full Alembic migrations arrive with production hardening; until then this keeps
# an already-running database in sync with new model columns without data loss.
_POSTGRES_COLUMN_PATCHES = (
    "ALTER TABLE videos ADD COLUMN IF NOT EXISTS like_count INTEGER DEFAULT 0",
    "ALTER TABLE videos ADD COLUMN IF NOT EXISTS comment_count INTEGER DEFAULT 0",
    "ALTER TABLE videos ADD COLUMN IF NOT EXISTS score_explanation TEXT",
    "ALTER TABLE videos ADD COLUMN IF NOT EXISTS scored_at TIMESTAMPTZ",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    if engine.dialect.name == "postgresql":
        with engine.begin() as conn:
            for statement in _POSTGRES_COLUMN_PATCHES:
                conn.execute(text(statement))
    with SessionLocal() as db:
        seed_search_queries(db)
    scheduler = build_scheduler()
    try:
        yield
    finally:
        if scheduler is not None:
            scheduler.shutdown(wait=False)


app = FastAPI(title="18plus Content Radar", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    settings = get_settings()
    stats = {
        "queries": db.scalar(select(func.count()).select_from(SearchQuery)) or 0,
        "videos": db.scalar(select(func.count()).select_from(Video)) or 0,
        "watchlist": db.scalar(select(func.count()).select_from(Channel).where(Channel.status == "watchlist")) or 0,
        "selected": db.scalar(select(func.count()).select_from(Video).where(Video.workflow_status == "selected")) or 0,
    }
    schedule = {
        "enabled": settings.scheduler_enabled,
        "search_interval_hours": settings.search_interval_hours,
        "metrics_interval_hours": settings.metrics_interval_hours,
    }
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "stats": stats,
            "schedule": schedule,
            "ok_message": request.query_params.get("ok"),
            "error_message": request.query_params.get("error"),
        },
    )


@app.post("/jobs/discovery/run")
def run_discovery_now(db: Session = Depends(get_db)):
    totals = run_all_enabled_queries(db)
    if totals["found"] == 0 and totals["errors"]:
        reason = totals["error_sample"] or "все запросы завершились ошибкой"
        return RedirectResponse(f"/?error={quote(reason)}", status_code=303)
    message = (
        f"Поиск по {totals['queries']} темам: найдено {totals['found']}, "
        f"новых {totals['created']}, обновлено {totals['updated']}"
        + (f", ошибок {totals['errors']}" if totals["errors"] else "")
    )
    return RedirectResponse(f"/?ok={quote(message)}", status_code=303)


@app.post("/jobs/metrics/run")
def run_metrics_now(db: Session = Depends(get_db)):
    try:
        totals = refresh_all_metrics(db)
    except YouTubeAPIError as exc:
        return RedirectResponse(f"/?error={quote(str(exc))}", status_code=303)
    message = f"Метрики обновлены: {totals['refreshed']} из {totals['tracked']} видео"
    return RedirectResponse(f"/?ok={quote(message)}", status_code=303)


@app.get("/queries", response_class=HTMLResponse)
def queries_page(request: Request, db: Session = Depends(get_db)):
    queries = db.scalars(select(SearchQuery).order_by(SearchQuery.priority.desc(), SearchQuery.name)).all()
    return templates.TemplateResponse(
        request,
        "queries.html",
        {
            "queries": queries,
            "ok_message": request.query_params.get("ok"),
            "error_message": request.query_params.get("error"),
        },
    )


@app.post("/queries")
def create_query(
    name: str = Form(...),
    query_text: str = Form(...),
    language: str = Form("ru"),
    priority: int = Form(50),
    db: Session = Depends(get_db),
):
    if db.scalar(select(SearchQuery).where(SearchQuery.query_text == query_text.strip())):
        raise HTTPException(status_code=409, detail="Такой поисковый запрос уже существует")
    db.add(SearchQuery(name=name.strip(), query_text=query_text.strip(), language=language, priority=max(1, min(priority, 100))))
    db.commit()
    return RedirectResponse("/queries", status_code=303)


@app.post("/queries/{query_id}/toggle")
def toggle_query(query_id: uuid.UUID, db: Session = Depends(get_db)):
    item = db.get(SearchQuery, query_id)
    if not item:
        raise HTTPException(status_code=404, detail="Запрос не найден")
    item.enabled = not item.enabled
    db.commit()
    return RedirectResponse("/queries", status_code=303)


@app.post("/queries/{query_id}/run")
def run_query(query_id: uuid.UUID, db: Session = Depends(get_db)):
    query = db.get(SearchQuery, query_id)
    if not query:
        raise HTTPException(status_code=404, detail="Запрос не найден")
    try:
        result = run_search_for_query(db, query)
    except YouTubeAPIError as exc:
        return RedirectResponse(f"/queries?error={quote(str(exc))}", status_code=303)
    message = (
        f"«{query.name}»: найдено {result['found']}, "
        f"новых {result['created']}, обновлено {result['updated']}"
    )
    return RedirectResponse(f"/queries?ok={quote(message)}", status_code=303)


@app.post("/scores/recompute")
def recompute_scores(db: Session = Depends(get_db)):
    count = recompute_all_scores(db)
    message = f"Рейтинг пересчитан для {count} видео"
    return RedirectResponse(f"/videos?ok={quote(message)}", status_code=303)


@app.get("/videos", response_class=HTMLResponse)
def videos_page(request: Request, db: Session = Depends(get_db)):
    videos = db.scalars(
        select(Video)
        .options(joinedload(Video.channel))
        .order_by(Video.viral_score.desc(), Video.published_at.desc().nullslast())
    ).all()
    items = [
        {"video": video, "why": json.loads(video.score_explanation or "[]")}
        for video in videos
    ]
    return templates.TemplateResponse(
        request,
        "videos.html",
        {"items": items, "ok_message": request.query_params.get("ok")},
    )
