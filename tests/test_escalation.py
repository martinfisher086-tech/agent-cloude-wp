# tests/test_escalation.py — Rule 23/24: [ESCALATE] triggers state machine
import asyncio
import pytest
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.memory import inicializar_db, get_conversation_state, set_conversation_state


@pytest.mark.asyncio
async def test_escalation_pauses_agent():
    """After escalation, conversation state must be PAUSED."""
    await inicializar_db()
    phone = "+541155559999"
    tenant = "salon_bella_test"

    # Ensure it starts as ACTIVE
    await set_conversation_state(phone, tenant, "ACTIVE")
    state_before = await get_conversation_state(phone, tenant)
    assert state_before == "ACTIVE"

    # Simulate escalation
    await set_conversation_state(phone, tenant, "PAUSED", reason="auto_escalation")
    state_after = await get_conversation_state(phone, tenant)
    assert state_after == "PAUSED", f"Expected PAUSED, got {state_after}"
    print("✅ Escalation state machine: PASS")


@pytest.mark.asyncio
async def test_resume_reactivates_agent():
    """After /resume, conversation state must return to ACTIVE."""
    await inicializar_db()
    phone = "+541155558888"
    tenant = "salon_bella_test"

    await set_conversation_state(phone, tenant, "PAUSED", reason="test")
    await set_conversation_state(phone, tenant, "ACTIVE")
    state = await get_conversation_state(phone, tenant)
    assert state == "ACTIVE"
    print("✅ Resume command: PASS")


if __name__ == "__main__":
    asyncio.run(test_escalation_pauses_agent())
    asyncio.run(test_resume_reactivates_agent())
