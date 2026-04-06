# agent/tenant.py — Tenant registry loader and TenantContext (rules 17-21)
import os
import signal
import logging
from typing import Optional
from dataclasses import dataclass
from zoneinfo import ZoneInfo
from datetime import datetime
import yaml

logger = logging.getLogger("agentkit")

_tenant_registry: dict = {}  # phone_number -> tenant config


@dataclass
class TenantContext:
    """Rule 19: Passed through entire request lifecycle. No global state."""
    tenant_id: str
    name: str
    locale: str
    db_url: str
    owner_phone: str
    business_hours: dict
    escalation_cooldown_minutes: int
    max_history_messages: int
    summary_threshold: int
    prompts_file: str
    knowledge_dir: str


def load_tenants(config_path: str | None = None) -> None:
    """Rule 17: Load tenant registry from YAML. Called at startup and on hot-reload."""
    global _tenant_registry
    path = config_path or os.getenv("TENANTS_CONFIG_PATH", "config/tenants.yaml")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        _tenant_registry = data.get("tenants", {})
        logger.info(f"Loaded {len(_tenant_registry)} tenant(s) from {path}")
    except FileNotFoundError:
        logger.warning(f"Tenant config not found at {path} — running with 0 tenants")
        _tenant_registry = {}
    except Exception as e:
        logger.error(f"Failed to load tenant config: {e}")


def get_tenant_context(destination_phone: str) -> TenantContext | None:
    """
    Rule 17: Look up tenant by destination WhatsApp Business number.
    Rule 20: Returns None if unknown tenant — caller must return HTTP 200 without processing.
    """
    config = _tenant_registry.get(destination_phone)
    if not config:
        # Try without country code prefix variations
        for key, val in _tenant_registry.items():
            if destination_phone.endswith(key.lstrip("+")) or key.endswith(destination_phone.lstrip("+")):
                config = val
                break

    if not config:
        logger.warning(f"Unknown tenant for destination={destination_phone} — ignoring message")
        return None

    return TenantContext(
        tenant_id=config.get("id", destination_phone),
        name=config.get("name", "Unknown"),
        locale=config.get("locale", "es-AR"),
        db_url=config.get("db_url", "sqlite+aiosqlite:///data/default.db"),
        owner_phone=config.get("owner_phone", ""),
        business_hours=config.get("business_hours", {}),
        escalation_cooldown_minutes=config.get("escalation_cooldown_minutes", 30),
        max_history_messages=config.get("max_history_messages", 20),
        summary_threshold=config.get("summary_threshold", 40),
        prompts_file=config.get("prompts_file", "config/prompts.yaml"),
        knowledge_dir=config.get("knowledge_dir", "knowledge/"),
    )


def get_tenant_by_token(token: str) -> TenantContext | None:
    """
    Fallback lookup por whapi_token cuando destino viene vacío (sandbox de Whapi).
    Itera el registro buscando el tenant cuyo whapi_token coincida con el token dado.
    """
    if not token:
        return None
    for config in _tenant_registry.values():
        if config.get("whapi_token") == token:
            return TenantContext(
                tenant_id=config.get("id", ""),
                name=config.get("name", "Unknown"),
                locale=config.get("locale", "es-AR"),
                db_url=config.get("db_url", "sqlite+aiosqlite:///data/default.db"),
                owner_phone=config.get("owner_phone", ""),
                business_hours=config.get("business_hours", {}),
                escalation_cooldown_minutes=config.get("escalation_cooldown_minutes", 30),
                max_history_messages=config.get("max_history_messages", 20),
                summary_threshold=config.get("summary_threshold", 40),
                prompts_file=config.get("prompts_file", "config/prompts.yaml"),
                knowledge_dir=config.get("knowledge_dir", "knowledge/"),
            )
    return None


def get_tenant_count() -> int:
    return len(_tenant_registry)


def is_within_business_hours(tenant_ctx: TenantContext) -> bool:
    """Rule 28 + 38: Check if current time is within tenant's configured business hours."""
    bh = tenant_ctx.business_hours
    if not bh:
        return True  # No hours configured → always open

    tz_name = bh.get("timezone", "America/Argentina/Buenos_Aires")
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")

    now = datetime.now(tz)
    weekday = now.isoweekday()  # 1=Monday ... 7=Sunday

    open_days = bh.get("days", [1, 2, 3, 4, 5])

    if weekday not in open_days:
        return False

    try:
        start_h, start_m = map(int, bh.get("start", "09:00").split(":"))
        end_h, end_m = map(int, bh.get("end", "20:00").split(":"))
    except ValueError:
        return True

    start_minutes = start_h * 60 + start_m
    end_minutes = end_h * 60 + end_m
    now_minutes = now.hour * 60 + now.minute

    return start_minutes <= now_minutes < end_minutes


def setup_sighup_handler():
    """Rule 21: Reload tenants on SIGHUP (Linux/Mac). Not supported on Windows."""
    try:
        signal.signal(signal.SIGHUP, lambda sig, frame: load_tenants())
        logger.info("SIGHUP handler registered for tenant hot-reload")
    except (AttributeError, OSError):
        logger.info("SIGHUP not available on this OS — use /admin/reload-tenants instead")
