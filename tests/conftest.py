import os
import sys
from datetime import datetime
from pathlib import Path

import pytest

os.environ["DATABASE_URL"] = "sqlite://"   # in-memory for the module-level app
os.environ["RUN_SCHEDULER"] = "0"
os.environ.pop("OPENAI_API_KEY", None)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import make_session_factory  # noqa: E402
from app.seed import reset_and_seed  # noqa: E402

NOW = datetime(2026, 10, 5, 8, 0)  # a Monday morning


@pytest.fixture
def session(tmp_path):
    Session = make_session_factory(f"sqlite:///{tmp_path}/t.db")
    with Session() as s:
        reset_and_seed(s, now=NOW)
        yield s
