"""Generates an editable Content Pack from a video transcript via an LLM.

The transcript is treated strictly as a research source: the prompt forbids
copying phrasing or structure, requires separating the source author's opinion
from verifiable facts, and adds a gentle "consult a specialist" note for
sensitive topics. Output is a single JSON object the dashboard renders and the
editor edits before publishing manually.
"""

from __future__ import annotations

import json

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import ContentPack, Transcript, Video

MAX_TRANSCRIPT_CHARS = 12000

SYSTEM_PROMPT = (
    "Ты — редактор блога о взрослых отношениях, знакомствах, интимной жизни, "
    "сексологии и психологии отношений. Пишешь по-русски, живо и по делу.\n"
    "ЖЁСТКИЕ ПРАВИЛА:\n"
    "1. Транскрипт используй ТОЛЬКО как источник фактов и обсуждаемых идей. "
    "Не копируй фразы, структуру или последовательность исходного видео.\n"
    "2. Создай самостоятельный, оригинальный материал, а не пересказ.\n"
    "3. Отделяй мнение автора видео от проверяемых фактов.\n"
    "4. Не давай медицинских, психотерапевтических или юридических гарантий.\n"
    "5. Для чувствительных тем добавляй мягкую рекомендацию обратиться к "
    "профильному специалисту.\n"
    "6. Тематика — про отношения и психологию, без порнографии и откровенного "
    "сексуального контента.\n"
    "Верни СТРОГО один JSON-объект по заданной схеме, без markdown-обёртки."
)

SCHEMA_HINT = """Схема JSON (заполни все поля осмысленным содержанием на русском):
{
  "meta": {"language": "ru", "editorial_angle": "краткий оригинальный угол подачи"},
  "blog_article": {
    "seo_title": "...", "seo_description": "...", "h1": "...",
    "slug_suggestion": "translit-slug",
    "intro": "...",
    "sections": [{"h2": "...", "body_markdown": "..."}],
    "faq": [{"question": "...", "answer": "..."}],
    "conclusion": "...",
    "editorial_disclaimer": "..."
  },
  "headlines": ["5-12 вариантов заголовков"],
  "instagram_carousel": {
    "cover_headline": "...",
    "slides": [{"slide_number": 1, "headline": "...", "body": "..."}],
    "caption": "...", "cta": "..."
  },
  "reels": [{"hook": "...", "duration_seconds": 30, "script": "...", "cta": "..."}],
  "vk_posts": [{"headline": "...", "body": "...", "cta": "..."}],
  "zen": {"headline": "...", "body_markdown": "...", "lead": "..."},
  "telegram_teasers": [{"text": "...", "cta": "..."}],
  "illustration_prompts": [
    {"placement": "article_hero", "prompt": "...(на английском)",
     "negative_prompt": "explicit nudity, pornography, minors, text, watermark"}
  ],
  "editor_notes": ["спорные утверждения, которые редактору стоит проверить"]
}
Объёмы: статья 5-8 секций, карусель 7-10 слайдов, 3-5 reels, 2-3 vk_posts,
3-5 telegram_teasers, 3-6 illustration_prompts."""


class AIContentError(RuntimeError):
    """The content pack could not be generated."""


def generate_content_pack(db: Session, video: Video, transcript: Transcript) -> ContentPack:
    settings = get_settings()
    if not settings.ai_api_key:
        raise AIContentError(
            "AI_API_KEY не задан. Добавьте ключ OpenAI в .env, чтобы генерировать "
            "контент."
        )

    user_prompt = _build_user_prompt(video, transcript)
    video.workflow_status = "content_generating"
    db.commit()

    try:
        raw = _chat_completion(SYSTEM_PROMPT, user_prompt)
        data = _parse_json(raw)
    except AIContentError:
        video.workflow_status = "transcript_ready"
        db.commit()
        raise
    except Exception as exc:
        video.workflow_status = "transcript_ready"
        db.commit()
        raise AIContentError(f"Ошибка генерации: {exc}") from exc

    data["attribution"] = {
        "source_video_url": video.url,
        "source_channel_name": video.channel.title if video.channel else None,
        "source_video_title": video.title,
        "source_published_at": video.published_at.isoformat() if video.published_at else None,
        "transcript_provider": transcript.provider,
        "editorial_rule": (
            "Материал создан как оригинальная редакционная переработка; "
            "transcript не копируется дословно."
        ),
    }

    pack = ContentPack(
        video=video,
        transcript_id=transcript.id,
        status="draft",
        model=settings.ai_model,
        content_json=json.dumps(data, ensure_ascii=False),
    )
    db.add(pack)
    video.workflow_status = "draft_ready"
    db.commit()
    return pack


def _build_user_prompt(video: Video, transcript: Transcript) -> str:
    text = transcript.raw_text[:MAX_TRANSCRIPT_CHARS]
    channel = video.channel.title if video.channel else "неизвестен"
    return (
        f"Исходное видео: «{video.title}»\n"
        f"Канал: {channel}\n"
        f"Ссылка: {video.url}\n\n"
        f"{SCHEMA_HINT}\n\n"
        f"Транскрипт видео (только как источник фактов, не копировать):\n"
        f"\"\"\"\n{text}\n\"\"\""
    )


def _chat_completion(system_prompt: str, user_prompt: str) -> str:
    settings = get_settings()
    from openai import OpenAI

    client_kwargs = {"api_key": settings.ai_api_key}
    if settings.ai_base_url:
        client_kwargs["base_url"] = settings.ai_base_url
    client = OpenAI(**client_kwargs)

    response = client.chat.completions.create(
        model=settings.ai_model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        response_format={"type": "json_object"},
        temperature=0.7,
    )
    return response.choices[0].message.content or ""


def _parse_json(raw: str) -> dict:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AIContentError(
            "Модель вернула невалидный JSON. Попробуйте перегенерировать."
        ) from exc
