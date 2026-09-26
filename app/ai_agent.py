"""Demo 2: AI receptionist on WhatsApp.

With OPENAI_API_KEY set, an LLM answers using tool calls against the real
database (services, free slots, booking, CRM, human handoff). It never invents
prices or times: those only come from tools.

Without a key the demo runs in "offline mode": a small rule-based responder
answers price questions from the database and routes booking to the menu flow,
so the simulator still works end to end.
"""
from __future__ import annotations

import json
import re
from datetime import datetime

from sqlalchemy import select

from . import booking_flow
from . import scheduling as sch
from .config import settings
from .crm import upsert_lead
from .db import Conversation, MessageLog
from .whatsapp import Option, Reply

SYSTEM_PROMPT = """You are the WhatsApp receptionist of {clinic}, a dental clinic.
Style: warm, natural and short (1-3 sentences). Ask ONE question at a time.
No long paragraphs, no emoji spam, no "Great question!".
Rules:
- Prices, services and free times come ONLY from tools. Never guess them.
- To book: find the service and a preferred day/time, call find_free_slots,
  offer at most 3 times, then book_appointment with the slot_id the patient picked.
- Keep the CRM current with update_lead (interest, status, one-line summary).
- If the patient asks for a person, is upset, or asks something medical you
  cannot answer, call handoff_to_human.
Today is {today}."""

TOOLS = [
    {"type": "function", "function": {
        "name": "get_services", "description": "List services with prices (EUR) and durations.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "find_free_slots", "description": "Find free appointment times.",
        "parameters": {"type": "object", "properties": {
            "day": {"type": "string", "description": "YYYY-MM-DD, optional"},
            "doctor_id": {"type": "integer", "description": "optional"}}}}},
    {"type": "function", "function": {
        "name": "book_appointment", "description": "Book a slot for this patient.",
        "parameters": {"type": "object", "required": ["slot_id"], "properties": {
            "slot_id": {"type": "integer"}, "patient_name": {"type": "string"}}}}},
    {"type": "function", "function": {
        "name": "update_lead", "description": "Create/update the CRM lead for this chat.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string"}, "interest": {"type": "string"},
            "status": {"type": "string", "enum": ["new", "qualified", "booked", "handoff", "lost"]},
            "summary": {"type": "string"}}}}},
    {"type": "function", "function": {
        "name": "handoff_to_human", "description": "Pass the conversation to staff.",
        "parameters": {"type": "object", "properties": {"reason": {"type": "string"}}}}},
]


def run_tool(s, conv: Conversation, name: str, args: dict, now: datetime) -> dict:
    if name == "get_services":
        return {"services": [{"name": x.name, "price_eur": x.price_eur, "minutes": x.duration_min,
                              "info": x.description} for x in sch.services(s)]}
    if name == "find_free_slots":
        slots = sch.free_slots(s, doctor_id=args.get("doctor_id"), day=args.get("day"),
                               phone=conv.phone, now=now, limit=6)
        return {"slots": [{"slot_id": x.id, "time": sch.fmt(x.start), "doctor": x.doctor.name,
                           "specialty": x.doctor.specialty} for x in slots]}
    if name == "book_appointment":
        try:
            a = sch.book(s, int(args["slot_id"]), conv.phone, args.get("patient_name", ""), now)
        except sch.SlotUnavailable:
            return {"ok": False, "error": "slot no longer available"}
        upsert_lead(s, conv.phone, status="booked", name=args.get("patient_name", ""))
        return {"ok": True, "time": sch.fmt(a.slot.start), "doctor": a.slot.doctor.name}
    if name == "update_lead":
        lead = upsert_lead(s, conv.phone, **args)
        return {"ok": True, "status": lead.status}
    if name == "handoff_to_human":
        conv.human_mode, conv.state = True, "human"
        upsert_lead(s, conv.phone, status="handoff", summary=args.get("reason", ""))
        return {"ok": True}
    return {"error": f"unknown tool {name}"}


def _history(s, phone: str, limit: int = 12) -> list[dict]:
    rows = list(s.scalars(select(MessageLog).where(MessageLog.phone == phone)
                          .order_by(MessageLog.id.desc()).limit(limit)))[::-1]
    return [{"role": "user" if r.direction == "in" else "assistant", "content": r.body} for r in rows]


def handle(s, conv: Conversation, text: str, name: str = "", now: datetime | None = None,
           client=None) -> list[Reply]:
    now = now or datetime.now()
    if client is None and settings.openai_api_key:
        from openai import OpenAI
        client = OpenAI(api_key=settings.openai_api_key)
    if client is None:
        return _offline(s, conv, text, name, now)

    upsert_lead(s, conv.phone, name=name)
    messages = [{"role": "system", "content": SYSTEM_PROMPT.format(
        clinic=settings.clinic_name, today=now.strftime("%A %Y-%m-%d %H:%M"))}]
    messages += _history(s, conv.phone)  # the current inbound message is already logged
    for _ in range(6):  # tool-call rounds
        resp = client.chat.completions.create(model=settings.openai_model, messages=messages,
                                              tools=TOOLS, temperature=0.3)
        msg = resp.choices[0].message
        if not msg.tool_calls:
            return [Reply(msg.content or "Sorry, could you rephrase that?")]
        messages.append({"role": "assistant", "content": msg.content or "",
                         "tool_calls": [tc.model_dump() for tc in msg.tool_calls]})
        for tc in msg.tool_calls:
            result = run_tool(s, conv, tc.function.name, json.loads(tc.function.arguments or "{}"), now)
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(result)})
    return [Reply("Let me get a colleague to help you with this.")]


# ---------------- offline mode (no API key) ----------------

PRICE_WORDS = re.compile(r"\b(price|cost|how much|fee|€|eur|fiyat|ücret)\b", re.I)
HOURS_WORDS = re.compile(r"\b(open|hours|opening|when are you)\b", re.I)


def _offline(s, conv: Conversation, text: str, name: str, now: datetime) -> list[Reply]:
    low = text.lower()
    upsert_lead(s, conv.phone, name=name)
    matched = [x for x in sch.services(s)
               if any(w in low for w in x.name.lower().replace("&", " ").split() if len(w) > 3)]

    if PRICE_WORDS.search(low) or (matched and conv.state == "idle"):
        if matched:
            svc = matched[0]
            upsert_lead(s, conv.phone, interest=svc.name, status="qualified",
                        summary=f"Asked about {svc.name} price")
            return [booking_flow._remember(conv, Reply(
                f"{svc.name} is {svc.price_eur} EUR and takes about {svc.duration_min} minutes. "
                "Would you like me to find you a time?",
                [Option("book", "Yes, find a time"), Option("staff", "Ask a question")]))]
        lines = "\n".join(f"• {x.name}: {x.price_eur} EUR" for x in sch.services(s))
        return [Reply(f"Our main prices:\n{lines}\n\nWhich one are you interested in?")]

    if HOURS_WORDS.search(low):
        return [Reply("We're open Monday to Saturday, 09:00-17:00. Would you like to book a visit?")]

    before = len(sch.upcoming(s, conv.phone, now))
    replies = booking_flow.handle(s, conv, text, name, now)
    if conv.human_mode:
        upsert_lead(s, conv.phone, status="handoff", summary="Asked for staff")
    elif len(sch.upcoming(s, conv.phone, now)) > before:
        upsert_lead(s, conv.phone, status="booked")
    return replies
