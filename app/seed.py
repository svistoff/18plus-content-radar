from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import SearchQuery

DEFAULT_QUERIES = [
    ("Психология отношений", "отношения психология", "ru", 90),
    ("Знакомства и dating", "знакомства отношения", "ru", 85),
    ("Интимность в паре", "интимность в отношениях", "ru", 80),
    ("Dating psychology", "dating psychology", "en", 75),
    ("Relationship advice", "relationship advice", "en", 70),
]


def seed_search_queries(db: Session) -> None:
    if db.scalar(select(SearchQuery.id).limit(1)):
        return
    db.add_all([SearchQuery(name=n, query_text=q, language=lang, priority=p) for n, q, lang, p in DEFAULT_QUERIES])
    db.commit()
