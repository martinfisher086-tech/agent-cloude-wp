# tests/test_business_hours.py — Rule 28+38: Out-of-hours check skips Claude
import pytest
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.tenant import TenantContext, is_within_business_hours
from unittest.mock import patch
from datetime import datetime
from zoneinfo import ZoneInfo


def make_ctx(start="09:00", end="20:00", days=None, tz="America/Argentina/Buenos_Aires"):
    return TenantContext(
        tenant_id="salon_bella", name="Salón Bella", locale="es-AR",
        db_url="sqlite+aiosqlite:///data/test.db", owner_phone="+541155550001",
        business_hours={"start": start, "end": end, "timezone": tz, "days": days or [1,2,3,4,5]},
        escalation_cooldown_minutes=30, max_history_messages=20, summary_threshold=40,
        prompts_file="config/prompts.yaml", knowledge_dir="knowledge/",
    )


def test_within_hours():
    ctx = make_ctx()
    tz = ZoneInfo("America/Argentina/Buenos_Aires")
    # Wednesday 10:00 AM — should be open
    fake_now = datetime(2026, 3, 25, 10, 0, tzinfo=tz)  # Wednesday
    with patch("agent.tenant.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now
        assert is_within_business_hours(ctx) is True
    print("✅ Within hours: PASS")


def test_outside_hours():
    ctx = make_ctx()
    tz = ZoneInfo("America/Argentina/Buenos_Aires")
    # Wednesday 22:00 — should be closed
    fake_now = datetime(2026, 3, 25, 22, 0, tzinfo=tz)
    with patch("agent.tenant.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now
        assert is_within_business_hours(ctx) is False
    print("✅ Outside hours: PASS")


def test_sunday_closed():
    ctx = make_ctx()
    tz = ZoneInfo("America/Argentina/Buenos_Aires")
    # Sunday 11:00 — should be closed (not in days list)
    fake_now = datetime(2026, 3, 29, 11, 0, tzinfo=tz)  # Sunday
    with patch("agent.tenant.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now
        assert is_within_business_hours(ctx) is False
    print("✅ Sunday closed: PASS")


if __name__ == "__main__":
    test_within_hours()
    test_outside_hours()
    test_sunday_closed()
