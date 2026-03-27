# tests/test_local.py — Terminal chat simulator (no WhatsApp required)
import asyncio
import sys
import os

# Force UTF-8 output on Windows terminals (handles emojis in Claude responses)
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr.encoding != "utf-8":
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.brain import generar_respuesta
from agent.memory import inicializar_db, guardar_mensaje, obtener_historial, limpiar_historial

TELEFONO_TEST = "test-local-001"
TENANT_TEST = "salon_bella"


async def main():
    await inicializar_db()

    print()
    print("=" * 55)
    print("   AgentKit — Salón Bella · Test Local")
    print("=" * 55)
    print()
    print("  Escribí mensajes como si fueras un cliente.")
    print("  Comandos:")
    print("    'limpiar'  — borra el historial")
    print("    'salir'    — termina el test")
    print()
    print("-" * 55)
    print()

    while True:
        try:
            mensaje = input("Vos: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n\nTest finalizado.")
            break

        if not mensaje:
            continue
        if mensaje.lower() == "salir":
            print("\nTest finalizado.")
            break
        if mensaje.lower() == "limpiar":
            await limpiar_historial(TELEFONO_TEST, TENANT_TEST)
            print("[Historial borrado]\n")
            continue

        historial = await obtener_historial(TELEFONO_TEST, tenant_id=TENANT_TEST)
        print("\nBella: ", end="", flush=True)
        respuesta, in_tok, out_tok = await generar_respuesta(
            mensaje, historial, locale="es-AR", tenant_id=TENANT_TEST
        )
        print(respuesta)
        print(f"  [{in_tok} in / {out_tok} out tokens]\n")

        await guardar_mensaje(TELEFONO_TEST, "user", mensaje, TENANT_TEST)
        await guardar_mensaje(TELEFONO_TEST, "assistant", respuesta, TENANT_TEST)


if __name__ == "__main__":
    asyncio.run(main())
