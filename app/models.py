import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class SearchQuery(Base):
    __tablename__ = "search_queries"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120))
    query_text: Mapped[str] = mapped_column(String(255), unique=True)
    language: Mapped[str] = mapped_column(String(10), default="ru")
    priority: Mapped[int] = mapped_column(Integer, default=50)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Channel(Base):
    __tablename__ = "channels"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    external_channel_id: Mapped[str] = mapped_column(String(100), unique=True)
    title: Mapped[str] = mapped_column(String(255))
    url: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="discovered")
    subscriber_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    videos: Mapped[list["Video"]] = relationship(back_populates="channel")


class Video(Base):
    __tablename__ = "videos"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    external_video_id: Mapped[str] = mapped_column(String(100), unique=True)
    channel_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("channels.id"))
    discovered_by_query_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("search_queries.id"), nullable=True
    )
    title: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    url: Mapped[str] = mapped_column(Text)
    thumbnail_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_type: Mapped[str] = mapped_column(String(20), default="topic_search")
    view_count: Mapped[int] = mapped_column(Integer, default=0)
    like_count: Mapped[int] = mapped_column(Integer, default=0)
    comment_count: Mapped[int] = mapped_column(Integer, default=0)
    viral_score: Mapped[int] = mapped_column(Integer, default=0)
    editorial_score: Mapped[int] = mapped_column(Integer, default=0)
    score_explanation: Mapped[str | None] = mapped_column(Text, nullable=True)
    scored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    workflow_status: Mapped[str] = mapped_column(String(30), default="discovered")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    channel: Mapped[Channel] = relationship(back_populates="videos")
    discovered_by_query: Mapped["SearchQuery | None"] = relationship()
    metric_snapshots: Mapped[list["VideoMetricSnapshot"]] = relationship(
        back_populates="video", cascade="all, delete-orphan"
    )
    transcript: Mapped["Transcript | None"] = relationship(
        back_populates="video", cascade="all, delete-orphan", uselist=False
    )
    content_packs: Mapped[list["ContentPack"]] = relationship(
        back_populates="video", cascade="all, delete-orphan"
    )


class VideoMetricSnapshot(Base):
    __tablename__ = "video_metric_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    video_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("videos.id"))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    view_count: Mapped[int] = mapped_column(Integer, default=0)
    like_count: Mapped[int] = mapped_column(Integer, default=0)
    comment_count: Mapped[int] = mapped_column(Integer, default=0)
    video: Mapped[Video] = relationship(back_populates="metric_snapshots")


class BrandSettings(Base):
    """Single editable row with the editor's style/brand instructions that get
    injected into every AI generation prompt."""

    __tablename__ = "brand_settings"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    editorial_style: Mapped[str] = mapped_column(Text, default="")
    target_audience: Mapped[str] = mapped_column(Text, default="")
    blog_cta: Mapped[str] = mapped_column(Text, default="")
    image_style: Mapped[str] = mapped_column(Text, default="")
    custom_instructions: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Transcript(Base):
    __tablename__ = "transcripts"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    video_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("videos.id"), unique=True)
    provider: Mapped[str] = mapped_column(String(30), default="youtube_captions")
    language: Mapped[str | None] = mapped_column(String(10), nullable=True)
    raw_text: Mapped[str] = mapped_column(Text, default="")
    segments: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="ready")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    video: Mapped[Video] = relationship(back_populates="transcript")


class ContentPack(Base):
    __tablename__ = "content_packs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    video_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("videos.id"))
    transcript_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("transcripts.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(20), default="draft")
    model: Mapped[str | None] = mapped_column(String(60), nullable=True)
    content_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    video: Mapped[Video] = relationship(back_populates="content_packs")
