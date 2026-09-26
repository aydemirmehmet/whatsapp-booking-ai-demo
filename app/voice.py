"""Demo 3: AI voice receptionist on the phone (Twilio Programmable Voice).

Same booking engine as the WhatsApp bots. Twilio sends each caller turn to
POST /voice (speech-to-text already done by Twilio <Gather input="speech">);
we answer with TwiML. The conversation logic is a small state machine so it
is fast, cheap and predictable on a phone line; an LLM can be plugged into
`understand()` for free-form questions.

After booking, the caller gets an SMS confirmation.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
from dataclasses import dataclass, field
from datetime import datetime
from xml.sax.saxutils import escape

from . import scheduling as sch
from .config import settings
from .crm import upsert_lead
from .db import Conversation
from .whatsapp import Reply

NUMBERS = {"one": 1, "first": 1, "1": 1, "two": 2, "second": 2, "2": 2, "three": 3, "third": 3, "3": 3}
YES = {"yes", "yeah", "yep", "sure", "correct", "ok", "okay", "please", "book", "right", "perfect"}
NO = {"no", "nope", "different", "another", "other", "change"}
HUMAN = {"human", "person", "receptionist", "reception", "someone", "operator", "staff", "agent"}
SPECIALTY_HINTS = {"whitening": "whitening", "white": "whitening", "braces": "orthodont",
                   "aligner": "orthodont", "orthodont": "orthodont", "check": "general",
                   "cleaning": "general", "filling": "general", "pain": "general"}


@dataclass
class VoiceTurn:
    say: str
    gather: bool = True          # keep listening
    hangup: bool = False
    transfer_to: str | None = None
    sms: list[str] = field(default_factory=list)


# ---------------- helpers ----------------

def spoken_time(dt: datetime) -> str:
    hour = dt.strftime("%I").lstrip("0")
    minute = "" if dt.minute == 0 else f":{dt.minute:02d}"
    return f"{dt.strftime('%A')} at {hour}{minute} {dt.strftime('%p')}"


def words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", (text or "").lower())


def call_key(call_sid: str) -> str:
    return "v:" + call_sid[-28:]


def verify_twilio_signature(auth_token: str, url: str, params: dict, signature: str | None) -> bool:
    """X-Twilio-Signature = base64(HMAC-SHA1(auth_token, url + sorted key/value pairs))."""
    if not auth_token:
        return True  # local demo
    if not signature:
        return False
    payload = url + "".join(k + str(params[k]) for k in sorted(params))
    digest = hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()
    return hmac.compare_digest(base64.b64encode(digest).decode(), signature)


def to_twiml(turn: VoiceTurn, action: str = "/voice") -> str:
    say = f'<Say voice="Polly.Joanna">{escape(turn.say)}</Say>'
    if turn.transfer_to:
        body = f"{say}<Dial>{escape(turn.transfer_to)}</Dial>"
    elif turn.hangup:
        body = f"{say}<Hangup/>"
    else:
        body = (f'<Gather input="speech dtmf" action="{action}" method="POST" '
                f'speechTimeout="auto" language="en-US">{say}</Gather>'
                f'<Redirect method="POST">{action}?silence=1</Redirect>')
    return f'<?xml version="1.0" encoding="UTF-8"?><Response>{body}</Response>'


# ---------------- conversation ----------------

def _pick_doctor(s, w: list[str]):
    docs = sch.doctors(s)
    for d in docs:
        last = d.name.split()[-1].lower()
        if last in w:
            return d
    for key, spec in SPECIALTY_HINTS.items():
        if any(x.startswith(key) for x in w):
            return next((d for d in docs if spec in d.specialty.lower()), None)
    if {"any", "anyone", "whoever", "first", "earliest", "soonest"} & set(w):
        return "any"
    return None


def _pick_slot(w: list[str], slots: list[dict]):
    for sl in slots:  # "thursday at 2 pm" -> matches day + time words
        tokens = set(words(sl["spoken"])) - {"at"}
        if len(tokens & set(w)) >= 2:
            return sl
    for x in w:  # "the second one", "3"
        if x in NUMBERS and NUMBERS[x] <= len(slots):
            return slots[NUMBERS[x] - 1]
    return None


def _offer(s, conv: Conversation, doctor_id: int | None, now: datetime) -> VoiceTurn:
    slots = sch.free_slots(s, doctor_id=doctor_id, phone=conv.phone, now=now, limit=3)
    if not slots:
        return VoiceTurn("Sorry, I have no free times in the next two weeks. "
                         "Let me pass you to our team.", transfer_to=settings.clinic_transfer_number or None,
                         hangup=not settings.clinic_transfer_number)
    offered = [{"id": x.id, "spoken": spoken_time(x.start), "doctor": x.doctor.name} for x in slots]
    conv.state, conv.data = "choose_slot", {**conv.data, "slots": offered, "misses": 0}
    options = ", ".join(f"{o['spoken']}" for o in offered[:-1]) + f", or {offered[-1]['spoken']}"
    who = "" if doctor_id else f" The first one is with {offered[0]['doctor']}."
    return VoiceTurn(f"I have {options}.{who} Which one works for you?")


def _miss(conv: Conversation, prompt: str) -> VoiceTurn:
    misses = conv.data.get("misses", 0) + 1
    conv.data = {**conv.data, "misses": misses}
    if misses >= 2:
        return _handoff(conv, "Sorry, I'm having trouble understanding.")
    return VoiceTurn(prompt)


def _handoff(conv: Conversation, lead_in: str) -> VoiceTurn:
    conv.state = "done"
    if settings.clinic_transfer_number:
        return VoiceTurn(f"{lead_in} Let me connect you to our reception.",
                         transfer_to=settings.clinic_transfer_number)
    return VoiceTurn(f"{lead_in} A colleague will call you back shortly. Goodbye.", hangup=True)


def handle_call(s, key: str, caller: str, speech: str | None, now: datetime | None = None,
                silence: bool = False) -> VoiceTurn:
    now = now or datetime.now()
    conv = s.get(Conversation, key)
    if conv is None:
        conv = Conversation(phone=key, state="start", data_json="{}")
        s.add(conv)
    conv.data = {**conv.data, "caller": caller}
    w = words(speech)
    turn = _step(s, conv, caller, w, now, silence)
    s.commit()
    return turn


def _step(s, conv: Conversation, caller: str, w: list[str], now: datetime, silence: bool) -> VoiceTurn:
    if conv.state == "start":
        conv.state = "choose_doctor"
        upsert_lead(s, caller, interest="Phone call", status="new")
        return VoiceTurn(f"Hi, thanks for calling {settings.clinic_name}. I'm the virtual receptionist. "
                         "I can book, move or cancel an appointment. "
                         "Which doctor would you like to see: Doctor Kaya, Doctor Demir or Doctor Yilmaz? "
                         "You can also say any doctor.")

    if silence:
        return _miss(conv, "Sorry, I didn't catch that. Could you say it again?")
    if HUMAN & set(w):
        upsert_lead(s, caller, status="handoff", summary="Asked for a person on the phone")
        return _handoff(conv, "Of course.")

    if conv.state == "choose_doctor":
        if {"cancel", "move", "reschedule", "change"} & set(w):
            appts = sch.upcoming(s, caller, now)
            if not appts:
                return VoiceTurn("I can't find an upcoming appointment for this number. "
                                 "Would you like to book one? Which doctor would you like to see?")
            a = appts[0]
            conv.state, conv.data = "confirm_cancel", {**conv.data, "appt": a.id}
            return VoiceTurn(f"I see your appointment on {spoken_time(a.slot.start)} with {a.slot.doctor.name}. "
                             "Shall I cancel it? Say yes or no.")
        doc = _pick_doctor(s, w)
        if doc is None:
            return _miss(conv, "Which doctor would you like: Doctor Kaya, Doctor Demir, Doctor Yilmaz, or any doctor?")
        return _offer(s, conv, None if doc == "any" else doc.id, now)

    if conv.state == "choose_slot":
        slot = _pick_slot(w, conv.data.get("slots", []))
        if slot is None:
            if NO & set(w):
                return _offer(s, conv, None, now)
            return _miss(conv, "Please say first, second or third.")
        try:
            sch.hold(s, slot["id"], caller, settings.slot_hold_minutes, now)
        except sch.SlotUnavailable:
            return _offer(s, conv, None, now)
        conv.state, conv.data = "confirm", {**conv.data, "slot": slot, "misses": 0}
        return VoiceTurn(f"{slot['spoken']} with {slot['doctor']}. Shall I book it? Say yes or no.")

    if conv.state == "confirm":
        slot = conv.data["slot"]
        if YES & set(w):
            try:
                appt = sch.book(s, slot["id"], caller, "", now)
            except sch.SlotUnavailable:
                return _offer(s, conv, None, now)
            conv.state = "done"
            upsert_lead(s, caller, status="booked", summary=f"Booked by phone: {slot['spoken']}")
            text = (f"{settings.clinic_name}: your appointment with {appt.slot.doctor.name} is confirmed for "
                    f"{sch.fmt(appt.slot.start)}. Reply to this message to change it.")
            return VoiceTurn(f"Done. You're booked for {slot['spoken']} with {slot['doctor']}. "
                             "I've sent you a text confirmation. Thanks for calling, goodbye.",
                             hangup=True, sms=[text])
        if NO & set(w):
            return _offer(s, conv, None, now)
        return _miss(conv, "Sorry, should I book it? Please say yes or no.")

    if conv.state == "confirm_cancel":
        if YES & set(w):
            sch.cancel(s, conv.data["appt"], caller)
            conv.state = "choose_doctor"
            upsert_lead(s, caller, status="lost", summary="Cancelled by phone")
            return VoiceTurn("Your appointment is cancelled. Would you like to book a new time? "
                             "If yes, tell me which doctor.",)
        conv.state = "done"
        return VoiceTurn("Okay, I'll keep your appointment. Goodbye.", hangup=True)

    return VoiceTurn("Thanks for calling. Goodbye.", hangup=True)


class TwilioSMSSender:
    """Sends SMS through the Twilio REST API (used for booking confirmations)."""

    def __init__(self, sid: str, token: str, from_number: str):
        self.url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
        self.auth = (sid, token)
        self.from_number = from_number

    def send(self, to: str, reply: Reply) -> None:
        import httpx
        httpx.post(self.url, auth=self.auth, timeout=10,
                   data={"From": self.from_number, "To": to, "Body": reply.text})
