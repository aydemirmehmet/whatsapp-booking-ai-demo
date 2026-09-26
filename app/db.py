"""Database models. SQLite by default, PostgreSQL via DATABASE_URL.

Double bookings are prevented at the database level: an appointment row
references a slot with a UNIQUE constraint, so two concurrent confirmations
for the same slot cannot both succeed.
"""
from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import (
    Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, create_engine,
)
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker


class Base(DeclarativeBase):
    pass


class Doctor(Base):
    __tablename__ = "doctors"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    specialty: Mapped[str] = mapped_column(String(120))


class Service(Base):
    __tablename__ = "services"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    price_eur: Mapped[int] = mapped_column(Integer)
    duration_min: Mapped[int] = mapped_column(Integer)
    description: Mapped[str] = mapped_column(Text, default="")


class Slot(Base):
    __tablename__ = "slots"
    id: Mapped[int] = mapped_column(primary_key=True)
    doctor_id: Mapped[int] = mapped_column(ForeignKey("doctors.id"))
    start: Mapped[datetime] = mapped_column(DateTime, index=True)
    # Temporary hold while the patient confirms
    held_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    held_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    doctor: Mapped[Doctor] = relationship()


class Appointment(Base):
    __tablename__ = "appointments"
    __table_args__ = (UniqueConstraint("slot_id", name="uq_one_booking_per_slot"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    slot_id: Mapped[int] = mapped_column(ForeignKey("slots.id"))
    phone: Mapped[str] = mapped_column(String(32), index=True)
    patient_name: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    reminded_24h: Mapped[bool] = mapped_column(Boolean, default=False)
    reminded_2h: Mapped[bool] = mapped_column(Boolean, default=False)
    slot: Mapped[Slot] = relationship()


class ProcessedMessage(Base):
    """Webhook idempotency: Meta retries deliveries, each wamid is handled once."""
    __tablename__ = "processed_messages"
    wamid: Mapped[str] = mapped_column(String(200), primary_key=True)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class Conversation(Base):
    __tablename__ = "conversations"
    phone: Mapped[str] = mapped_column(String(32), primary_key=True)
    state: Mapped[str] = mapped_column(String(40), default="idle")
    data_json: Mapped[str] = mapped_column(Text, default="{}")
    human_mode: Mapped[bool] = mapped_column(Boolean, default=False)
    last_inbound_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    @property
    def data(self) -> dict:
        return json.loads(self.data_json or "{}")

    @data.setter
    def data(self, value: dict) -> None:
        self.data_json = json.dumps(value)


class MessageLog(Base):
    __tablename__ = "message_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    phone: Mapped[str] = mapped_column(String(32), index=True)
    direction: Mapped[str] = mapped_column(String(3))  # in | out
    body: Mapped[str] = mapped_column(Text)
    at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class Lead(Base):
    __tablename__ = "leads"
    phone: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), default="")
    interest: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[str] = mapped_column(String(40), default="new")  # new|qualified|booked|handoff|lost
    source: Mapped[str] = mapped_column(String(60), default="whatsapp")
    summary: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)


def make_session_factory(url: str):
    kwargs = {}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        if url in ("sqlite://", "sqlite:///:memory:"):
            kwargs["poolclass"] = StaticPool  # one shared in-memory DB
    engine = create_engine(url, **kwargs)
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)
