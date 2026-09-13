import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from app.database import Base, SessionLocal, engine, get_db
from app.models import Channel, SearchQuery, Video
from app.seed import seed_search_queries
from app.services.discovery_service import run_search_for_query
from app.services.youtube_client import YouTubeAPIError

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        seed_search_queries(db)
    yield


app = FastAPI(title="18plus Content Radar", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    stats = {
        "queries": db.scalar(select(func.count()).select_from(SearchQuery)) or 0,
        "videos": db.scalar(select(func.count()).select_from(Video)) or 0,
        "watchlist": db.scalar(select(func.count()).select_from(Channel).where(Channel.status == "watchlist")) or 0,
        "selected": db.scalar(select(func.count()).select_from(Video).where(Video.workflow_status == "selected")) or 0,
    }
    return templates.TemplateResponse(request, "dashboard.html", {"stats": stats})


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


@app.get("/videos", response_class=HTMLResponse)
def videos_page(request: Request, db: Session = Depends(get_db)):
    videos = db.scalars(
        select(Video)
        .options(joinedload(Video.channel))
        .order_by(Video.published_at.desc().nullslast())
    ).all()
    return templates.TemplateResponse(request, "videos.html", {"videos": videos})
