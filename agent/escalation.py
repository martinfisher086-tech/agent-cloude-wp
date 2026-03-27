# agent/escalation.py — Human escalation orchestration (rules 23-29)
import logging
from datetime import datetime, timedelta, timezone

from agent.memory import (
    set_conversation_state, get_conversation_state,
    log_escalation, obtener_historial,
)

logger = logging.getLogger("agentkit")


async def handle_escalation(
    phone: str,
    tenant_ctx,
    last_message: str,
    proveedor,
) -> None:
    """
    Rule 24: Orchestrate escalation when Claude returns [ESCALATE].
    a) Pause the agent for this conversation
    b) Notify owner (if not in cooldown)
    c) Log the event
    """
    from agent.memory import EscalationLog
    import sqlalchemy as sa
    from agent.memory import async_session

    now = datetime.now(timezone.utc)

    # Rule 24e: Check cooldown — was this phone escalated recently?
    async with async_session() as session:
        cutoff = now - timedelta(minutes=tenant_ctx.escalation_cooldown_minutes)
        result = await session.execute(
            sa.select(EscalationLog)
            .where(EscalationLog.phone == phone)
            .where(EscalationLog.tenant_id == tenant_ctx.tenant_id)
            .where(EscalationLog.escalated_at >= cutoff)
            .order_by(EscalationLog.escalated_at.desc())
        )
        recent = result.scalars().first()
        within_cooldown = recent is not None

    # a) Always pause the agent
    await set_conversation_state(
        phone=phone,
        tenant_id=tenant_ctx.tenant_id,
        state="PAUSED",
        reason="auto_escalation",
    )
    logger.info(f"Agent PAUSED for phone={phone[-4:]}**** tenant={tenant_ctx.tenant_id}")

    # b) Notify owner only if not in cooldown
    if not within_cooldown and tenant_ctx.owner_phone:
        history = await obtener_historial(
            phone, tenant_id=tenant_ctx.tenant_id, limite=6
        )
        await notify_owner(
            owner_phone=tenant_ctx.owner_phone,
            tenant_name=tenant_ctx.name,
            customer_phone=phone,
            reason="El cliente solicitó atención humana",
            history=history,
            proveedor=proveedor,
        )
    elif within_cooldown:
        logger.info(f"Owner notification skipped (cooldown active) for phone={phone[-4:]}****")

    # c) Log the escalation event
    await log_escalation(phone=phone, tenant_id=tenant_ctx.tenant_id, reason="auto_escalation")


async def notify_owner(
    owner_phone: str,
    tenant_name: str,
    customer_phone: str,
    reason: str,
    history: list[dict],
    proveedor,
) -> None:
    """Rule 26: Send escalation notification to the business owner."""
    # Build last 3 messages preview
    last_msgs = history[-3:] if len(history) >= 3 else history
    msgs_text = "\n".join(
        f"> {'Cliente' if m['role'] == 'user' else 'Agente'}: {m['content'][:120]}"
        for m in last_msgs
    )
    # Mask phone for display (show last 4 digits only — rule 36)
    masked = f"+54911****{customer_phone[-4:]}" if len(customer_phone) >= 4 else customer_phone

    message = (
        f"⚡ Escalamiento — {tenant_name}\n"
        f"Cliente: {masked}\n"
        f"Motivo: {reason}\n\n"
        f"Últimos mensajes:\n{msgs_text}\n\n"
        f"Para reactivar el agente:\n/resume {customer_phone}\n\n"
        f"Para cerrar la conversación:\n/close {customer_phone}"
    )

    success = await proveedor.enviar_mensaje(owner_phone, message)
    if success:
        logger.info(f"Owner notified for escalation tenant={tenant_name}")
    else:
        logger.error(f"Failed to notify owner for escalation tenant={tenant_name}")


async def handle_owner_command(
    owner_phone: str,
    message: str,
    tenant_ctx,
    proveedor,
) -> bool:
    """
    Rule 27: Intercept owner commands before routing to Claude.
    Returns True if message was a command (consumed), False otherwise.
    """
    text = message.strip()

    if text.startswith("/resume "):
        target = text.split(" ", 1)[1].strip()
        await set_conversation_state(target, tenant_ctx.tenant_id, "ACTIVE")
        await proveedor.enviar_mensaje(owner_phone, f"✅ Agente reactivado para {target}")
        logger.info(f"Owner resumed agent for {target[-4:]}****")
        return True

    if text.startswith("/close "):
        target = text.split(" ", 1)[1].strip()
        await set_conversation_state(target, tenant_ctx.tenant_id, "CLOSED")
        # Send closing message to customer
        closing = "Gracias por comunicarte con nosotros. ¡Hasta pronto! 😊"
        await proveedor.enviar_mensaje(target, closing)
        await proveedor.enviar_mensaje(owner_phone, f"✅ Conversación cerrada para {target}")
        return True

    if text.startswith("/pause "):
        target = text.split(" ", 1)[1].strip()
        await set_conversation_state(target, tenant_ctx.tenant_id, "PAUSED", reason="manual")
        await proveedor.enviar_mensaje(owner_phone, f"⏸️ Agente pausado para {target}")
        return True

    if text == "/status":
        # Rule 27: count PAUSED conversations
        import sqlalchemy as sa
        from agent.memory import async_session, ConversationState
        async with async_session() as session:
            result = await session.execute(
                sa.select(ConversationState)
                .where(ConversationState.tenant_id == tenant_ctx.tenant_id)
                .where(ConversationState.state == "PAUSED")
            )
            paused = result.scalars().all()
        if paused:
            lines = "\n".join(f"• {p.phone[-4:]}**** — pausado: {p.paused_reason}" for p in paused)
            reply = f"📊 Estado — {tenant_ctx.name}\n{len(paused)} conv. en espera:\n{lines}"
        else:
            reply = f"📊 Estado — {tenant_ctx.name}\nSin conversaciones en espera. ✅"
        await proveedor.enviar_mensaje(owner_phone, reply)
        return True

    if text == "/usage":
        # Quick token summary for the owner (rule 27)
        import sqlalchemy as sa
        from agent.memory import async_session, UsageLog
        from datetime import date
        month_start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        async with async_session() as session:
            result = await session.execute(
                sa.select(
                    sa.func.sum(UsageLog.input_tokens),
                    sa.func.sum(UsageLog.output_tokens),
                )
                .where(UsageLog.tenant_id == tenant_ctx.tenant_id)
                .where(UsageLog.timestamp >= month_start)
            )
            row = result.one()
            in_tok = row[0] or 0
            out_tok = row[1] or 0
        cost = (in_tok / 1_000_000 * 3) + (out_tok / 1_000_000 * 15)
        reply = (
            f"📈 Uso este mes — {tenant_ctx.name}\n"
            f"Tokens entrada: {in_tok:,}\n"
            f"Tokens salida: {out_tok:,}\n"
            f"Costo estimado: USD ${cost:.4f}"
        )
        await proveedor.enviar_mensaje(owner_phone, reply)
        return True

    return False
