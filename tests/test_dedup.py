# tests/test_dedup.py — Rule 30: Duplicate message_id must be ignored
import asyncio
import pytest
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.memory import inicializar_db, is_duplicate_message


@pytest.mark.asyncio
async def test_deduplication():
    """Second call with same message_id must return True (is duplicate)."""
    await inicializar_db()
    import uuid
    msg_id = f"test-dedup-{uuid.uuid4().hex[:12]}"  # unique per run
    tenant = "salon_bella"

    first = await is_duplicate_message(msg_id, tenant)
    assert first is False, "First call should not be a duplicate"

    second = await is_duplicate_message(msg_id, tenant)
    assert second is True, "Second call with same ID should be a duplicate"
    print("✅ Deduplication: PASS")


if __name__ == "__main__":
    asyncio.run(test_deduplication())
