"""Database models for VK Alarm Bot."""

from sqlalchemy import (
    Column,
    Integer,
    String,
    Boolean,
    Float,
    DateTime,
    Text,
    ForeignKey,
    JSON,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship
from datetime import datetime
from app.models.database import Base


class Setting(Base):
    __tablename__ = "settings"

    id = Column(Integer, primary_key=True, index=True)
    key = Column(String(100), unique=True, nullable=False)
    value = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Monitor(Base):
    """A monitoring subscription: parse VK sources, analyze with AI, send to Max."""
    __tablename__ = "monitors"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(200), nullable=False)
    is_active = Column(Boolean, default=True)
    # Per-stream check frequency in minutes (falls back to the global default).
    check_interval_minutes = Column(Integer, default=15)

    # Source VK channels (comma-separated list of VK domains/IDs)
    source_channels = Column(Text, nullable=False)

    # Filters (applied BEFORE AI analysis)
    keywords = Column(Text, nullable=True)
    minus_words = Column(Text, nullable=True)
    min_er = Column(Float, default=0.0)
    max_er = Column(Float, default=100.0)

    # AI analysis prompt for filtering
    use_ai = Column(Boolean, default=False)
    ai_prompt = Column(Text, nullable=True)  # Prompt that describes what to filter FOR
    ai_tone = Column(String(100), default="нейтральный")
    ai_max_length = Column(Integer, default=5000)
    ai_fallback_to_original = Column(Boolean, default=True)

    # Media: forward VK post attachments (photos/docs) to Max
    copy_media = Column(Boolean, default=True)
    # Debug option: record every parsed post in ``events`` (so the UI can show
    # which ones were dropped by keywords/ER and which reached Max).
    log_all_posts = Column(Boolean, default=False)

    # Target Max messenger channels (newline-separated list of Max chat IDs/links)
    max_channels = Column(Text, nullable=True)

    # Stats
    posts_processed = Column(Integer, default=0)
    posts_published = Column(Integer, default=0)
    posts_filtered_keywords = Column(Integer, default=0)
    posts_filtered_er = Column(Integer, default=0)
    ai_successes = Column(Integer, default=0)
    ai_fallbacks = Column(Integer, default=0)
    ai_filtered_out = Column(Integer, default=0)  # Posts filtered out by AI
    publication_errors = Column(Integer, default=0)
    last_post_ids = Column(JSON, nullable=True)
    last_run_at = Column(DateTime, nullable=True)
    # Set while a run is in progress, so the UI can show "run in progress"
    # instead of a stale "due now" countdown (long streams take minutes).
    run_started_at = Column(DateTime, nullable=True)
    paused_at = Column(DateTime, nullable=True)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    events = relationship("Event", back_populates="monitor", cascade="all, delete-orphan")
    logs = relationship("Log", back_populates="monitor", cascade="all, delete-orphan")
    owner = relationship("User", backref="monitors")


class Event(Base):
    """A single detected event (matched VK post) that was sent to Max."""
    __tablename__ = "events"

    id = Column(Integer, primary_key=True, index=True)
    monitor_id = Column(Integer, ForeignKey("monitors.id"), nullable=False)

    # Original post data
    original_id = Column(String(100), nullable=False)
    original_owner_id = Column(String(100), nullable=False)
    original_url = Column(String(500), nullable=False)
    original_text = Column(Text, nullable=True)
    original_date = Column(DateTime, nullable=True)
    attachments = Column(Text, nullable=True)

    # Stats
    likes = Column(Integer, default=0)
    reposts = Column(Integer, default=0)
    comments = Column(Integer, default=0)
    views = Column(Integer, default=0)
    er = Column(Float, default=0.0)

    # AI analysis result
    ai_analysis_result = Column(Text, nullable=True)  # What AI said about this post
    ai_filtered = Column(Boolean, default=False)  # True if it passed AI filter

    # Status: filtered (matched), sent, failed, pending (retryable Max failure)
    status = Column(String(50), default="filtered")
    sent_at = Column(DateTime, nullable=True)
    error_message = Column(Text, nullable=True)
    # Failed publications are retried from the ``pending`` state; the counter
    # bounds the retries so a broken Max setup cannot loop forever.
    retry_attempts = Column(Integer, default=0)

    created_at = Column(DateTime, default=datetime.utcnow)

    monitor = relationship("Monitor", back_populates="events")


class Log(Base):
    __tablename__ = "logs"

    id = Column(Integer, primary_key=True, index=True)
    monitor_id = Column(Integer, ForeignKey("monitors.id"), nullable=False)
    level = Column(String(20), default="info")
    message = Column(Text, nullable=False)
    details = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    monitor = relationship("Monitor", back_populates="logs")


class Notification(Base):
    __tablename__ = "notifications"

    id = Column(Integer, primary_key=True, index=True)
    level = Column(String(20), default="error")
    title = Column(String(250), nullable=False)
    message = Column(Text, nullable=False)
    monitor_id = Column(Integer, ForeignKey("monitors.id"), nullable=True)
    is_read = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class User(Base):
    __tablename__ = "users"
    
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(100), unique=True, index=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    role = Column(String(50), default="user", nullable=False)  # admin, user
    is_active = Column(Boolean, default=True, nullable=False)
    theme_preference = Column(String(20), default="system", nullable=False)
    # Bumped on every password change: JWTs carry the value they were issued
    # with, so an old token (stolen or from a shared PC) stops working at once
    # instead of staying valid for the whole 24 h lifetime.
    token_version = Column(Integer, default=1, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)