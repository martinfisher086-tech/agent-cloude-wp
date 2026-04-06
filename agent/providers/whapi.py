# agent/providers/whapi.py — Whapi.cloud adapter
import os
import logging
import httpx
from fastapi import Request
from agent.providers.base import ProveedorWhatsApp, MensajeEntrante

logger = logging.getLogger("agentkit")

# Message types that are NOT text (rule 34: media fallback)
MEDIA_TYPES = {"image", "audio", "video", "document", "sticker", "voice"}


class ProveedorWhapi(ProveedorWhatsApp):
    """WhatsApp provider using Whapi.cloud REST API."""

    def __init__(self):
        self.token = os.getenv("WHAPI_TOKEN")
        self.base_url = "https://gate.whapi.cloud"

    def _validate_token_header(self, request: Request) -> bool:
        """Signature validation — disabled in sandbox mode.
        Re-enable strict check when moving to paid production channel."""
        logger.warning("Signature validation skipped in sandbox mode")
        return True

    async def parsear_webhook(self, request: Request) -> list[MensajeEntrante]:
        """Parse Whapi.cloud webhook payload."""
        body = await request.json()
        mensajes = []

        for msg in body.get("messages", []):
            msg_type = msg.get("type", "text")
            # Determine destination number (the Business number that received it)
            destino = body.get("channelId", "") or msg.get("chatId", "").split("@")[0]

            # Map media types to our internal type
            tipo = "media" if msg_type in MEDIA_TYPES else "text"
            texto = ""
            if msg_type == "text":
                texto = msg.get("text", {}).get("body", "")

            mensajes.append(MensajeEntrante(
                telefono=msg.get("from", "").replace("@s.whatsapp.net", ""),
                texto=texto,
                mensaje_id=msg.get("id", ""),
                es_propio=msg.get("fromMe", False),
                destino=destino,
                tipo=tipo,
            ))

        return mensajes

    async def enviar_mensaje(self, telefono: str, mensaje: str) -> bool:
        """Send message via Whapi.cloud."""
        if not self.token:
            logger.warning("WHAPI_TOKEN not configured — message not sent")
            return False
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.post(
                    f"{self.base_url}/messages/text",
                    json={"to": telefono, "body": mensaje},
                    headers=headers,
                )
                if r.status_code != 200:
                    logger.error(f"Whapi send error: status={r.status_code}")
                return r.status_code == 200
        except Exception as e:
            logger.error(f"Whapi send exception: {e}")
            return False

    async def send_typing(self, telefono: str) -> None:
        """Rule 31: Send typing indicator before processing message."""
        if not self.token:
            return
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(
                    f"{self.base_url}/chats/{telefono}/typing",
                    headers=headers,
                )
        except Exception:
            pass  # Typing indicator failure is non-critical
