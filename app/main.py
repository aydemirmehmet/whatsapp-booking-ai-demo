"""FastAPI app: WhatsApp webhook, Twilio voice webhook and a browser simulator."""
from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse, Response
from pydantic import BaseModel
from sqlalchemy import select

from . import scheduling as sch
from .config import settings
from .db import Appointment, Conversation, Lead, Slot, make_session_factory
from .seed import ensure_seeded, reset_and_seed
from .service import process_inbound, run_reminders
from .voice import TwilioSMSSender, call_key, handle_call, to_twiml, verify_twilio_signature
from .whatsapp import CloudAPISender, Inbound, MemorySender, Reply, parse_webhook, verify_signature

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("app")
STATIC = Path(__file__).parent / "static"


class VoiceIn(BaseModel):
    call_id: str
    phone: str = "905551112233"
    text: str | None = None
    silence: bool = False


class SendIn(BaseModel):
    phone: str = "905551112233"
    name: str = "Ayse Demir"
    text: str
    mode: str = "booking"  # booking | ai


def _serialize(replies) -> list[dict]:
    return [{"text": r.text, "template": r.template,
             "options": [{"id": o.id, "title": o.title, "description": o.description}
                         for o in r.options]} for r in replies]


def create_app(database_url: str | None = None, live_sender=None) -> FastAPI:
    Session = make_session_factory(database_url or settings.database_url)
    with Session() as s:
        ensure_seeded(s)

    if live_sender is None:
        live_sender = (CloudAPISender(settings.access_token, settings.phone_number_id,
                                      settings.graph_version, settings.template_lang)
                       if settings.cloud_api_enabled else MemorySender())

    sms_sender = (TwilioSMSSender(settings.twilio_account_sid, settings.twilio_auth_token,
                                  settings.twilio_from_number)
                  if settings.twilio_account_sid and settings.twilio_from_number else MemorySender())

    async def reminder_loop():
        while True:
            await asyncio.sleep(60)
            try:
                with Session() as s:
                    n = run_reminders(s, live_sender)
                    if n:
                        log.info("sent %s reminders", n)
            except Exception:
                log.exception("reminder job failed")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(reminder_loop()) if settings.run_scheduler else None
        yield
        if task:
            task.cancel()

    app = FastAPI(title="WhatsApp Automation Demos", lifespan=lifespan)
    app.state.Session = Session

    # ---------- WhatsApp Cloud API webhook ----------

    @app.get("/webhook")
    def verify(request: Request):
        q = request.query_params
        if q.get("hub.mode") == "subscribe" and q.get("hub.verify_token") == settings.verify_token:
            return PlainTextResponse(q.get("hub.challenge", ""))
        raise HTTPException(403, "verification failed")

    @app.post("/webhook")
    async def receive(request: Request, background: BackgroundTasks):
        raw = await request.body()
        if not verify_signature(settings.app_secret, raw, request.headers.get("X-Hub-Signature-256")):
            raise HTTPException(403, "bad signature")
        messages = parse_webhook(await request.json())

        def work():
            with Session() as s:
                for m in messages:
                    process_inbound(s, m, live_sender)

        background.add_task(work)  # answer Meta within its timeout, process after
        return {"received": len(messages)}

    # ---------- Twilio voice webhook ----------

    @app.post("/voice")
    async def voice(request: Request):
        form = dict(await request.form())
        url = (settings.public_base_url.rstrip("/") + request.url.path + (
            "?" + request.url.query if request.url.query else "")) if settings.public_base_url else str(request.url)
        if not verify_twilio_signature(settings.twilio_auth_token, url, form,
                                       request.headers.get("X-Twilio-Signature")):
            raise HTTPException(403, "bad signature")
        with Session() as s:
            turn = handle_call(s, call_key(form.get("CallSid", "local")), form.get("From", ""),
                               form.get("SpeechResult") or form.get("Digits"),
                               silence=request.query_params.get("silence") == "1")
        for text in turn.sms:
            sms_sender.send(form.get("From", ""), Reply(text))
        return Response(to_twiml(turn), media_type="application/xml")

    @app.post("/admin/release/{phone}")
    def release(phone: str):
        """Staff finished: hand the chat back to the bot."""
        with Session() as s:
            conv = s.get(Conversation, phone)
            if conv:
                conv.human_mode, conv.state = False, "idle"
                s.commit()
        return {"ok": True}

    # ---------- browser simulator ----------

    @app.get("/")
    def root():
        return RedirectResponse("/demo")

    @app.get("/demo")
    def demo_page():
        return FileResponse(STATIC / "demo.html")

    @app.post("/demo/api/send")
    def demo_send(body: SendIn):
        sender = MemorySender()
        with Session() as s:
            replies = process_inbound(
                s, Inbound(wamid=f"demo-{uuid.uuid4()}", phone=body.phone, name=body.name,
                           text=body.text), sender, mode=body.mode)
        return {"replies": _serialize(replies), "state": _state(body.phone)}

    @app.post("/demo/api/voice")
    def demo_voice(body: VoiceIn):
        with Session() as s:
            turn = handle_call(s, call_key(body.call_id), body.phone, body.text, silence=body.silence)
        return {"say": turn.say, "hangup": turn.hangup, "transfer_to": turn.transfer_to,
                "sms": turn.sms, "twiml": to_twiml(turn), "state": _state(body.phone)}

    @app.post("/demo/api/reset")
    def demo_reset():
        with Session() as s:
            reset_and_seed(s)
        return {"ok": True}

    @app.post("/demo/api/fast-forward")
    def demo_fast_forward(phone: str = "905551112233", hours_before: float = 23):
        """Pretend the clock is `hours_before` hours before the patient's next
        appointment and run the reminder job, to show reminders in a demo."""
        with Session() as s:
            nxt = sch.upcoming(s, phone)
            if not nxt:
                return {"replies": [], "note": "no upcoming appointment"}
            fake_now = nxt[0].slot.start - timedelta(hours=hours_before)
            sender = MemorySender()
            run_reminders(s, sender, now=fake_now)
            return {"replies": _serialize(sender.outbox.get(phone, [])),
                    "simulated_time": fake_now.isoformat(timespec="minutes"), "state": _state(phone)}

    @app.get("/demo/api/state")
    def demo_state(phone: str = "905551112233"):
        return _state(phone)

    def _state(phone: str) -> dict:
        with Session() as s:
            appts = s.scalars(select(Appointment).join(Slot).where(Appointment.phone == phone)
                              .order_by(Slot.start))
            lead = s.get(Lead, phone)
            conv = s.get(Conversation, phone)
            return {
                "appointments": [{"id": a.id, "time": sch.fmt(a.slot.start), "doctor": a.slot.doctor.name,
                                  "reminded_24h": a.reminded_24h, "reminded_2h": a.reminded_2h}
                                 for a in appts],
                "lead": lead and {"name": lead.name, "interest": lead.interest, "status": lead.status,
                                  "summary": lead.summary},
                "conversation": conv and {"state": conv.state, "human_mode": conv.human_mode},
                "ai_enabled": bool(settings.openai_api_key),
                "server_time": datetime.now().isoformat(timespec="minutes"),
            }

    return app


app = create_app()
