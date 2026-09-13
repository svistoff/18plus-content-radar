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
from app.services.settings_service import get_brand_settings

MAX_TRANSCRIPT_CHARS = 14000
MAX_OUTPUT_TOKENS = 6000

SYSTEM_PROMPT = (
    "Ты — сильный контент-редактор блога о взрослых отношениях, знакомствах, "
    "интимной жизни, сексологии и психологии отношений. Пишешь по-русски.\n"
    "ЖЁСТКИЕ ПРАВИЛА:\n"
    "1. Транскрипт используй ТОЛЬКО как источник фактов и обсуждаемых идей. "
    "Не копируй фразы, структуру или последовательность исходного видео.\n"
    "2. Создай самостоятельный, оригинальный, ВОВЛЕКАЮЩИЙ материал, а не пересказ. "
    "Каждый формат должен быть готов к публикации, а не заготовкой в пару строк.\n"
    "3. Отделяй мнение автора видео от проверяемых фактов.\n"
    "4. Не давай медицинских, психотерапевтических или юридических гарантий.\n"
    "5. Для чувствительных тем добавляй мягкую рекомендацию обратиться к "
    "профильному специалисту.\n"
    "6. Тематика — про отношения и психологию, без порнографии и откровенного "
    "сексуального контента.\n"
    "Верни СТРОГО один JSON-объект по заданной схеме, без markdown-обёртки."
)

SCHEMA_HINT = """Схема JSON (заполни ВСЕ поля развёрнутым содержанием на русском):
{
  "meta": {"language": "ru", "editorial_angle": "краткий оригинальный угол подачи"},
  "blog_article": {
    "seo_title": "до 60 знаков", "seo_description": "до 160 знаков", "h1": "...",
    "slug_suggestion": "translit-slug",
    "intro": "2-3 абзаца, крепкий цепляющий заход",
    "sections": [{"h2": "...", "body_markdown": "3-5 полных абзацев с примерами"}],
    "faq": [{"question": "...", "answer": "развёрнутый ответ 2-4 предложения"}],
    "conclusion": "...",
    "editorial_disclaimer": "..."
  },
  "headlines": ["8-12 вариантов заголовков разных типов: провокационные, экспертные, SEO"],
  "instagram_carousel": {
    "cover_headline": "...",
    "slides": [{"slide_number": 1, "headline": "...", "body": "2-4 живых предложения"}],
    "caption": "полноценная подпись с эмоцией и вопросом к аудитории", "cta": "..."
  },
  "reels": [{"hook": "...", "duration_seconds": 30,
             "script": "ПОЛНЫЙ сценарий по секундам: что говорить и показывать",
             "shot_list": ["кадр 1", "кадр 2"], "caption": "...", "cta": "..."}],
  "vk_posts": [{"headline": "...", "body": "самодостаточный пост 4-8 абзацев (лонгрид)", "cta": "..."}],
  "zen": {"headline": "...", "lead": "...", "body_markdown": "полноценная адаптированная статья 3000+ знаков"},
  "telegram_teasers": [{"text": "цепляющий тизер 2-4 предложения", "cta": "..."}],
  "illustration_prompts": [
    {"placement": "article_hero / section_1 / carousel / reels / teaser",
     "prompt": "детальный промпт на английском для генератора изображений",
     "negative_prompt": "explicit nudity, pornography, minors, text, watermark"}
  ],
  "editor_notes": ["спорные утверждения, которые редактору стоит проверить"]
}
ОБЪЁМЫ (соблюдай строго): статья 5-8 разделов по 3-5 абзацев; карусель 8-10 слайдов;
3-5 reels с полными сценариями; 2-3 vk_posts-лонгрида; 3-5 telegram_teasers;
5-8 illustration_prompts под разные места. Не оставляй поля пустыми и короткими."""


class AIContentError(RuntimeError):
    """The content pack could not be generated."""


def generate_content_pack(db: Session, video: Video, transcript: Transcript) -> ContentPack:
    settings = get_settings()
    if not settings.ai_api_key:
        raise AIContentError(
            "AI_API_KEY не задан. Добавьте ключ OpenAI в .env, чтобы генерировать "
            "контент."
        )

    brand = get_brand_settings(db)
    user_prompt = _build_user_prompt(video, transcript, brand)
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


def _build_user_prompt(video: Video, transcript: Transcript, brand) -> str:
    text = transcript.raw_text[:MAX_TRANSCRIPT_CHARS]
    channel = video.channel.title if video.channel else "неизвестен"
    description = (video.description or "").strip()[:1500]

    brand_block = _brand_block(brand)

    parts = [
        f"Исходное видео: «{video.title}»",
        f"Канал: {channel}",
        f"Ссылка: {video.url}",
    ]
    if description:
        parts.append(f"Описание видео (доп. контекст): {description}")
    if brand_block:
        parts.append("\nРЕДАКЦИОННЫЕ НАСТРОЙКИ БРЕНДА (обязательно учитывай):\n" + brand_block)
    parts.append("\n" + SCHEMA_HINT)
    parts.append(
        "\nТранскрипт видео (только как источник фактов, не копировать):\n"
        f'"""\n{text}\n"""'
    )
    return "\n".join(parts)


def _brand_block(brand) -> str:
    lines = []
    if getattr(brand, "editorial_style", ""):
        lines.append(f"- Тон и стиль: {brand.editorial_style}")
    if getattr(brand, "target_audience", ""):
        lines.append(f"- Целевая аудитория: {brand.target_audience}")
    if getattr(brand, "blog_cta", ""):
        lines.append(f"- Призыв/CTA (используй в подводках и концовках): {brand.blog_cta}")
    if getattr(brand, "image_style", ""):
        lines.append(f"- Стиль иллюстраций (закладывай в illustration_prompts): {brand.image_style}")
    if getattr(brand, "custom_instructions", ""):
        lines.append(f"- Дополнительные требования: {brand.custom_instructions}")
    return "\n".join(lines)


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
        temperature=0.8,
        max_tokens=MAX_OUTPUT_TOKENS,
    )
    return response.choices[0].message.content or ""


def _parse_json(raw: str) -> dict:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AIContentError(
            "Модель вернула невалидный JSON. Попробуйте перегенерировать."
        ) from exc
