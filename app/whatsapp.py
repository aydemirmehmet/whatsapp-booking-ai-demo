"""WhatsApp Cloud API helpers: signature check, webhook parsing, message senders."""
from __future__ import annotations

import hashlib
import hmac
import logging
from collections import defaultdict
from dataclasses import dataclass, field

import httpx

log = logging.getLogger("whatsapp")


# ---------- outgoing message model ----------

@dataclass
class Option:
    id: str
    title: str          # max 20 chars for buttons, 24 for list rows
    description: str = ""


@dataclass
class Reply:
    text: str
    options: list[Option] = field(default_factory=list)
    template: str | None = None               # name of an approved template
    template_params: list[str] = field(default_factory=list)

    def to_cloud_payload(self, to: str, lang: str = "en") -> dict:
        """Build a Cloud API /messages payload.

        <=3 short options -> reply buttons, more -> list message, none -> text.
        Templates are used outside the 24h customer-service window (reminders).
        """
        base = {"messaging_product": "whatsapp", "to": to}
        if self.template:
            return {**base, "type": "template", "template": {
                "name": self.template, "language": {"code": lang},
                "components": [{"type": "body", "parameters": [
                    {"type": "text", "text": p} for p in self.template_params]}]}}
        if not self.options:
            return {**base, "type": "text", "text": {"body": self.text, "preview_url": False}}
        if len(self.options) <= 3 and all(len(o.title) <= 20 for o in self.options):
            return {**base, "type": "interactive", "interactive": {
                "type": "button", "body": {"text": self.text},
                "action": {"buttons": [{"type": "reply", "reply": {"id": o.id, "title": o.title}}
                                       for o in self.options]}}}
        return {**base, "type": "interactive", "interactive": {
            "type": "list", "body": {"text": self.text},
            "action": {"button": "Choose", "sections": [{"title": "Options", "rows": [
                {"id": o.id, "title": o.title[:24], "description": o.description[:72]}
                for o in self.options[:10]]}]}}}


# ---------- incoming ----------

@dataclass
class Inbound:
    wamid: str
    phone: str
    name: str
    text: str          # free text, or the id of the tapped button / list row


def verify_signature(app_secret: str, raw_body: bytes, header: str | None) -> bool:
    """Validate X-Hub-Signature-256. If no app secret is configured (local demo), allow."""
    if not app_secret:
        return True
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


def parse_webhook(payload: dict) -> list[Inbound]:
    """Extract user messages; ignores status callbacks (sent/delivered/read)."""
    out: list[Inbound] = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            names = {c.get("wa_id"): c.get("profile", {}).get("name", "")
                     for c in value.get("contacts", [])}
            for m in value.get("messages", []):
                kind = m.get("type")
                if kind == "text":
                    text = m["text"]["body"]
                elif kind == "interactive":
                    inter = m["interactive"]
                    text = (inter.get("button_reply") or inter.get("list_reply") or {}).get("id", "")
                elif kind == "button":  # quick-reply button on a template
                    text = m["button"].get("payload") or m["button"].get("text", "")
                else:
                    text = f"[{kind}]"
                out.append(Inbound(wamid=m["id"], phone=m["from"],
                                   name=names.get(m["from"], ""), text=text.strip()))
    return out


# ---------- senders ----------

class MemorySender:
    """Keeps outgoing messages in memory. Used by the web simulator and tests."""

    def __init__(self) -> None:
        self.outbox: dict[str, list[Reply]] = defaultdict(list)

    def send(self, to: str, reply: Reply) -> None:
        self.outbox[to].append(reply)


class CloudAPISender:
    """Sends through graph.facebook.com with retries on 5xx / network errors."""

    def __init__(self, token: str, phone_number_id: str, version: str = "v21.0", lang: str = "en"):
        self.url = f"https://graph.facebook.com/{version}/{phone_number_id}/messages"
        self.headers = {"Authorization": f"Bearer {token}"}
        self.lang = lang

    def send(self, to: str, reply: Reply) -> None:
        payload = reply.to_cloud_payload(to, self.lang)
        for attempt in range(3):
            try:
                r = httpx.post(self.url, json=payload, headers=self.headers, timeout=10)
                if r.status_code < 500:
                    if r.is_error:
                        log.error("WhatsApp API %s: %s", r.status_code, r.text)
                    return
            except httpx.HTTPError as exc:
                log.warning("send attempt %s failed: %s", attempt + 1, exc)
        log.error("giving up sending to %s", to)
