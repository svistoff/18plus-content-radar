"""Editable brand/editorial settings injected into AI generation prompts."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import BrandSettings

DEFAULTS = {
    "editorial_style": (
        "Пиши живо и по-человечески, с лёгким уместным юмором, без канцелярита и "
        "воды. Короткие абзацы, разговорный тон, конкретные примеры. Цепляющее "
        "вступление, которое хочется дочитать."
    ),
    "target_audience": (
        "Взрослые 25–45 лет, интересующиеся отношениями, психологией и интимной "
        "жизнью. Читают с телефона, ценят честность и практическую пользу."
    ),
    "blog_cta": "Читайте полный разбор в блоге и подписывайтесь, чтобы не пропустить новое.",
    "image_style": (
        "Тёплые кинематографичные фотографии, естественный свет, пастельная гамма, "
        "живые эмоции, без текста и водяных знаков. Атмосферно и стильно."
    ),
    "custom_instructions": "",
}


def get_brand_settings(db: Session) -> BrandSettings:
    settings = db.scalar(select(BrandSettings).limit(1))
    if settings is None:
        settings = BrandSettings(**DEFAULTS)
        db.add(settings)
        db.commit()
    return settings
