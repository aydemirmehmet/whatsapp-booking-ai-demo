"""Booking domain logic shared by the menu bot and the AI agent."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError

from .db import Appointment, Doctor, Service, Slot


class SlotUnavailable(Exception):
    pass


def fmt(dt: datetime) -> str:
    return dt.strftime("%a %d %b, %H:%M")


def doctors(s) -> list[Doctor]:
    return list(s.scalars(select(Doctor).order_by(Doctor.id)))


def services(s) -> list[Service]:
    return list(s.scalars(select(Service).order_by(Service.id)))


def free_slots(s, doctor_id: int | None = None, day: str | None = None, phone: str | None = None,
               now: datetime | None = None, limit: int = 6) -> list[Slot]:
    """Future slots with no appointment and no active hold by someone else."""
    now = now or datetime.now()
    q = (select(Slot).outerjoin(Appointment, Appointment.slot_id == Slot.id)
         .where(Appointment.id.is_(None), Slot.start > now)
         .where(or_(Slot.held_until.is_(None), Slot.held_until < now, Slot.held_by == phone))
         .order_by(Slot.start, Slot.doctor_id))
    if doctor_id:
        q = q.where(Slot.doctor_id == doctor_id)
    if day:
        d = datetime.fromisoformat(day)
        q = q.where(and_(Slot.start >= d, Slot.start < d + timedelta(days=1)))
    return list(s.scalars(q.limit(limit)))


def hold(s, slot_id: int, phone: str, minutes: int, now: datetime | None = None) -> Slot:
    now = now or datetime.now()
    slot = s.get(Slot, slot_id)
    if slot is None or slot.start <= now:
        raise SlotUnavailable
    if s.scalar(select(Appointment.id).where(Appointment.slot_id == slot_id)) is not None:
        raise SlotUnavailable
    if slot.held_by not in (None, phone) and slot.held_until and slot.held_until > now:
        raise SlotUnavailable
    slot.held_by, slot.held_until = phone, now + timedelta(minutes=minutes)
    s.commit()
    return slot


def book(s, slot_id: int, phone: str, name: str = "", now: datetime | None = None) -> Appointment:
    now = now or datetime.now()
    slot = s.get(Slot, slot_id)
    if slot is None or slot.start <= now:
        raise SlotUnavailable
    if slot.held_by not in (None, phone) and slot.held_until and slot.held_until > now:
        raise SlotUnavailable
    appt = Appointment(slot_id=slot_id, phone=phone, patient_name=name)
    s.add(appt)
    try:
        s.flush()  # UNIQUE(slot_id) fires here if someone else just booked it
    except IntegrityError:
        s.rollback()
        raise SlotUnavailable
    slot.held_by = slot.held_until = None
    s.commit()
    return appt


def upcoming(s, phone: str, now: datetime | None = None) -> list[Appointment]:
    now = now or datetime.now()
    return list(s.scalars(select(Appointment).join(Slot)
                          .where(Appointment.phone == phone, Slot.start > now)
                          .order_by(Slot.start)))


def cancel(s, appointment_id: int, phone: str) -> bool:
    appt = s.get(Appointment, appointment_id)
    if appt is None or appt.phone != phone:
        return False
    s.delete(appt)
    s.commit()
    return True
