"""Demo data: doctors, services and two weeks of 30-minute slots."""
from datetime import datetime, time, timedelta

from sqlalchemy import delete, select

from .db import (
    Appointment, Conversation, Doctor, Lead, MessageLog, ProcessedMessage, Service, Slot,
)

DOCTORS = [("Dr. Elif Kaya", "General dentistry"), ("Dr. Can Demir", "Orthodontics"),
           ("Dr. Sara Yilmaz", "Teeth whitening & aesthetics")]

SERVICES = [
    ("Check-up & cleaning", 60, 30, "Exam, scaling and polishing."),
    ("Teeth whitening", 150, 60, "In-office whitening, one session."),
    ("Filling", 90, 45, "Composite (white) filling, per tooth."),
    ("Orthodontic consultation", 40, 30, "Braces / aligner assessment."),
]


def reset_and_seed(session, now: datetime | None = None, days: int = 14) -> None:
    now = now or datetime.now()
    for model in (Appointment, Slot, Doctor, Service, Conversation, MessageLog, Lead, ProcessedMessage):
        session.execute(delete(model))
    doctors = [Doctor(name=n, specialty=s) for n, s in DOCTORS]
    session.add_all(doctors)
    session.add_all(Service(name=n, price_eur=p, duration_min=d, description=desc)
                    for n, p, d, desc in SERVICES)
    session.flush()
    day = now.date()
    for offset in range(days):
        d = day + timedelta(days=offset)
        if d.weekday() == 6:  # closed on Sunday
            continue
        for doc in doctors:
            t = datetime.combine(d, time(9, 0))
            end = datetime.combine(d, time(17, 0))
            while t < end:
                if t > now + timedelta(hours=1) and t.hour != 12:  # lunch break
                    session.add(Slot(doctor_id=doc.id, start=t))
                t += timedelta(minutes=30)
    session.commit()


def ensure_seeded(session) -> None:
    if session.scalar(select(Doctor.id).limit(1)) is None:
        reset_and_seed(session)
