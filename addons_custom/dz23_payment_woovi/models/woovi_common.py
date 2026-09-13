# Regras puras do PIX Woovi (sem banco): normalização de status, ordem monotônica,
# valores em centavos e extração do valor efetivamente recebido.
from datetime import UTC, datetime

from odoo.tools import float_round

WOOVI_STATUSES = [
    ("active", "Ativa"),
    ("pending", "Pendente"),
    ("completed", "Paga"),
    ("expired", "Expirada"),
    ("cancelled", "Cancelada"),
    ("refunded", "Estornada"),
    ("error", "Erro"),
]
_STATUS_MAP = {
    "ACTIVE": "active",
    "PENDING": "pending",
    "COMPLETED": "completed",
    "PAID": "completed",
    "CONFIRMED": "completed",
    "EXPIRED": "expired",
    "CANCELLED": "cancelled",
    "CANCELED": "cancelled",
    "REFUNDED": "refunded",
}
_EVENT_STATUS = {
    "OPENPIX:CHARGE_CREATED": "active",
    "OPENPIX:CHARGE_COMPLETED": "completed",
    "OPENPIX:CHARGE_EXPIRED": "expired",
    "OPENPIX:TRANSACTION_REFUND_RECEIVED": "refunded",
}
# Ordem monotônica: um evento atrasado não regride (ex.: EXPIRED depois de COMPLETED).
STATUS_RANK = {
    "active": 10,
    "pending": 10,
    "error": 15,
    "expired": 20,
    "cancelled": 20,
    "completed": 30,
    "refunded": 40,
}
REUSABLE_STATUSES = ("active", "pending")


class WooviEventError(Exception):
    """Evento válido que não pode ser aplicado por regra de negócio (ex.: sem valor)."""


def to_cents(amount):
    return int(float_round((amount or 0.0) * 100, precision_digits=0))


def normalize_status(payment_data):
    data = payment_data or {}
    charge = data.get("charge") or {}
    raw = str(charge.get("status") or "").strip().upper()
    return _STATUS_MAP.get(raw) or _EVENT_STATUS.get(str(data.get("event") or "").upper())


def _as_cents(value):
    if value in (None, ""):
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def paid_cents(payment_data):
    """Valor RECEBIDO em centavos: `pix.value` (transação) e, na falta, `charge.value`
    (a Woovi só conclui a cobrança paga integralmente). None se ausente."""
    data = payment_data or {}
    pix = data.get("pix")
    if isinstance(pix, list):
        pix = pix[0] if pix else {}
    received = _as_cents((pix or {}).get("value"))
    if received is not None:
        return received
    return _as_cents((data.get("charge") or {}).get("value"))


def payment_currency(payment_data):
    data = payment_data or {}
    charge = data.get("charge") or {}
    return str(charge.get("currency") or data.get("currency") or "BRL").strip().upper()


def parse_datetime(value):
    """ISO-8601 da Woovi -> datetime UTC naive (ou False)."""
    if not value:
        return False
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed
