# agent/brain.py — Claude API integration with circuit breaker + summarization
import os
import time
import logging
import yaml
from anthropic import AsyncAnthropic
from dotenv import load_dotenv

load_dotenv(override=False)
logger = logging.getLogger("agentkit")

client = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

# Rule 32: Circuit breaker state (in-memory, per process)
_circuit_breaker = {
    "failures": 0,
    "open_until": 0.0,  # Unix timestamp when circuit closes again
    "threshold": int(os.getenv("CIRCUIT_BREAKER_THRESHOLD", 3)),
    "timeout": int(os.getenv("CIRCUIT_BREAKER_TIMEOUT_SECONDS", 60)),
}

# Rule 33: Summarization prompt
SUMMARIZE_PROMPT = (
    "Summarize this WhatsApp conversation history into a single paragraph of key facts: "
    "customer name (if mentioned), what they asked about, what was resolved, and any pending items. "
    "Be factual, no commentary. Max 150 words."
)


def _cargar_config() -> dict:
    try:
        with open("config/prompts.yaml", "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        logger.error("config/prompts.yaml not found")
        return {}


def cargar_system_prompt(locale: str = "es-AR") -> str:
    config = _cargar_config()
    prompt = config.get("system_prompt", "Sos una asistente útil. Respondé en español.")
    # Rule 37: inject locale into system prompt
    return prompt.replace("{locale}", locale)


def obtener_mensaje_error() -> str:
    return _cargar_config().get("error_message", "Lo siento, estoy teniendo problemas técnicos. Por favor intentá de nuevo en unos minutos.")


def obtener_mensaje_fallback() -> str:
    return _cargar_config().get("fallback_message", "Disculpá, no entendí tu mensaje. ¿Podés reformularlo?")


def _is_circuit_open() -> bool:
    """Rule 32: Returns True if circuit breaker is active (skip Claude calls)."""
    if _circuit_breaker["open_until"] > time.time():
        return True
    # Auto-reset if timeout has passed
    if _circuit_breaker["open_until"] > 0:
        _circuit_breaker["failures"] = 0
        _circuit_breaker["open_until"] = 0.0
    return False


def _record_failure():
    """Rule 32: Record an API failure and open the circuit if threshold reached."""
    _circuit_breaker["failures"] += 1
    if _circuit_breaker["failures"] >= _circuit_breaker["threshold"]:
        open_until = time.time() + _circuit_breaker["timeout"]
        _circuit_breaker["open_until"] = open_until
        logger.error(
            f"Circuit breaker OPEN after {_circuit_breaker['failures']} failures. "
            f"Resuming in {_circuit_breaker['timeout']}s."
        )


def _record_success():
    """Rule 32: Reset failure counter on success."""
    _circuit_breaker["failures"] = 0
    _circuit_breaker["open_until"] = 0.0


async def summarize_history(history: list[dict]) -> str:
    """Rule 33: Compress old messages into a summary using Claude."""
    conversation_text = "\n".join(
        f"{m['role'].upper()}: {m['content']}" for m in history
    )
    try:
        response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            messages=[{"role": "user", "content": f"{SUMMARIZE_PROMPT}\n\n{conversation_text}"}],
        )
        return response.content[0].text
    except Exception as e:
        logger.error(f"Summarization failed: {e}")
        return ""


async def generar_respuesta(
    mensaje: str,
    historial: list[dict],
    locale: str = "es-AR",
    tenant_id: str = "default",
    summary_threshold: int = 40,
) -> tuple[str, int, int]:
    """
    Generate a response using Claude API.

    Returns:
        (response_text, input_tokens, output_tokens)
    """
    if not mensaje or len(mensaje.strip()) < 2:
        return obtener_mensaje_fallback(), 0, 0

    # Rule 32: Check circuit breaker
    if _is_circuit_open():
        logger.warning(f"Circuit breaker active — returning error message for tenant={tenant_id}")
        return obtener_mensaje_error(), 0, 0

    system_prompt = cargar_system_prompt(locale)

    # Rule 33: Summarize if conversation is too long
    mensajes: list[dict] = []
    if len(historial) > summary_threshold:
        half = len(historial) // 2
        older = historial[:half]
        recent = historial[half:]
        summary = await summarize_history(older)
        if summary:
            mensajes.append({"role": "user", "content": f"[Resumen de conversación anterior]: {summary}"})
            mensajes.append({"role": "assistant", "content": "Entendido, tengo en cuenta el contexto anterior."})
        mensajes.extend([{"role": m["role"], "content": m["content"]} for m in recent])
    else:
        mensajes = [{"role": m["role"], "content": m["content"]} for m in historial]

    mensajes.append({"role": "user", "content": mensaje})

    try:
        response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=system_prompt,
            messages=mensajes,
        )
        _record_success()
        text = response.content[0].text
        in_tok = response.usage.input_tokens
        out_tok = response.usage.output_tokens
        logger.info(f"Claude response generated: {in_tok} in / {out_tok} out [tenant={tenant_id}]")
        return text, in_tok, out_tok

    except Exception as e:
        logger.error(f"Claude API error [tenant={tenant_id}]: {e}")
        _record_failure()
        return obtener_mensaje_error(), 0, 0
