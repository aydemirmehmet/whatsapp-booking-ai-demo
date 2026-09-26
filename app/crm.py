"""Minimal CRM sync: leads are stored locally and optionally pushed to a webhook
(HubSpot / GoHighLevel / Zapier / Make catch hook, or your own endpoint)."""
from __future__ import annotations

import logging

import httpx

from .config import settings
from .db import Lead

log = logging.getLogger("crm")
ALLOWED_STATUS = {"new", "qualified", "booked", "handoff", "lost"}


def upsert_lead(s, phone: str, **fields) -> Lead:
    lead = s.get(Lead, phone) or Lead(phone=phone)
    for key in ("name", "interest", "summary"):
        if fields.get(key):
            setattr(lead, key, str(fields[key])[:2000])
    if fields.get("status") in ALLOWED_STATUS:
        lead.status = fields["status"]
    s.add(lead)
    s.commit()
    if settings.crm_webhook_url:
        try:
            httpx.post(settings.crm_webhook_url, timeout=5, json={
                "phone": lead.phone, "name": lead.name, "interest": lead.interest,
                "status": lead.status, "source": lead.source, "summary": lead.summary})
        except httpx.HTTPError as exc:  # never break the chat because the CRM is down
            log.warning("CRM webhook failed: %s", exc)
    return lead
