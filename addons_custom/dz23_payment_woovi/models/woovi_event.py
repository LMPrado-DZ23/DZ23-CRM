# Eventos Woovi PERSISTIDOS antes de processar (Fase 5): webhook e conciliação viram
# registros deduplicados pelo hash do corpo; o processamento roda na hora (rápido)
# e, se falhar, fica pendente para o cron. Cada efeito é aplicado uma única vez.
import hashlib
import json
import logging

import psycopg2
from odoo import api, fields, models

from .woovi_common import WooviEventError, normalize_status, paid_cents

_logger = logging.getLogger(__name__)

_MAX_ATTEMPTS = 5


class DZ23WooviEvent(models.Model):
    _name = "dz23.woovi.event"
    _description = "DZ23 — Evento de pagamento Woovi (deduplicado)"
    _order = "id desc"
    _rec_name = "event_key"

    event_key = fields.Char(required=True, index=True, readonly=True)
    source = fields.Selection(
        [("webhook", "Webhook"), ("reconcile", "Conciliação")], required=True, readonly=True
    )
    event_type = fields.Char(readonly=True)
    correlation_id = fields.Char(index=True, readonly=True)
    charge_id = fields.Char(index=True, readonly=True)
    woovi_status = fields.Char("Status Woovi", readonly=True)
    value_cents = fields.Integer("Valor recebido (centavos)", readonly=True)
    transaction_id = fields.Many2one(
        "payment.transaction", ondelete="set null", index=True, readonly=True
    )
    company_id = fields.Many2one(
        related="transaction_id.company_id", store=True, index=True, readonly=True
    )
    state = fields.Selection(
        [
            ("pending", "Pendente"),
            ("done", "Aplicado"),
            ("ignored", "Ignorado"),
            ("rejected", "Rejeitado"),
            ("failed", "Falhou"),
        ],
        default="pending",
        required=True,
        index=True,
        readonly=True,
    )
    attempts = fields.Integer(readonly=True)
    result = fields.Char(readonly=True)
    error = fields.Char(readonly=True)
    received_at = fields.Datetime(default=fields.Datetime.now, readonly=True)
    processed_at = fields.Datetime(readonly=True)
    payload = fields.Text(readonly=True)

    _key_uniq = models.Constraint("unique(event_key)", "Evento Woovi já recebido.")

    @api.model
    def _ingest_payload(self, data, source="webhook", raw=None):
        """Persiste (dedupe) e tenta aplicar. Retorna (evento, criado?).
        Erro de persistência propaga (o webhook responde 500)."""
        body = json.dumps(data or {}, sort_keys=True, ensure_ascii=False, default=str)
        key = hashlib.sha256(raw if raw else body.encode("utf-8")).hexdigest()
        Event = self.sudo()
        existing = Event.search([("event_key", "=", key)], limit=1)
        if existing:
            return existing, False
        charge = (data or {}).get("charge") or {}
        vals = {
            "event_key": key,
            "source": source,
            "event_type": str((data or {}).get("event") or "")[:64] or False,
            "correlation_id": charge.get("correlationID") or False,
            "charge_id": charge.get("identifier") or charge.get("globalID") or False,
            "woovi_status": normalize_status(data) or False,
            "value_cents": paid_cents(data) or 0,
            "payload": body,
        }
        try:
            with self.env.cr.savepoint():
                event = Event.create(vals)
        except psycopg2.IntegrityError:
            dup = Event.search([("event_key", "=", key)], limit=1)
            if dup:
                return dup, False
            raise
        event._process_one()
        return event, True

    def _find_transaction(self):
        self.ensure_one()
        Tx = self.env["payment.transaction"].sudo()
        tx = Tx.browse()
        if self.correlation_id:
            tx = Tx.search(
                [("reference", "=", self.correlation_id), ("provider_code", "=", "woovi")], limit=1
            )
        if not tx and self.charge_id:
            tx = Tx.search(
                [("woovi_charge_id", "=", self.charge_id), ("provider_code", "=", "woovi")], limit=1
            )
        return tx

    def _process_one(self):
        self.ensure_one()
        if self.state != "pending":
            return
        attempts = self.attempts + 1
        now = fields.Datetime.now()
        try:
            with self.env.cr.savepoint():
                tx = self.transaction_id or self._find_transaction()
                if not tx:
                    self.write(
                        {
                            "state": "ignored",
                            "attempts": attempts,
                            "result": "transação não encontrada",
                            "processed_at": now,
                        }
                    )
                    return
                outcome, message = tx._woovi_apply_payment_data(json.loads(self.payload or "{}"))
                self.write(
                    {
                        "transaction_id": tx.id,
                        "state": {"applied": "done", "ignored": "ignored", "rejected": "rejected"}[
                            outcome
                        ],
                        "result": message,
                        "attempts": attempts,
                        "processed_at": now,
                        "error": False,
                    }
                )
        except WooviEventError as error:
            # Regra de negócio (ex.: sem valor recebido): não confirma; a conciliação
            # periódica consulta a cobrança e aplica o status real.
            self.write({"state": "failed", "attempts": attempts, "error": str(error)[:200]})
        except Exception as error:  # noqa: BLE001 - retry pelo cron
            self.write(
                {
                    "state": "failed" if attempts >= _MAX_ATTEMPTS else "pending",
                    "attempts": attempts,
                    "error": type(error).__name__,
                }
            )
            _logger.warning("Evento Woovi %s falhou (%s).", self.id, type(error).__name__)

    @api.model
    def _cron_process_pending(self, limit=100):
        for event in self.sudo().search([("state", "=", "pending")], order="id", limit=limit):
            event._process_one()
