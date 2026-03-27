# tests/test_circuit_breaker.py — Rule 32: 3 API failures activate circuit breaker
import asyncio
import pytest
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent.brain as brain


@pytest.mark.asyncio
async def test_circuit_breaker():
    """After CIRCUIT_BREAKER_THRESHOLD failures, circuit opens and error msg is returned."""
    # Reset breaker state
    brain._circuit_breaker["failures"] = 0
    brain._circuit_breaker["open_until"] = 0.0
    threshold = brain._circuit_breaker["threshold"]

    # Simulate failures
    for _ in range(threshold):
        brain._record_failure()

    assert brain._is_circuit_open(), "Circuit should be open after threshold failures"

    # Next call should return error message without calling Claude
    respuesta, in_tok, out_tok = await brain.generar_respuesta("hola", [], tenant_id="test")
    assert in_tok == 0, "Circuit breaker should skip Claude (no tokens used)"
    print("✅ Circuit breaker: PASS")

    # Reset for other tests
    brain._circuit_breaker["failures"] = 0
    brain._circuit_breaker["open_until"] = 0.0


if __name__ == "__main__":
    asyncio.run(test_circuit_breaker())
