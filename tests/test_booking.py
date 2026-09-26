from datetime import timedelta

import pytest

from app import scheduling as sch
from app.service import process_inbound, run_reminders
from app.whatsapp import Inbound, MemorySender
from tests.conftest import NOW

PHONE = "905551112233"
_n = 0


def say(s, text, sender=None, phone=PHONE, mode="booking"):
    global _n
    _n += 1
    sender = sender or MemorySender()
    return process_inbound(s, Inbound(f"w{_n}", phone, "Ayse Demir", text), sender, mode=mode, now=NOW)


def test_full_booking_flow_by_typing_numbers(session):
    assert "Welcome" in say(session, "hi")[0].text
    assert "doctor" in say(session, "1")[0].text          # Book appointment
    slots = say(session, "1")[0]                            # first doctor
    assert slots.options and slots.options[0].id.startswith("s:")
    confirm = say(session, "1")[0]                          # first slot
    assert "Shall I book" in confirm.text
    done = say(session, "1")[0]                             # yes
    assert done.text.startswith("Booked")
    assert len(sch.upcoming(session, PHONE, NOW)) == 1


def test_no_double_booking(session):
    slot = sch.free_slots(session, now=NOW)[0]
    sch.book(session, slot.id, "900000000001", now=NOW)
    with pytest.raises(sch.SlotUnavailable):
        sch.book(session, slot.id, "900000000002", now=NOW)


def test_hold_blocks_other_patients(session):
    slot = sch.free_slots(session, now=NOW)[0]
    sch.hold(session, slot.id, "900000000001", minutes=5, now=NOW)
    assert slot.id not in [x.id for x in sch.free_slots(session, now=NOW, limit=50, phone="900000000002")]
    with pytest.raises(sch.SlotUnavailable):
        sch.book(session, slot.id, "900000000002", now=NOW)
    # hold expires
    later = NOW + timedelta(minutes=6)
    assert slot.id in [x.id for x in sch.free_slots(session, now=later, limit=50, phone="900000000002")]


def test_duplicate_webhook_delivery_is_processed_once(session):
    sender = MemorySender()
    msg = Inbound("same-id", PHONE, "Ayse", "hi")
    process_inbound(session, msg, sender, mode="booking", now=NOW)
    process_inbound(session, msg, sender, mode="booking", now=NOW)
    assert len(sender.outbox[PHONE]) == 1


def test_human_handover_silences_bot(session):
    say(session, "I want to talk to a person")
    assert say(session, "hello?") == []
    assert "Welcome" in say(session, "menu")[0].text


def test_cancel_and_reschedule(session):
    slot = sch.free_slots(session, now=NOW)[0]
    appt = sch.book(session, slot.id, PHONE, now=NOW)
    say(session, "hi")
    r = say(session, f"r:{appt.id}")[0]
    new_slot = r.options[1].id
    say(session, new_slot)
    done = say(session, "yes:" + new_slot[2:])[0]
    assert done.text.startswith("Rescheduled")
    ups = sch.upcoming(session, PHONE, NOW)
    assert len(ups) == 1 and ups[0].slot_id != slot.id
    assert "cancelled" in say(session, f"c:{ups[0].id}")[0].text


def test_reminders_sent_once_each(session):
    slot = sch.free_slots(session, now=NOW + timedelta(days=1))[0]
    sch.book(session, slot.id, PHONE, "Ayse", now=NOW)
    sender = MemorySender()
    assert run_reminders(session, sender, now=slot.start - timedelta(hours=23)) == 1
    assert run_reminders(session, sender, now=slot.start - timedelta(hours=22)) == 0
    assert run_reminders(session, sender, now=slot.start - timedelta(hours=1, minutes=30)) == 1
    assert run_reminders(session, sender, now=slot.start - timedelta(hours=1)) == 0
    assert all(r.template == "appointment_reminder" for r in sender.outbox[PHONE])
