"""Inbound pipeline (idempotency, logging, routing) and reminder job."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from . import ai_agent, booking_flow
from . import scheduling as sch
from .config import settings
from .db import Appointment, Conversation, MessageLog, ProcessedMessage, Slot
from .whatsapp import Inbound, Reply

log = logging.getLogger("service")


def _log(s, phone: str, direction: str, body: str) -> None:
    s.add(MessageLog(phone=phone, direction=direction, body=body))


def process_inbound(s, msg: Inbound, sender, mode: str | None = None,
                    now: datetime | None = None, ai_client=None) -> list[Reply]:
    now = now or datetime.now()
    mode = mode or settings.bot_mode

    # 1) idempotency: Meta may deliver the same message more than once
    s.add(ProcessedMessage(wamid=msg.wamid))
    try:
        s.commit()
    except IntegrityError:
        s.rollback()
        log.info("duplicate %s ignored", msg.wamid)
        return []

    conv = s.get(Conversation, msg.phone) or Conversation(phone=msg.phone)
    s.add(conv)
    conv.last_inbound_at = now
    _log(s, msg.phone, "in", msg.text)
    s.commit()

    # 2) staff has taken over: bot stays silent unless the patient restarts it
    if conv.human_mode and msg.text.lower() not in {"menu", "bot"}:
        return []
    if conv.human_mode:
        conv.human_mode, conv.state = False, "idle"

    # 3) route to the selected demo
    try:
        if mode == "ai":
            replies = ai_agent.handle(s, conv, msg.text, msg.name, now, client=ai_client)
        else:
            replies = booking_flow.handle(s, conv, msg.text, msg.name, now)
    except Exception:  # never leave the patient without an answer
        log.exception("handler failed")
        s.rollback()
        replies = [Reply("Sorry, something went wrong on our side. A colleague will reply shortly.")]

    for r in replies:
        sender.send(msg.phone, r)
        _log(s, msg.phone, "out", r.text)
    s.commit()
    return replies


def run_reminders(s, sender, now: datetime | None = None) -> int:
    """Send 24h and 2h reminders once each. Uses an approved template because
    reminders are business-initiated (outside the 24h service window)."""
    now = now or datetime.now()
    due = s.scalars(select(Appointment).join(Slot)
                    .where(Slot.start > now, Slot.start <= now + timedelta(hours=24)))
    sent = 0
    for a in due:
        left = a.slot.start - now
        if left <= timedelta(hours=2) and not a.reminded_2h:
            label, a.reminded_2h, a.reminded_24h = "in 2 hours", True, True
        elif not a.reminded_24h:
            label, a.reminded_24h = "tomorrow" if left > timedelta(hours=2) else "soon", True
        else:
            continue
        when = sch.fmt(a.slot.start)
        reply = Reply(
            f"Reminder: your appointment with {a.slot.doctor.name} is {label} ({when}). "
            "Reply *my* to reschedule or cancel.",
            template=settings.reminder_template,
            template_params=[a.patient_name or "there", when, a.slot.doctor.name])
        sender.send(a.phone, reply)
        _log(s, a.phone, "out", reply.text)
        sent += 1
    s.commit()
    return sent
