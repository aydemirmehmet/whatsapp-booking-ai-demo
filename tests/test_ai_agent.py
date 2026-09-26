"""AI agent tests with a fake OpenAI client (no network, deterministic)."""
import json
from types import SimpleNamespace as NS

from app import scheduling as sch
from app.db import Lead
from app.service import process_inbound
from app.whatsapp import Inbound, MemorySender
from tests.conftest import NOW

PHONE = "905551112233"


def tool_call(i, name, args):
    return NS(id=f"call{i}", type="function", function=NS(name=name, arguments=json.dumps(args)),
              model_dump=lambda: {"id": f"call{i}", "type": "function",
                                  "function": {"name": name, "arguments": json.dumps(args)}})


class FakeClient:
    """Plays back a scripted sequence of model responses."""

    def __init__(self, script):
        self.script = list(script)
        self.seen = []
        self.chat = NS(completions=NS(create=self._create))

    def _create(self, **kw):
        self.seen.append(kw["messages"])
        step = self.script.pop(0)
        msg = NS(content=step, tool_calls=None) if isinstance(step, str) else NS(content="", tool_calls=step)
        return NS(choices=[NS(message=msg)])


def test_agent_books_through_tools_and_updates_crm(session):
    slot = sch.free_slots(session, now=NOW)[0]
    client = FakeClient([
        [tool_call(1, "get_services", {})],
        [tool_call(2, "update_lead", {"interest": "Teeth whitening", "status": "qualified",
                                      "summary": "Wants whitening"})],
        [tool_call(3, "book_appointment", {"slot_id": slot.id, "patient_name": "Ayse"})],
        "Done! You're booked. See you then.",
    ])
    replies = process_inbound(session, Inbound("a1", PHONE, "Ayse", "Book whitening please"),
                              MemorySender(), mode="ai", now=NOW, ai_client=client)
    assert replies[0].text.startswith("Done")
    assert len(sch.upcoming(session, PHONE, NOW)) == 1
    lead = session.get(Lead, PHONE)
    assert lead.status == "booked" and lead.interest == "Teeth whitening"
    # tool results were fed back to the model
    assert any(m.get("role") == "tool" and "Teeth whitening" in m["content"] for m in client.seen[1])


def test_agent_handoff_silences_bot(session):
    client = FakeClient([[tool_call(1, "handoff_to_human", {"reason": "asked for a person"})],
                         "A colleague will reply shortly."])
    process_inbound(session, Inbound("a2", PHONE, "Ayse", "Can I speak to someone?"),
                    MemorySender(), mode="ai", now=NOW, ai_client=client)
    assert session.get(Lead, PHONE).status == "handoff"
    assert process_inbound(session, Inbound("a3", PHONE, "Ayse", "hello?"), MemorySender(),
                           mode="ai", now=NOW, ai_client=FakeClient([])) == []


def test_offline_mode_answers_price_from_database(session):
    [r] = process_inbound(session, Inbound("o1", PHONE, "Ayse", "How much is teeth whitening?"),
                          MemorySender(), mode="ai", now=NOW)
    assert "150 EUR" in r.text
    assert session.get(Lead, PHONE).status == "qualified"
