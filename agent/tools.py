# agent/tools.py — Business tools for Salón Bella
# Appointment persistence: Google Sheets (primary) + SQLite (fallback)
from __future__ import annotations
import os
import uuid
import logging
import asyncio
import unicodedata
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo
import yaml

_TZ_BA = ZoneInfo("America/Argentina/Buenos_Aires")

logger = logging.getLogger("agentkit")

# ── Service catalog ────────────────────────────────────────────────────────

SERVICIOS = {
    "Corte de cabello":        {"duracion": 45,  "precio": 4500},
    "Coloración completa":     {"duracion": 120, "precio": 9500},
    "Balayage / Mechas":       {"duracion": 180, "precio": 14000},
    "Manicuría / Nail Art":    {"duracion": 60,  "precio": 5500},
    "Limpieza facial profunda":{"duracion": 90,  "precio": 5500},
    "Peinado social":          {"duracion": 60,  "precio": 6000},
}

SINONIMOS = {
    "manicure": "Manicuría / Nail Art",
    "manicura": "Manicuría / Nail Art",
    "uñas": "Manicuría / Nail Art",
    "nail art": "Manicuría / Nail Art",
    "mechas": "Balayage / Mechas",
    "balayage": "Balayage / Mechas",
    "iluminación": "Balayage / Mechas",
    "corte": "Corte de cabello",
    "pelo": "Corte de cabello",
    "coloracion": "Coloración completa",
    "coloración": "Coloración completa",
    "tinte": "Coloración completa",
    "color": "Coloración completa",
    "facial": "Limpieza facial profunda",
    "limpieza": "Limpieza facial profunda",
    "peinado": "Peinado social",
    "evento": "Peinado social",
    "fiesta": "Peinado social",
}

# Sheet column order (must match the actual spreadsheet header row)
SHEET_COLUMNS = ["ID", "Fecha", "Hora", "Servicio", "Duracion",
                 "Cliente", "Telefono", "Estado", "Agendado_en", "Tenant",
                 "Confirmado_por_cliente", "Notas"]


def _strip_accents(text: str) -> str:
    """Normalize text: lowercase + remove diacritics for accent-insensitive comparison."""
    nfkd = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in nfkd if not unicodedata.combining(c))


# ── SQLite backup table ────────────────────────────────────────────────────

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import String, Integer, select, update as sa_update, text

_backup_engine = None
_backup_session = None


def _get_backup_session():
    global _backup_engine, _backup_session
    if _backup_session is None:
        db_url = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./agentkit.db")
        _backup_engine = create_async_engine(db_url, echo=False)
        _backup_session = async_sessionmaker(
            _backup_engine, class_=AsyncSession, expire_on_commit=False
        )
    return _backup_session


class _BackupBase(DeclarativeBase):
    pass


class TurnoBackup(_BackupBase):
    """Local SQLite copy of every appointment (fallback when Sheets is unavailable)."""
    __tablename__ = "turnos_backup"

    id: Mapped[str] = mapped_column(String(50), primary_key=True)
    fecha: Mapped[str] = mapped_column(String(20))
    hora: Mapped[str] = mapped_column(String(10))
    servicio: Mapped[str] = mapped_column(String(100))
    duracion: Mapped[int] = mapped_column(Integer)
    cliente: Mapped[str] = mapped_column(String(200))
    telefono: Mapped[str] = mapped_column(String(50))
    estado: Mapped[str] = mapped_column(String(20), default="CONFIRMADO")
    agendado_en: Mapped[str] = mapped_column(String(30))
    tenant: Mapped[str] = mapped_column(String(100))
    confirmado_por_cliente: Mapped[str] = mapped_column(String(5), default="SI")
    notas: Mapped[str] = mapped_column(String(500), default="")
    sheets_synced: Mapped[int] = mapped_column(Integer, default=0)  # 0=pending, 1=synced


class TenantCounter(_BackupBase):
    """Per-tenant autoincrement counter for human-readable appointment IDs (TRN-001, TRN-002...)."""
    __tablename__ = "tenant_counters"

    tenant: Mapped[str] = mapped_column(String(100), primary_key=True)
    last_seq: Mapped[int] = mapped_column(Integer, default=0)


async def _init_backup_table():
    session_factory = _get_backup_session()
    async with _backup_engine.begin() as conn:
        await conn.run_sync(_BackupBase.metadata.create_all)
        # Additive migrations for columns added after initial deploy.
        # ALTER TABLE ADD COLUMN is idempotent-safe: we catch the error if already exists.
        for stmt in [
            "ALTER TABLE turnos_backup ADD COLUMN confirmado_por_cliente TEXT NOT NULL DEFAULT 'SI'",
            "ALTER TABLE turnos_backup ADD COLUMN notas TEXT NOT NULL DEFAULT ''",
        ]:
            try:
                await conn.execute(text(stmt))
            except Exception:
                pass  # column already exists — nothing to do


async def _next_turno_id(tenant: str) -> str:
    """
    Return the next sequential appointment ID for a tenant (e.g. TRN-001, TRN-002...).
    The counter persists in SQLite so it never resets on server restart.
    Thread-safe: wrapped in a transaction so concurrent requests don't collide.
    """
    await _init_backup_table()
    session_factory = _get_backup_session()
    async with session_factory() as session:
        async with session.begin():
            result = await session.execute(
                select(TenantCounter).where(TenantCounter.tenant == tenant)
            )
            row = result.scalar_one_or_none()
            if row is None:
                row = TenantCounter(tenant=tenant, last_seq=1)
                session.add(row)
                seq = 1
            else:
                row.last_seq += 1
                seq = row.last_seq
        # transaction committed here
    return f"TRN-{seq:03d}"


async def _save_backup(turno: dict) -> None:
    await _init_backup_table()
    session_factory = _get_backup_session()
    async with session_factory() as session:
        row = TurnoBackup(
            id=turno["id"],
            fecha=turno["fecha"],
            hora=turno["hora"],
            servicio=turno["servicio"],
            duracion=turno["duracion"],
            cliente=turno["cliente"],
            telefono=turno["telefono"],
            estado=turno.get("estado", "CONFIRMADO"),
            agendado_en=turno["agendado_en"],
            tenant=turno.get("tenant", ""),
            confirmado_por_cliente=turno.get("confirmado_por_cliente", "SI"),
            notas=turno.get("notas", ""),
            sheets_synced=turno.get("_sheets_synced", 0),
        )
        session.add(row)
        await session.commit()


async def _get_backup_turnos_del_dia(fecha: str, tenant: str) -> list[dict]:
    await _init_backup_table()
    session_factory = _get_backup_session()
    async with session_factory() as session:
        result = await session.execute(
            select(TurnoBackup)
            .where(TurnoBackup.fecha == fecha)
            .where(TurnoBackup.tenant == tenant)
            .where(TurnoBackup.estado != "CANCELADO")
        )
        rows = result.scalars().all()
        return [
            {"id": r.id, "fecha": r.fecha, "hora": r.hora,
             "servicio": r.servicio, "duracion": r.duracion,
             "cliente": r.cliente, "telefono": r.telefono,
             "estado": r.estado, "tenant": r.tenant}
            for r in rows
        ]


async def _cancel_backup(turno_id: str) -> bool:
    await _init_backup_table()
    session_factory = _get_backup_session()
    async with session_factory() as session:
        await session.execute(
            sa_update(TurnoBackup)
            .where(TurnoBackup.id == turno_id)
            .values(estado="CANCELADO")
        )
        await session.commit()
    return True


# ── Google Sheets adapter ──────────────────────────────────────────────────

class GoogleSheetsAdapter:
    """
    Primary appointment persistence via Google Sheets.
    Falls back to SQLite on any Sheets error (rule 14 / rule 32 pattern).

    Sheet header (row 1) must be exactly:
    ID | Fecha | Hora | Servicio | Duracion | Cliente | Telefono | Estado | Agendado_en | Tenant
    """

    def __init__(self):
        self._client = None   # gspread client (lazy-initialized)
        self._sheet = None    # gspread worksheet handle

    def _get_sheet(self):
        """Lazy-initialize gspread client, open worksheet, and ensure header row exists."""
        if self._sheet is not None:
            return self._sheet

        creds_path = os.getenv("GOOGLE_CREDENTIALS_JSON", "config/google_credentials.json")
        sheet_id   = os.getenv("GOOGLE_SHEET_ID", "")

        if not sheet_id:
            raise RuntimeError("GOOGLE_SHEET_ID not configured in .env")
        if not os.path.exists(creds_path):
            raise FileNotFoundError(f"Google credentials not found at: {creds_path}")

        import gspread
        from google.oauth2.service_account import Credentials

        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive.readonly",
        ]
        creds = Credentials.from_service_account_file(creds_path, scopes=scopes)
        gc = gspread.authorize(creds)
        spreadsheet = gc.open_by_key(sheet_id)
        sheet = spreadsheet.sheet1

        # Ensure row 1 is the header — never insert, only write if needed.
        existing = sheet.row_values(1)
        if not existing:
            # Sheet is completely empty — write headers
            sheet.append_row(SHEET_COLUMNS)
            logger.info("Sheet header row created (sheet was empty)")
        elif existing != SHEET_COLUMNS:
            # Headers are outdated (e.g. new columns added) — update in place
            sheet.update("A1", [SHEET_COLUMNS])
            logger.info("Sheet header row updated in place")
        # else: headers are correct — nothing to do

        self._sheet = sheet
        return self._sheet

    def _build_row(self, turno: dict) -> list:
        """Convert a turno dict to a sheet row matching SHEET_COLUMNS order."""
        return [
            turno.get("id", ""),
            turno.get("fecha", ""),
            turno.get("hora", ""),
            turno.get("servicio", ""),
            turno.get("duracion", ""),
            turno.get("cliente", ""),
            turno.get("telefono", ""),
            turno.get("estado", "CONFIRMADO"),
            turno.get("agendado_en", ""),
            turno.get("tenant", ""),
            turno.get("confirmado_por_cliente", "SI"),
            turno.get("notas", ""),
        ]

    async def escribir_turno(self, turno: dict) -> bool:
        """
        Append a new appointment row to Google Sheets.
        Falls back to SQLite-only if Sheets is unavailable.
        Returns True if written to Sheets, False if fallback-only.
        """
        # Always write backup first so it's never lost
        turno.setdefault("id", str(uuid.uuid4())[:8].upper())
        turno.setdefault("agendado_en", datetime.now(timezone.utc).isoformat())

        try:
            sheet = await asyncio.to_thread(self._get_sheet)
            row = self._build_row(turno)
            await asyncio.to_thread(sheet.append_row, row, value_input_option="USER_ENTERED")
            turno["_sheets_synced"] = 1
            logger.info(f"Appointment written to Sheets: id={turno['id']} tenant={turno.get('tenant')}")
            await _save_backup(turno)
            return True

        except Exception as e:
            # Rule 14: log full error internally, don't expose to client
            logger.error(f"Google Sheets write failed (falling back to SQLite): {e}")
            turno["_sheets_synced"] = 0
            await _save_backup(turno)
            return False

    async def leer_turnos_del_dia(self, fecha: str, tenant: str = "") -> list[dict]:
        """
        Read all appointments for a given date from Google Sheets (source of truth).
        Falls back to SQLite if Sheets is unavailable.
        fecha format: DD/MM/YYYY
        """
        try:
            sheet = await asyncio.to_thread(self._get_sheet)
            all_rows = await asyncio.to_thread(sheet.get_all_records)

            turnos = [
                r for r in all_rows
                if r.get("Fecha") == fecha
                and r.get("Estado") != "CANCELADO"
                and (not tenant or r.get("Tenant") == tenant)
            ]
            logger.info(f"Sheets: {len(turnos)} appointments found for {fecha}")
            return [
                {
                    "id":       r["ID"],
                    "fecha":    r["Fecha"],
                    "hora":     r["Hora"],
                    "servicio": r["Servicio"],
                    "duracion": r["Duracion"],
                    "cliente":  r["Cliente"],
                    "telefono": r["Telefono"],
                    "estado":   r["Estado"],
                    "tenant":   r.get("Tenant", ""),
                }
                for r in turnos
            ]

        except Exception as e:
            logger.error(f"Google Sheets read failed (falling back to SQLite): {e}")
            return await _get_backup_turnos_del_dia(fecha, tenant)

    async def cancelar_turno(self, turno_id: str) -> bool:
        """
        Mark an appointment as CANCELADO in Google Sheets.
        Also cancels in SQLite backup.
        Returns True on success, False if Sheets update failed.
        """
        sheets_ok = False
        try:
            sheet = await asyncio.to_thread(self._get_sheet)
            # Find the row with matching ID
            cell = await asyncio.to_thread(sheet.find, turno_id)
            if cell is None:
                logger.warning(f"cancelar_turno: ID {turno_id} not found in Sheets")
            else:
                estado_col = SHEET_COLUMNS.index("Estado") + 1  # 1-indexed
                await asyncio.to_thread(sheet.update_cell, cell.row, estado_col, "CANCELADO")
                logger.info(f"Appointment {turno_id} cancelled in Sheets")
                sheets_ok = True

        except Exception as e:
            logger.error(f"Google Sheets cancel failed: {e}")

        # Always update backup regardless of Sheets result
        await _cancel_backup(turno_id)
        return sheets_ok


# ── Module-level singleton ─────────────────────────────────────────────────

_sheets_adapter: Optional[GoogleSheetsAdapter] = None


def get_sheets_adapter() -> GoogleSheetsAdapter:
    """Return the module-level GoogleSheetsAdapter singleton."""
    global _sheets_adapter
    if _sheets_adapter is None:
        _sheets_adapter = GoogleSheetsAdapter()
    return _sheets_adapter


# ── Other tools ────────────────────────────────────────────────────────────

def cargar_info_negocio() -> dict:
    try:
        with open("config/business.yaml", "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        logger.error("config/business.yaml not found")
        return {}


# ── Helper: build turno dict from conversation data ────────────────────────

def _resolver_servicio(servicio: str) -> tuple[str, dict]:
    """
    Resolve a service name to its canonical name and info dict.
    Uses accent-insensitive, case-insensitive matching with synonym fallback.
    Returns (canonical_name, info_dict).
    """
    # 1. Exact match
    if servicio in SERVICIOS:
        return servicio, SERVICIOS[servicio]

    texto_norm = _strip_accents(servicio)

    # 2. Accent-insensitive match against catalog keys
    for nombre, info in SERVICIOS.items():
        if _strip_accents(nombre) == texto_norm:
            return nombre, info

    # 3. Partial accent-insensitive match
    for nombre, info in SERVICIOS.items():
        if _strip_accents(nombre) in texto_norm or texto_norm in _strip_accents(nombre):
            return nombre, info

    # 4. Synonym lookup (accent-insensitive)
    for sinonimo, oficial in SINONIMOS.items():
        if _strip_accents(sinonimo) in texto_norm:
            return oficial, SERVICIOS.get(oficial, {})

    # 5. Fallback: try prompts.yaml for a duration hint
    try:
        config = cargar_info_negocio()  # reuse existing loader
    except Exception:
        config = {}
    fallback_duration = 60  # sane default
    servicios_yaml = config.get("servicios", {})
    if isinstance(servicios_yaml, dict):
        for k, v in servicios_yaml.items():
            if _strip_accents(k) in texto_norm or texto_norm in _strip_accents(k):
                fallback_duration = v.get("duracion", fallback_duration)
                break

    logger.warning(f"Service '{servicio}' not found in catalog — using fallback duration {fallback_duration}min")
    return servicio, {"duracion": fallback_duration, "precio": 0}


async def construir_turno(
    servicio: str,
    fecha: str,
    hora: str,
    cliente: str,
    telefono: str,
    tenant: str = "",
    notas: str = "",
) -> dict:
    """
    Build a normalized appointment dict ready for escribir_turno().
    fecha: DD/MM/YYYY  |  hora: HH:MM
    ID format: TRN-001, TRN-002... (persistent sequential counter per tenant).
    Agendado_en is formatted as DD/MM/YYYY HH:MMhs (Buenos Aires timezone).
    """
    canonical, info = _resolver_servicio(servicio)
    turno_id = await _next_turno_id(tenant or "default")
    now_ba = datetime.now(_TZ_BA)
    agendado_en = now_ba.strftime("%d/%m/%Y %H:%Mhs")
    return {
        "id":                    turno_id,
        "fecha":                 fecha,
        "hora":                  hora,
        "servicio":              canonical,
        "duracion":              info.get("duracion", 60),
        "cliente":               cliente,
        "telefono":              telefono,
        "estado":                "CONFIRMADO",
        "agendado_en":           agendado_en,
        "tenant":                tenant,
        "confirmado_por_cliente": "SI",
        "notas":                 notas,
    }


def normalizar_servicio(texto: str) -> Optional[str]:
    """Map user input to official service name via exact match then synonyms (accent-insensitive)."""
    texto_norm = _strip_accents(texto.strip())
    for nombre in SERVICIOS:
        if _strip_accents(nombre) in texto_norm:
            return nombre
    for sinonimo, oficial in SINONIMOS.items():
        if _strip_accents(sinonimo) in texto_norm:
            return oficial
    return None


def buscar_en_knowledge(consulta: str) -> str:
    resultados = []
    knowledge_dir = "knowledge"
    if not os.path.exists(knowledge_dir):
        return "No hay archivos de conocimiento disponibles."
    for archivo in os.listdir(knowledge_dir):
        ruta = os.path.join(knowledge_dir, archivo)
        if archivo.startswith(".") or not os.path.isfile(ruta):
            continue
        try:
            with open(ruta, "r", encoding="utf-8") as f:
                contenido = f.read()
                if consulta.lower() in contenido.lower():
                    resultados.append(f"[{archivo}]: {contenido[:500]}")
        except (UnicodeDecodeError, IOError):
            continue
    return "\n---\n".join(resultados) if resultados else "No encontré información específica sobre eso."
