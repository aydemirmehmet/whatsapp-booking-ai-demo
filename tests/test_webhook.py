import hashlib
import hmac
import json

from app.whatsapp import Option, Reply, parse_webhook, verify_signature


def payload(text="hi", wamid="wamid.1", interactive_id=None):
    msg = {"from": "905550000000", "id": wamid, "type": "text", "text": {"body": text}}
    if interactive_id:
        msg = {"from": "905550000000", "id": wamid, "type": "interactive",
               "interactive": {"type": "button_reply", "button_reply": {"id": interactive_id, "title": "x"}}}
    return {"entry": [{"changes": [{"value": {
        "contacts": [{"wa_id": "905550000000", "profile": {"name": "Ali Veli"}}],
        "messages": [msg]}}]}]}


def test_signature_valid_and_invalid():
    body = json.dumps(payload()).encode()
    good = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    assert verify_signature("secret", body, good)
    assert not verify_signature("secret", body, "sha256=deadbeef")
    assert not verify_signature("secret", body, None)


def test_parse_text_and_button():
    [m] = parse_webhook(payload("Hello"))
    assert (m.phone, m.name, m.text) == ("905550000000", "Ali Veli", "Hello")
    [b] = parse_webhook(payload(interactive_id="book"))
    assert b.text == "book"


def test_status_callbacks_are_ignored():
    status_only = {"entry": [{"changes": [{"value": {"statuses": [{"id": "x", "status": "read"}]}}]}]}
    assert parse_webhook(status_only) == []


def test_payload_shapes():
    assert Reply("hi").to_cloud_payload("1")["type"] == "text"
    btn = Reply("q", [Option("a", "A"), Option("b", "B")]).to_cloud_payload("1")
    assert btn["interactive"]["type"] == "button"
    lst = Reply("q", [Option(str(i), f"Opt {i}") for i in range(5)]).to_cloud_payload("1")
    assert lst["interactive"]["type"] == "list"
    tpl = Reply("r", template="appointment_reminder", template_params=["A"]).to_cloud_payload("1")
    assert tpl["type"] == "template"


def test_http_webhook_verify_and_signature(monkeypatch):
    from fastapi.testclient import TestClient
    from app import main
    client = TestClient(main.create_app("sqlite://"))
    r = client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "demo-verify-token",
                                       "hub.challenge": "42"})
    assert r.text == "42"
    assert client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "no"}).status_code == 403


def test_http_webhook_end_to_end():
    from fastapi.testclient import TestClient
    from app import main
    from app.whatsapp import MemorySender
    sender = MemorySender()
    client = TestClient(main.create_app("sqlite://", live_sender=sender))
    r = client.post("/webhook", json=payload("hi", wamid="e2e-1"))
    assert r.json() == {"received": 1}
    client.post("/webhook", json=payload("hi", wamid="e2e-1"))  # Meta retry
    assert len(sender.outbox["905550000000"]) == 1
    assert "Welcome" in sender.outbox["905550000000"][0].text
