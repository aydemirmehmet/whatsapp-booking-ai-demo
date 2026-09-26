"""Demo 1: menu-driven clinic booking bot (no AI needed).

Patients can tap buttons/list rows or just type the option number.
State per phone number is stored in the `conversations` table.
"""
from __future__ import annotations

from datetime import datetime

from . import scheduling as sch
from .config import settings
from .db import Conversation
from .whatsapp import Option, Reply

GREETINGS = {"hi", "hello", "hey", "menu", "start", "merhaba", "selam"}
HUMAN_WORDS = {"staff", "human", "agent", "operator", "person"}


def _remember(conv: Conversation, reply: Reply) -> Reply:
    data = conv.data
    data["opts"] = [o.id for o in reply.options]
    conv.data = data
    return reply


def main_menu(conv: Conversation, name: str = "") -> Reply:
    hello = f"Hi {name.split()[0]}! " if name else "Hi! "
    return _remember(conv, Reply(
        f"{hello}Welcome to {settings.clinic_name}. How can I help?",
        [Option("book", "Book appointment"), Option("my", "My appointments"),
         Option("staff", "Talk to staff")]))


def handle(s, conv: Conversation, text: str, name: str = "", now: datetime | None = None) -> list[Reply]:
    now = now or datetime.now()
    raw = text.strip()
    low = raw.lower()

    # Typed "2" -> id of the 2nd option we last offered
    opts = conv.data.get("opts", [])
    if low.isdigit() and 1 <= int(low) <= len(opts):
        low = opts[int(low) - 1]
    # a few free-text shortcuts
    elif "my appointment" in low or low in {"appointments", "reschedule", "cancel"}:
        low = "my"
    elif low.startswith(("book", "i want to book", "appointment", "randevu")):
        low = "book"

    if low in GREETINGS:
        conv.state = "idle"
        return [main_menu(conv, name)]

    if low == "staff" or any(w in low.split() for w in HUMAN_WORDS):
        conv.human_mode = True
        conv.state = "human"
        return [Reply("OK, I've passed your chat to our team. A staff member will reply here "
                      "during opening hours (Mon-Sat 09:00-17:00).")]

    if low == "book":
        conv.data = {**conv.data, "reschedule": None}
        return [_ask_doctor(s, conv)]

    if low.startswith("d:"):
        return [_ask_slot(s, conv, int(low[2:]), now)]

    if low.startswith("s:"):
        slot_id = int(low[2:])
        try:
            slot = sch.hold(s, slot_id, conv.phone, settings.slot_hold_minutes, now)
        except sch.SlotUnavailable:
            return [Reply("Sorry, that time was just taken."), _ask_slot(s, conv, None, now)]
        conv.state = "confirm"
        return [_remember(conv, Reply(
            f"{slot.doctor.name}\n{sch.fmt(slot.start)}\n\nShall I book it? "
            f"(held for you for {settings.slot_hold_minutes} min)",
            [Option(f"yes:{slot_id}", "Yes, book it"), Option("book", "Pick another")]))]

    if low.startswith("yes:"):
        slot_id = int(low[4:])
        try:
            appt = sch.book(s, slot_id, conv.phone, name, now)
        except sch.SlotUnavailable:
            return [Reply("Sorry, that time is no longer available."), _ask_doctor(s, conv)]
        old = conv.data.get("reschedule")
        if old:
            sch.cancel(s, old, conv.phone)
        conv.state, conv.data = "idle", {}
        verb = "Rescheduled" if old else "Booked"
        return [_remember(conv, Reply(
            f"{verb} ✅\n{appt.slot.doctor.name}\n{sch.fmt(appt.slot.start)}\n\n"
            "I'll send you a reminder 24 hours and 2 hours before.",
            [Option("my", "My appointments"), Option("menu", "Main menu")]))]

    if low == "my":
        return [_my_appointments(s, conv, now)]

    if low.startswith("c:"):
        ok = sch.cancel(s, int(low[2:]), conv.phone)
        conv.state = "idle"
        return [_remember(conv, Reply("Your appointment is cancelled." if ok else
                                      "I couldn't find that appointment.",
                                      [Option("book", "Book new time"), Option("menu", "Main menu")]))]

    if low.startswith("r:"):
        appt_id = int(low[2:])
        appt = next((a for a in sch.upcoming(s, conv.phone, now) if a.id == appt_id), None)
        if not appt:
            return [_my_appointments(s, conv, now)]
        conv.data = {**conv.data, "reschedule": appt_id}
        return [_ask_slot(s, conv, appt.slot.doctor_id, now)]

    if conv.state == "idle":
        return [main_menu(conv, name)]
    return [Reply("Please choose one of the options above, or type *menu* to start again.")]


def _ask_doctor(s, conv: Conversation) -> Reply:
    conv.state = "choose_doctor"
    return _remember(conv, Reply("Which doctor would you like to see?", [
        Option(f"d:{d.id}", d.name, d.specialty) for d in sch.doctors(s)]))


def _ask_slot(s, conv: Conversation, doctor_id: int | None, now: datetime) -> Reply:
    if doctor_id is None:
        doctor_id = conv.data.get("doctor_id")
    conv.data = {**conv.data, "doctor_id": doctor_id}
    conv.state = "choose_slot"
    slots = sch.free_slots(s, doctor_id=doctor_id, phone=conv.phone, now=now, limit=8)
    if not slots:
        return _remember(conv, Reply("No free times in the next two weeks, sorry.",
                                     [Option("book", "Other doctor"), Option("staff", "Talk to staff")]))
    return _remember(conv, Reply("Here are the next available times:", [
        Option(f"s:{sl.id}", sch.fmt(sl.start), sl.doctor.name) for sl in slots]))


def _my_appointments(s, conv: Conversation, now: datetime) -> Reply:
    appts = sch.upcoming(s, conv.phone, now)
    if not appts:
        return _remember(conv, Reply("You have no upcoming appointments.",
                                     [Option("book", "Book appointment"), Option("menu", "Main menu")]))
    lines = [f"{i}. {sch.fmt(a.slot.start)} – {a.slot.doctor.name}" for i, a in enumerate(appts, 1)]
    options = []
    for i, a in enumerate(appts[:4], 1):
        options += [Option(f"r:{a.id}", f"Reschedule #{i}", sch.fmt(a.slot.start)),
                    Option(f"c:{a.id}", f"Cancel #{i}", sch.fmt(a.slot.start))]
    return _remember(conv, Reply("Your upcoming appointments:\n" + "\n".join(lines), options))
