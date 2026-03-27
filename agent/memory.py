# agent/memory.py — Conversation memory with SQLite + full multi-tenant support
from __future__ import annotations
import os
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import String, Text, DateTime, Integer, select, delete
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("agentkit")

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./agentkit.db")
if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

engine = create_async_engine(DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

# Per-tenant engine cache (rule 18: each tenant has its own SQLite)
_tenant_engines: dict = {}
_tenant_sessions: dict = {}


def _get_tenant_session(db_url: str):
    """Return (or create) an async session factory for a tenant's database."""
    if db_url not in _tenant_sessions:
        eng = create_async_engine(db_url, echo=False)
        _tenant_engines[db_url] = eng
        _tenant_sessions[db_url] = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    return _tenant_sessions[db_url]


class Base(DeclarativeBase):
    pass


class Mensaje(Base):
    """Conversation message — always scoped to tenant_id (rule 18)."""
    __tablename__ = "mensajes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(100), index=True)
    telefono: Mapped[str] = mapped_column(String(50), index=True)
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class ProcessedMessage(Base):
    """Rule 30: Deduplication table. Stores processed message IDs with a 24h TTL."""
    __tablename__ = "processed_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(100), index=True)
    message_id: Mapped[str] = mapped_column(String(200), index=True, unique=True)
    processed_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class ConversationState(Base):
    """Rule 25: State machine for each conversation. ACTIVE | PAUSED | CLOSED."""
    __tablename__ = "conversation_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(100), index=True)
    phone: Mapped[str] = mapped_column(String(50), index=True)
    state: Mapped[str] = mapped_column(String(20), default="ACTIVE")
    # Nullable fields: avoid Optional[] annotation — not compatible with Python 3.14 + SQLAlchemy 2.0.x
    paused_at = mapped_column(DateTime, nullable=True)
    paused_reason = mapped_column(String(200), nullable=True)
    last_escalation = mapped_column(DateTime, nullable=True)
    human_agent = mapped_column(String(100), nullable=True)


class UsageLog(Base):
    """Rule 22: Per-tenant Claude API usage tracking for billing."""
    __tablename__ = "usage_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(100), index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    model: Mapped[str] = mapped_column(String(100), default="claude-sonnet-4-6")
    conversation_id: Mapped[str] = mapped_column(String(200), default="")


class EscalationLog(Base):
    """Rule 24d: Log each escalation event."""
    __tablename__ = "escalation_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(100), index=True)
    phone: Mapped[str] = mapped_column(String(50), index=True)
    reason: Mapped[str] = mapped_column(String(500), default="")
    escalated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    resolved_at = mapped_column(DateTime, nullable=True)


async def inicializar_db():
    """Create all tables if they don't exist."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database initialized")


async def guardar_mensaje(telefono: str, role: str, content: str, tenant_id: str = "default"):
    """Save a message to conversation history, scoped to tenant (rule 18)."""
    async with async_session() as session:
        msg = Mensaje(tenant_id=tenant_id, telefono=telefono, role=role, content=content)
        session.add(msg)
        await session.commit()


async def obtener_historial(telefono: str, tenant_id: str = "default", limite: int = 20) -> list[dict]:
    """Retrieve last N messages for a conversation, strictly filtered by tenant_id (rule 18)."""
    async with async_session() as session:
        query = (
            select(Mensaje)
            .where(Mensaje.telefono == telefono)
            .where(Mensaje.tenant_id == tenant_id)   # rule 18: ALWAYS filter by tenant
            .order_by(Mensaje.timestamp.desc())
            .limit(limite)
        )
        result = await session.execute(query)
        mensajes = result.scalars().all()
        mensajes.reverse()
        return [{"role": m.role, "content": m.content} for m in mensajes]


async def limpiar_historial(telefono: str, tenant_id: str = "default"):
    """Delete all messages for a conversation."""
    async with async_session() as session:
        await session.execute(
            delete(Mensaje)
            .where(Mensaje.telefono == telefono)
            .where(Mensaje.tenant_id == tenant_id)
        )
        await session.commit()


async def is_duplicate_message(message_id: str, tenant_id: str = "default") -> bool:
    """Rule 30: Check if a message_id was already processed within the last 24 hours."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    async with async_session() as session:
        # Purge expired entries
        await session.execute(
            delete(ProcessedMessage).where(ProcessedMessage.processed_at < cutoff)
        )
        # Check for existing entry
        result = await session.execute(
            select(ProcessedMessage)
            .where(ProcessedMessage.message_id == message_id)
            .where(ProcessedMessage.tenant_id == tenant_id)
        )
        exists = result.scalars().first() is not None
        if not exists:
            session.add(ProcessedMessage(tenant_id=tenant_id, message_id=message_id))
        await session.commit()
        return exists


async def get_conversation_state(phone: str, tenant_id: str = "default") -> str:
    """Rule 25: Get current state for a conversation. Returns ACTIVE if not found."""
    async with async_session() as session:
        result = await session.execute(
            select(ConversationState)
            .where(ConversationState.phone == phone)
            .where(ConversationState.tenant_id == tenant_id)
        )
        state_row = result.scalars().first()
        return state_row.state if state_row else "ACTIVE"


async def set_conversation_state(
    phone: str, tenant_id: str, state: str,
    reason: str = "", human_agent: str = ""
):
    """Rule 25: Update conversation state."""
    async with async_session() as session:
        result = await session.execute(
            select(ConversationState)
            .where(ConversationState.phone == phone)
            .where(ConversationState.tenant_id == tenant_id)
        )
        row = result.scalars().first()
        now = datetime.now(timezone.utc)

        if not row:
            row = ConversationState(phone=phone, tenant_id=tenant_id)
            session.add(row)

        row.state = state
        if state == "PAUSED":
            row.paused_at = now
            row.paused_reason = reason
            row.last_escalation = now
        if human_agent:
            row.human_agent = human_agent
        await session.commit()


async def log_usage(tenant_id: str, input_tokens: int, output_tokens: int,
                    model: str = "claude-sonnet-4-6", conversation_id: str = ""):
    """Rule 22: Record Claude API token usage for billing."""
    async with async_session() as session:
        session.add(UsageLog(
            tenant_id=tenant_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model=model,
            conversation_id=conversation_id,
        ))
        await session.commit()


async def log_escalation(phone: str, tenant_id: str, reason: str):
    """Rule 29: Log an escalation event."""
    async with async_session() as session:
        session.add(EscalationLog(tenant_id=tenant_id, phone=phone, reason=reason))
        await session.commit()
