# agent/providers/base.py — Abstract base class for WhatsApp providers
from abc import ABC, abstractmethod
from dataclasses import dataclass
from fastapi import Request


@dataclass
class MensajeEntrante:
    """Normalized inbound message — same format regardless of provider."""
    telefono: str       # Sender phone number
    texto: str          # Message text content
    mensaje_id: str     # Unique message ID (for deduplication)
    es_propio: bool     # True if sent by the agent itself (skip processing)
    destino: str = ""   # Destination WhatsApp Business number (tenant_id key)
    tipo: str = "text"  # Message type: text | image | audio | document | sticker


class ProveedorWhatsApp(ABC):
    """Interface that every WhatsApp provider must implement."""

    @abstractmethod
    async def parsear_webhook(self, request: Request) -> list[MensajeEntrante]:
        """Extract and normalize messages from the webhook payload."""
        ...

    @abstractmethod
    async def enviar_mensaje(self, telefono: str, mensaje: str) -> bool:
        """Send a text message. Returns True on success."""
        ...

    async def send_typing(self, telefono: str) -> None:
        """Send typing indicator (no-op by default, implemented where supported)."""
        pass

    async def validar_webhook(self, request: Request) -> dict | int | None:
        """GET webhook verification (only Meta requires it). Returns response or None."""
        return None
