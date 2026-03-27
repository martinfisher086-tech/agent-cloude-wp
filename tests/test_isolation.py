# tests/test_isolation.py — Rule 18: Verify tenant data never crosses boundaries
import asyncio
import pytest
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.memory import inicializar_db, guardar_mensaje, obtener_historial


@pytest.mark.asyncio
async def test_tenant_isolation():
    """Messages saved for tenant A must NOT appear when querying tenant B."""
    await inicializar_db()
    phone = "+541155550001"

    await guardar_mensaje(phone, "user", "Hola soy tenant A", tenant_id="tenant_a")
    await guardar_mensaje(phone, "user", "Hola soy tenant B", tenant_id="tenant_b")

    history_a = await obtener_historial(phone, tenant_id="tenant_a")
    history_b = await obtener_historial(phone, tenant_id="tenant_b")

    assert any("tenant A" in m["content"] for m in history_a), "Tenant A message missing"
    assert not any("tenant A" in m["content"] for m in history_b), "Cross-tenant data leak!"
    assert any("tenant B" in m["content"] for m in history_b), "Tenant B message missing"
    assert not any("tenant B" in m["content"] for m in history_a), "Cross-tenant data leak!"
    print("✅ Tenant isolation: PASS")


if __name__ == "__main__":
    asyncio.run(test_isolation())

async def test_isolation():
    await test_tenant_isolation()
