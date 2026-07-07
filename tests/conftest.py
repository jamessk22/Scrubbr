import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "test.db")
    db.init_db(c)
    yield c
    c.close()


@pytest.fixture
def profile():
    from app.models import Profile
    return Profile(
        full_name="Jane Q. Public", aliases="Jane Public",
        emails="jane@example.com, jq@example.com", phones="555-0100",
        addresses="123 Main St, Springfield CA 90000\n456 Old Rd, Fresno CA 93700",
        date_of_birth="1990-01-15",
    )
