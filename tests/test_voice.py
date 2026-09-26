import base64
import hashlib
import hmac

from app import scheduling as sch
from app.db import Lead
from app.voice import handle_call, to_twiml, verify_twilio_signature
from tests.conftest import NOW

CALLER = "+905551119999"


def say(session, text, key="v:call-1", silence=False):
    return handle_call(session, key, CALLER, text, now=NOW, silence=silence)


def test_book_by_phone_end_to_end(session):
    greet = say(session, None)
    assert "Which doctor" in greet.say and greet.gather
    offer = say(session, "Doctor Kaya please")
    assert offer.say.startswith("I have") and "Which one" in offer.say
    confirm = say(session, "the second one")
    assert "Shall I book it" in confirm.say
    done = say(session, "yes please")
    assert done.hangup and "booked" in done.say
    assert len(done.sms) == 1 and "confirmed" in done.sms[0]
    [appt] = sch.upcoming(session, CALLER, NOW)
    assert appt.slot.doctor.name == "Dr. Elif Kaya"
    assert session.get(Lead, CALLER).status == "booked"


def test_specialty_words_pick_the_right_doctor(session):
    say(session, None, key="v:c2")
    offer = say(session, "I want teeth whitening", key="v:c2")
    assert offer.say.startswith("I have")
    confirm = say(session, "first", key="v:c2")
    assert "Yilmaz" in confirm.say


def test_two_misunderstandings_hand_over_to_a_person(session):
    say(session, None, key="v:c3")
    assert not say(session, "banana", key="v:c3").hangup
    end = say(session, "banana again", key="v:c3")
    assert end.hangup or end.transfer_to


def test_asking_for_a_person_hands_over(session):
    say(session, None, key="v:c4")
    end = say(session, "can I talk to a person", key="v:c4")
    assert end.hangup or end.transfer_to
    assert session.get(Lead, CALLER).status == "handoff"


def test_cancel_by_phone(session):
    slot = sch.free_slots(session, now=NOW)[0]
    sch.book(session, slot.id, CALLER, now=NOW)
    say(session, None, key="v:c5")
    ask = say(session, "I need to cancel my appointment", key="v:c5")
    assert "Shall I cancel" in ask.say
    say(session, "yes", key="v:c5")
    assert sch.upcoming(session, CALLER, NOW) == []


def test_twiml_shapes():
    from app.voice import VoiceTurn
    assert "<Gather" in to_twiml(VoiceTurn("Hi"))
    assert "<Hangup/>" in to_twiml(VoiceTurn("Bye", hangup=True))
    assert "<Dial>+1555</Dial>" in to_twiml(VoiceTurn("Wait", transfer_to="+1555"))
    assert "&amp;" in to_twiml(VoiceTurn("A & B"))


def test_twilio_signature():
    url, params, token = "https://example.com/voice", {"CallSid": "CA1", "From": "+1"}, "secret"
    data = url + "CallSidCA1From+1"
    sig = base64.b64encode(hmac.new(token.encode(), data.encode(), hashlib.sha1).digest()).decode()
    assert verify_twilio_signature(token, url, params, sig)
    assert not verify_twilio_signature(token, url, params, "wrong")


def test_http_voice_webhook_returns_twiml():
    from fastapi.testclient import TestClient
    from app import main
    client = TestClient(main.create_app("sqlite://"))
    r = client.post("/voice", data={"CallSid": "CAtest", "From": "+15550001"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/xml")
    assert "<Gather" in r.text and "virtual receptionist" in r.text
