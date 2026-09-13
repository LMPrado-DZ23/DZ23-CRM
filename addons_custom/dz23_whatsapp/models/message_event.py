# Eventos de mensagem (append-only): um registro por callback de status/mensagem
# recebido de um provedor, já NORMALIZADO. Alimenta o ciclo de vida monotônico da
# outbox (ADR-006) e serve de trilha de auditoria. Callback repetido é deduplicado.
import logging

import psycopg2
from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.tools.translate import _

from .queue_utils import payload_digest, payload_json, payload_preview, sanitize_error

_logger = logging.getLogger(__name__)

MESSAGE_STATUSES = [
    ("queued", "Na fila"),
    ("sent", "Enviada"),
    ("delivered", "Entregue"),
    ("read", "Lida"),
    ("failed", "Falhou"),
    ("undelivered", "Não entregue"),
    ("expired", "Expirada"),
    ("cancelled", "Cancelada"),
    ("unknown", "Desconhecido"),
]
# Rank para monotonia: só avança quando o novo rank é MAIOR que o atual.
# Falhas (25) valem antes da entrega; depois de delivered (30) só registram erro.
STATUS_RANK = {
    "unknown": 0,
    "queued": 10,
    "sent": 20,
    "failed": 25,
    "undelivered": 25,
    "expired": 25,
    "cancelled": 25,
    "delivered": 30,
    "read": 40,
}
DIRECTIONS = ("inbound", "outbound")

_PROVIDER_STATUS_MAP = {
    "meta_cloud": {
        "sent": "sent",
        "delivered": "delivered",
        "read": "read",
        "failed": "failed",
        "deleted": "cancelled",
    },
    "twilio": {
        "accepted": "queued",
        "scheduled": "queued",
        "queued": "queued",
        "sending": "queued",
        "sent": "sent",
        "delivered": "delivered",
        "read": "read",
        "failed": "failed",
        "undelivered": "undelivered",
        "canceled": "cancelled",
    },
    "evolution": {
        "error": "failed",
        "pending": "queued",
        "server_ack": "sent",
        "delivery_ack": "delivered",
        "read": "read",
        "played": "read",
        "deleted": "cancelled",
        "0": "failed",
        "1": "queued",
        "2": "sent",
        "3": "delivered",
        "4": "read",
        "5": "read",
    },
}


def normalize_status(provider, raw_status):
    """Status do provedor -> status normalizado (desconhecido => 'unknown')."""
    key = str(raw_status if raw_status is not None else "").strip().lower()
    return _PROVIDER_STATUS_MAP.get(provider, {}).get(key, "unknown")


class DZ23MessageEvent(models.Model):
    _name = "dz23.message.event"
    _description = "DZ23 — Evento de mensagem (status/entrega, append-only)"
    _order = "occurred_at desc, id desc"
    _rec_name = "provider_message_id"

    # Campos que podem mudar após a criação (vínculo/processamento); o resto é fato.
    _MUTABLE_FIELDS = frozenset({"processed", "outbox_id", "inbox_id"})

    channel_id = fields.Many2one(
        "dz23.channel", required=True, ondelete="cascade", index=True, readonly=True
    )
    company_id = fields.Many2one(
        related="channel_id.company_id", store=True, index=True, readonly=True
    )
    provider = fields.Char(required=True, readonly=True)
    provider_message_id = fields.Char(required=True, index=True, readonly=True)
    message_direction = fields.Selection(
        [("inbound", "Recebida"), ("outbound", "Enviada")], required=True, readonly=True
    )
    status = fields.Selection(MESSAGE_STATUSES, required=True, index=True, readonly=True)
    provider_status = fields.Char(readonly=True, help="Status original do provedor.")
    occurred_at = fields.Datetime(readonly=True, help="Quando o provedor diz que ocorreu.")
    received_at = fields.Datetime(default=fields.Datetime.now, readonly=True)
    payload_hash = fields.Char(readonly=True)
    payload_preview = fields.Text(readonly=True)
    error_code = fields.Char(readonly=True)
    error_message = fields.Char(readonly=True)
    outbox_id = fields.Many2one("dz23.message.outbox", ondelete="set null", readonly=True)
    inbox_id = fields.Many2one("dz23.message.inbox", ondelete="set null", readonly=True)
    processed = fields.Boolean(default=False, readonly=True)

    _dedupe_uniq = models.Constraint(
        "unique(channel_id, provider_message_id, message_direction, status)",
        "Evento de status já registrado (callback repetido).",
    )
    _company_status_idx = models.Index("(company_id, status)")
    _channel_provider_msg_idx = models.Index("(channel_id, provider_message_id)")
    _outbox_occurred_idx = models.Index("(outbox_id, occurred_at)")
    _pending_idx = models.Index("(id) WHERE processed IS NOT TRUE")

    def write(self, vals):
        if set(vals) - self._MUTABLE_FIELDS:
            raise UserError(_("Eventos de mensagem são imutáveis (append-only)."))
        return super().write(vals)

    # ---------- contrato interno ----------
    @api.model
    def _validate_contract(self, event):
        """Valida o evento normalizado antes de gravar (ValueError se inválido)."""
        if not isinstance(event, dict):
            raise ValueError("evento deve ser dict")
        if not event.get("provider"):
            raise ValueError("provider obrigatório")
        if not event.get("provider_message_id"):
            raise ValueError("provider_message_id obrigatório")
        if event.get("direction") not in DIRECTIONS:
            raise ValueError("direction inválida")
        if event.get("status") not in STATUS_RANK:
            raise ValueError("status normalizado inválido")

    @api.model
    def _record(self, channel, event):
        """Grava (com dedupe) um evento normalizado e aplica-o à outbox.

        `event`: dict com provider, provider_message_id, direction, status,
        provider_status, occurred_at (datetime/str), error_code, error_message,
        payload (dict) e opcionalmente inbox_id. Retorna (record, created?).
        """
        self._validate_contract(event)
        Event = self.sudo()
        domain = [
            ("channel_id", "=", channel.id),
            ("provider_message_id", "=", str(event["provider_message_id"])),
            ("message_direction", "=", event["direction"]),
            ("status", "=", event["status"]),
        ]
        existing = Event.search(domain, limit=1)
        if existing:
            return existing, False
        body = payload_json(event.get("payload"))
        occurred = event.get("occurred_at")
        vals = {
            "channel_id": channel.id,
            "provider": event["provider"],
            "provider_message_id": str(event["provider_message_id"]),
            "message_direction": event["direction"],
            "status": event["status"],
            "provider_status": str(event.get("provider_status") or "")[:64] or False,
            "occurred_at": fields.Datetime.to_datetime(occurred) if occurred else False,
            "payload_hash": payload_digest(body),
            "payload_preview": payload_preview(body),
            "error_code": str(event.get("error_code") or "")[:64] or False,
            "error_message": sanitize_error(event.get("error_message")) or False,
            "inbox_id": event.get("inbox_id") or False,
        }
        try:
            with self.env.cr.savepoint():
                rec = Event.create(vals)
        except psycopg2.IntegrityError:
            dup = Event.search(domain, limit=1)
            if dup:
                return dup, False
            raise
        rec._apply_to_outbox()
        return rec, True

    def _apply_to_outbox(self):
        """Correlaciona com a outbox (canal + id do provedor) e aplica o status."""
        Outbox = self.env["dz23.message.outbox"].sudo()
        for ev in self:
            outbox = Outbox.browse()
            if ev.message_direction == "outbound":
                outbox = ev.outbox_id or Outbox.search(
                    [
                        ("channel_id", "=", ev.channel_id.id),
                        ("provider_message_id", "=", ev.provider_message_id),
                    ],
                    limit=1,
                )
                if outbox:
                    outbox._apply_status(
                        ev.status,
                        occurred_at=ev.occurred_at or ev.received_at,
                        error_code=ev.error_code,
                        error_message=ev.error_message,
                    )
            ev.write({"processed": True, "outbox_id": outbox.id or False})
