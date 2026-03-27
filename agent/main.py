# agent/main.py — FastAPI server + webhook handler
# Production-grade: multi-tenant, rate-limited, circuit-breaker, dedup, escalation
import os
import sys
import time
import logging
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import PlainTextResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from dotenv import load_dotenv

# ── TEMP DEBUG — remove after Railway diagnosis ───────────────────────────
import os as _os
print("DEBUG ENV CHECK:", flush=True)
print(f"  ANTHROPIC_API_KEY present: {'ANTHROPIC_API_KEY' in _os.environ}", flush=True)
print(f"  WHATSAPP_PROVIDER present: {'WHATSAPP_PROVIDER' in _os.environ}", flush=True)
print(f"  ADMIN_SECRET present: {'ADMIN_SECRET' in _os.environ}", flush=True)
print(f"  WHAPI_TOKEN present: {'WHAPI_TOKEN' in _os.environ}", flush=True)
print(f"  Matching keys: {[k for k in _os.environ if any(x in k for x in ['WHATSAPP','ANTHROPIC','ADMIN','WHAPI'])]}", flush=True)
# ── END TEMP DEBUG ────────────────────────────────────────────────────────

load_dotenv(override=False)

# ── Google credentials: write from env var if file path not present ───────
# Allows passing credentials as GOOGLE_CREDENTIALS_CONTENT JSON string
# (Railway env var) instead of a mounted file.
_creds_content = os.getenv("GOOGLE_CREDENTIALS_CONTENT", "")
if _creds_content:
    _creds_path = "/tmp/google_credentials.json"
    with open(_creds_path, "w") as _f:
        _f.write(_creds_content)
    os.environ["GOOGLE_CREDENTIALS_JSON"] = _creds_path

# ── Rule 35: Startup environment validation ───────────────────────────────
REQUIRED_ENV_VARS = ["ANTHROPIC_API_KEY", "WHATSAPP_PROVIDER", "ADMIN_SECRET"]
PROVIDER_VARS = {
    "whapi": ["WHAPI_TOKEN"],
    "meta": ["META_ACCESS_TOKEN", "META_PHONE_NUMBER_ID", "META_VERIFY_TOKEN"],
    "twilio": ["TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER"],
}

def _validate_env():
    missing = [v for v in REQUIRED_ENV_VARS if not os.getenv(v)]
    provider = os.getenv("WHATSAPP_PROVIDER", "whapi").lower()
    if provider in PROVIDER_VARS:
        missing += [v for v in PROVIDER_VARS[provider] if not os.getenv(v)]
    if missing:
        print(f"[AgentKit] ERROR: Missing required env vars: {', '.join(missing)}", flush=True)
        print("[AgentKit] Set them in your .env file and restart.", flush=True)
        sys.exit(1)

_validate_env()

PORT = int(os.getenv("PORT", 8000))

# ── Rule 36: Structured JSON logging in production ────────────────────────
ENVIRONMENT = os.getenv("ENVIRONMENT", "development")

class JsonFormatter(logging.Formatter):
    def format(self, record):
        log = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "event": record.getMessage(),
            "logger": record.name,
        }
        if hasattr(record, "tenant_id"):
            log["tenant_id"] = record.tenant_id
        if hasattr(record, "phone_tail"):
            log["customer_phone"] = f"****{record.phone_tail}"
        if hasattr(record, "duration_ms"):
            log["duration_ms"] = record.duration_ms
        return json.dumps(log, ensure_ascii=False)

handler = logging.StreamHandler()
if ENVIRONMENT == "production":
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler])
else:
    logging.basicConfig(level=logging.DEBUG)

logger = logging.getLogger("agentkit")

# ── Imports after logging setup ───────────────────────────────────────────
from agent.brain import generar_respuesta
from agent.memory import (
    inicializar_db, guardar_mensaje, obtener_historial,
    is_duplicate_message, get_conversation_state, log_usage,
)
from agent.providers import obtener_proveedor
from agent.tenant import load_tenants, get_tenant_context, get_tenant_count, setup_sighup_handler, is_within_business_hours
from agent.escalation import handle_escalation, handle_owner_command
import yaml

proveedor = obtener_proveedor()
_start_time = time.time()

# ── Rule 13: Rate limiter (20 requests/minute/IP) ─────────────────────────
limiter = Limiter(key_func=get_remote_address)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await inicializar_db()
    load_tenants()
    setup_sighup_handler()
    logger.info(f"AgentKit started | provider={proveedor.__class__.__name__} | env={ENVIRONMENT} | port={PORT}")
    yield


app = FastAPI(title="AgentKit — Salón Bella", version="1.0.0", lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


def _load_prompts() -> dict:
    try:
        with open("config/prompts.yaml", "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


# ── Endpoints ─────────────────────────────────────────────────────────────

@app.get("/")
async def health_check():
    return {"status": "ok", "service": "agentkit", "environment": ENVIRONMENT}


@app.get("/version")
async def version():
    """Rule 44: Health + version endpoint."""
    return {
        "version": "1.0.0",
        "model": "claude-sonnet-4-6",
        "tenants": get_tenant_count(),
        "uptime_seconds": int(time.time() - _start_time),
    }


@app.get("/webhook")
async def webhook_verificacion(request: Request):
    """GET verification for Meta Cloud API (no-op for Whapi)."""
    result = await proveedor.validar_webhook(request)
    if result is not None:
        return PlainTextResponse(str(result))
    return {"status": "ok"}


@app.post("/webhook")
@limiter.limit(f"{os.getenv('RATE_LIMIT_PER_MINUTE', '20')}/minute")  # Rule 13
async def webhook_handler(request: Request):
    """
    Main webhook handler.
    Rules applied: 12 (sig validation), 13 (rate limit), 14 (no error exposure),
    17-20 (tenant), 24 (escalation), 27 (owner commands), 28 (business hours),
    30 (dedup), 31 (typing), 34 (media), 36 (JSON logging).
    """
    t0 = time.time()
    try:
        # Rule 12: Validate webhook signature BEFORE reading payload
        if hasattr(proveedor, '_validate_token_header'):
            if not proveedor._validate_token_header(request):
                logger.warning("Webhook signature validation failed — returning 401")
                raise HTTPException(status_code=401, detail="Unauthorized")

        mensajes = await proveedor.parsear_webhook(request)

        for msg in mensajes:
            if msg.es_propio or not msg.texto and msg.tipo == "text":
                continue

            phone_tail = msg.telefono[-4:] if len(msg.telefono) >= 4 else msg.telefono

            # Rule 17-20: Tenant resolution
            tenant_ctx = get_tenant_context(msg.destino or msg.telefono)
            if tenant_ctx is None:
                # Rule 20: Unknown tenant → HTTP 200, no processing
                return {"status": "ok"}

            tenant_id = tenant_ctx.tenant_id

            # Rule 30: Deduplication
            if await is_duplicate_message(msg.mensaje_id, tenant_id):
                logger.info(f"Duplicate message ignored: id={msg.mensaje_id[:12]}...")
                return {"status": "ok"}

            # Rule 34: Media message fallback
            if msg.tipo == "media":
                prompts = _load_prompts()
                fallback = prompts.get("media_fallback_message",
                    "Lo siento, por ahora solo puedo procesar mensajes de texto. ¿En qué puedo ayudarte?")
                await proveedor.enviar_mensaje(msg.telefono, fallback)
                return {"status": "ok"}

            # Rule 27: Owner command interception
            if msg.telefono == tenant_ctx.owner_phone:
                handled = await handle_owner_command(
                    owner_phone=msg.telefono,
                    message=msg.texto,
                    tenant_ctx=tenant_ctx,
                    proveedor=proveedor,
                )
                if handled:
                    return {"status": "ok"}

            # Rule 25: Check conversation state
            state = await get_conversation_state(msg.telefono, tenant_id)
            if state == "PAUSED":
                logger.info(f"Conversation PAUSED for phone=****{phone_tail} — skipping Claude")
                return {"status": "ok"}
            if state == "CLOSED":
                return {"status": "ok"}

            # Rule 28: Out-of-hours check (saves tokens)
            if not is_within_business_hours(tenant_ctx):
                prompts = _load_prompts()
                oor_msg = prompts.get("out_of_hours_message",
                    "Nuestro horario es Lunes a Viernes de 9:00 a 20:00 y Sábados de 9:00 a 17:00.")
                await proveedor.enviar_mensaje(msg.telefono, oor_msg)
                logger.info(f"Out-of-hours auto-response sent to ****{phone_tail}")
                return {"status": "ok"}

            # Rule 31: Typing indicator
            await proveedor.send_typing(msg.telefono)

            # Retrieve conversation history
            historial = await obtener_historial(
                msg.telefono, tenant_id=tenant_id,
                limite=tenant_ctx.max_history_messages
            )

            # Generate Claude response
            respuesta, in_tok, out_tok = await generar_respuesta(
                mensaje=msg.texto,
                historial=historial,
                locale=tenant_ctx.locale,
                tenant_id=tenant_id,
                summary_threshold=tenant_ctx.summary_threshold,
            )

            # Rule 22: Log token usage for billing
            await log_usage(
                tenant_id=tenant_id,
                input_tokens=in_tok,
                output_tokens=out_tok,
                conversation_id=msg.telefono,
            )

            # Rule 24: Escalation detection
            if respuesta.startswith("[ESCALATE]"):
                clean_response = respuesta.removeprefix("[ESCALATE]").strip()
                await proveedor.enviar_mensaje(msg.telefono, clean_response)
                await handle_escalation(
                    phone=msg.telefono,
                    tenant_ctx=tenant_ctx,
                    last_message=msg.texto,
                    proveedor=proveedor,
                )
            else:
                # Persist conversation
                await guardar_mensaje(msg.telefono, "user", msg.texto, tenant_id)
                await guardar_mensaje(msg.telefono, "assistant", respuesta, tenant_id)
                await proveedor.enviar_mensaje(msg.telefono, respuesta)

            duration_ms = int((time.time() - t0) * 1000)
            logger.info(
                f"Processed message | tenant={tenant_id} | phone=****{phone_tail} | {duration_ms}ms"
            )

        return {"status": "ok"}

    except HTTPException:
        raise
    except Exception as e:
        # Rule 14: Log full error internally, return generic message to client
        logger.error(f"Webhook handler error: {e}")
        return {"status": "error", "message": "Error interno del servidor"}


# ── Admin endpoints ────────────────────────────────────────────────────────

def _verify_admin(request: Request):
    """Verify ADMIN_SECRET header or query param."""
    secret = os.getenv("ADMIN_SECRET", "")
    provided = request.headers.get("X-Admin-Secret") or request.query_params.get("secret", "")
    if not secret or provided != secret:
        raise HTTPException(status_code=401, detail="Unauthorized")


@app.post("/admin/reload-tenants")
async def reload_tenants(request: Request):
    """Rule 21: Hot-reload tenant registry without server restart."""
    _verify_admin(request)
    load_tenants()
    return {"status": "ok", "tenants": get_tenant_count()}


@app.get("/admin/usage")
async def admin_usage(request: Request, tenant_id: str = "", month: str = ""):
    """Rule 22 + 29: Token usage and escalation analytics per tenant per month."""
    _verify_admin(request)
    import sqlalchemy as sa
    from agent.memory import async_session, UsageLog, EscalationLog
    from datetime import date

    # Parse month filter
    if month:
        try:
            y, m = map(int, month.split("-"))
            from calendar import monthrange
            last_day = monthrange(y, m)[1]
            start = datetime(y, m, 1, tzinfo=timezone.utc)
            end = datetime(y, m, last_day, 23, 59, 59, tzinfo=timezone.utc)
        except Exception:
            raise HTTPException(status_code=400, detail="month must be YYYY-MM")
    else:
        now = datetime.now(timezone.utc)
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = now

    async with async_session() as session:
        # Usage query
        usage_q = sa.select(
            sa.func.sum(UsageLog.input_tokens),
            sa.func.sum(UsageLog.output_tokens),
            sa.func.count(UsageLog.id),
        ).where(UsageLog.timestamp.between(start, end))
        if tenant_id:
            usage_q = usage_q.where(UsageLog.tenant_id == tenant_id)
        usage_row = (await session.execute(usage_q)).one()

        in_tok = usage_row[0] or 0
        out_tok = usage_row[1] or 0
        calls = usage_row[2] or 0
        cost = (in_tok / 1_000_000 * 3) + (out_tok / 1_000_000 * 15)

        # Escalation analytics (rule 29)
        esc_q = sa.select(sa.func.count(EscalationLog.id)).where(
            EscalationLog.escalated_at.between(start, end)
        )
        if tenant_id:
            esc_q = esc_q.where(EscalationLog.tenant_id == tenant_id)
        esc_count = (await session.execute(esc_q)).scalar() or 0

    return {
        "tenant_id": tenant_id or "all",
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "usage": {
            "api_calls": calls,
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "estimated_cost_usd": round(cost, 4),
        },
        "escalations": {"total": esc_count},
    }
