# agent/providers/__init__.py — Provider factory
import os
from agent.providers.base import ProveedorWhatsApp


def obtener_proveedor() -> ProveedorWhatsApp:
    """Returns the WhatsApp provider configured in .env (WHATSAPP_PROVIDER)."""
    proveedor = os.getenv("WHATSAPP_PROVIDER", "whapi").lower()

    if proveedor == "whapi":
        from agent.providers.whapi import ProveedorWhapi
        return ProveedorWhapi()
    elif proveedor == "meta":
        from agent.providers.meta import ProveedorMeta
        return ProveedorMeta()
    elif proveedor == "twilio":
        from agent.providers.twilio import ProveedorTwilio
        return ProveedorTwilio()
    else:
        raise ValueError(f"Proveedor no soportado: {proveedor}. Opciones: whapi, meta, twilio")
