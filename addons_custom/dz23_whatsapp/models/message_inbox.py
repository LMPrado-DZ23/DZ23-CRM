# Inbox DURÁVEL de mensagens recebidas. O webhook autentica, valida e PERSISTE
# aqui (dedupe por (provider, channel_id, message_id)) e responde rápido; um worker
# (cron) processa depois com claim + lease (ADR-003), retry exponencial com jitter
# e DLQ. Entrega do provedor é at-least-once; o dedupe garante UM registro por
# message_id e os efeitos de negócio são idempotentes (ADR-005).
import json
import logging
import time
import uuid

import psycopg2
from odoo import api, fields, models
from odoo.tools.translate import _

from .queue_utils import (
    backoff_seconds,
    can_commit,
    claim_due,
    expired_leases,
    payload_digest,
    payload_json,
    payload_preview,
    sanitize_error,
)

_logger = logging.getLogger(__name__)

_MAX_ATTEMPTS = 6
_BATCH = 20
_LEASE_SECONDS = 600  # IA local pode levar até 120 s; margem para o worker
_TIME_BUDGET_SECONDS = 50  # não emendar execuções do cron de 1 min


class DZ23MessageInbox(models.Model):
    _name = "dz23.message.inbox"
    _description = "DZ23 — Inbox durável de mensagens (idempotente + DLQ)"
    _order = "id"

    channel_id = fields.Many2one("dz23.channel", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one(
        related="channel_id.company_id", store=True, index=True, readonly=True
    )
    provider = fields.Char(index=True)
    message_id = fields.Char(required=True, index=True)
    sender = fields.Char(help="provider_user_id / número E.164")
    text = fields.Text()
    payload = fields.Text(help="Envelope JSON completo (validado, sem truncamento).")
    payload_hash = fields.Char(index=True, readonly=True, help="SHA-256 do payload.")
    payload_preview = fields.Text(readonly=True)
    correlation_id = fields.Char(
        index=True, readonly=True, copy=False, default=lambda self: uuid.uuid4().hex
    )
    received_at = fields.Datetime(default=fields.Datetime.now, readonly=True)
    processed_at = fields.Datetime(readonly=True)
    status = fields.Selection(
        [
            ("pending", "Pendente"),
            ("processing", "Processando"),
            ("done", "Concluída"),
            ("failed", "Falha (retry)"),
            ("dead", "DLQ"),
        ],
        default="pending",
        required=True,
        index=True,
    )
    attempts = fields.Integer(default=0)
    max_attempts = fields.Integer(default=_MAX_ATTEMPTS)
    next_attempt_at = fields.Datetime(default=fields.Datetime.now, index=True)
    lease_until = fields.Datetime(index=True, readonly=True)
    duration_ms = fields.Integer(readonly=True, help="Duração da última tentativa.")
    error = fields.Char()

    _uniq = models.Constraint(
        "unique(provider, channel_id, message_id)", "Mensagem já recebida (idempotência)."
    )

    # ---------- enfileirar (chamado pelo webhook) ----------
    @api.model
    def _enqueue(self, channel, message_id, sender, text, payload_dict):
        """Persiste com dedupe. Retorna (record, created?).

        Só a violação de unicidade (duplicata concorrente) é tratada; qualquer
        outro erro PROPAGA para o webhook responder 500 e o provedor reentregar.
        """
        if not message_id:
            raise ValueError("message_id obrigatório para o dedupe do inbox")
        Inbox = self.sudo()
        domain = [("channel_id", "=", channel.id), ("message_id", "=", message_id)]
        existing = Inbox.search(domain, limit=1)
        if existing:
            return existing, False
        body = payload_json(payload_dict)
        json.loads(body)  # valida o JSON antes de gravar
        try:
            with self.env.cr.savepoint():
                rec = Inbox.create(
                    {
                        "channel_id": channel.id,
                        "provider": channel.provider,
                        "message_id": message_id,
                        "sender": sender,
                        "text": text,
                        "payload": body,
                        "payload_hash": payload_digest(body),
                        "payload_preview": payload_preview(body),
                        "status": "pending",
                        "next_attempt_at": fields.Datetime.now(),
                    }
                )
            return rec, True
        except psycopg2.IntegrityError:
            dup = Inbox.search(domain, limit=1)
            if dup:
                return dup, False
            raise

    # ---------- worker (cron) ----------
    @api.model
    def _cron_process(self, limit=_BATCH):
        """Recupera leases vencidos, reivindica itens devidos e processa um a um."""
        self.flush_model()  # o claim é SQL: grava antes o que está pendente no ORM
        self._recover_expired_leases()
        # A recuperação escreve via ORM: grava ANTES do UPDATE do claim, senão o
        # flush tardio sobrescreveria o estado 'processing' do claim.
        self.flush_model()
        ids = claim_due(
            self.env.cr, self._table, ("pending", "failed"), "processing", _LEASE_SECONDS, limit
        )
        if not ids:
            return
        self.invalidate_model(["status", "attempts", "lease_until"])
        if can_commit():
            self.env.cr.commit()
        started = time.monotonic()
        for rec in self.browse(ids):
            if time.monotonic() - started > _TIME_BUDGET_SECONDS:
                # Devolve o restante sem gastar tentativa (o claim contou uma).
                rec.write(
                    {
                        "status": "pending",
                        "attempts": max(0, rec.attempts - 1),
                        "lease_until": False,
                    }
                )
                continue
            rec._process_one()
            if can_commit():
                self.env.cr.commit()

    @api.model
    def _recover_expired_leases(self):
        """Item preso em 'processing' (worker morto/timeout) volta para retry ou DLQ."""
        self.flush_model()
        for rec in self.browse(expired_leases(self.env.cr, self._table, "processing")):
            if rec.attempts >= rec.max_attempts:
                rec.write(
                    {
                        "status": "dead",
                        "lease_until": False,
                        "error": _("DLQ: lease expirado após %s tentativas") % rec.attempts,
                    }
                )
                _logger.warning("Inbox %s -> DLQ (lease expirado).", rec.id)
            else:
                rec.write(
                    {
                        "status": "failed",
                        "lease_until": False,
                        "error": _("Lease expirado (worker interrompido); reprocessando."),
                        "next_attempt_at": fields.Datetime.now(),
                    }
                )

    def _claim_one(self):
        """Claim de um item fora do cron (ação manual/teste): conta a tentativa."""
        self.ensure_one()
        self.write(
            {
                "status": "processing",
                "attempts": self.attempts + 1,
                "lease_until": fields.Datetime.add(fields.Datetime.now(), seconds=_LEASE_SECONDS),
            }
        )

    def _payload_dict(self):
        try:
            return json.loads(self.payload or "{}")
        except ValueError:
            # Registros antigos gravados truncados (antes da 19.0.8): o
            # atendimento não depende do envelope, só de sender/text.
            return {}

    def _process_one(self):
        self.ensure_one()
        if self.status == "done":
            return
        if self.status != "processing":
            self._claim_one()
        started = time.monotonic()
        try:
            with self.env.cr.savepoint():
                self.channel_id._processing_self().handle_inbound(
                    self.sender, self.text, self._payload_dict()
                )
                self.write(
                    {
                        "status": "done",
                        "error": False,
                        "lease_until": False,
                        "processed_at": fields.Datetime.now(),
                        "duration_ms": int((time.monotonic() - started) * 1000),
                    }
                )
        except Exception as e:  # noqa: BLE001 - qualquer falha vira retry/DLQ
            self._register_failure(e, started)

    def _register_failure(self, exc, started):
        vals = {
            "lease_until": False,
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
        if self.attempts >= self.max_attempts:
            vals.update(
                {"status": "dead", "error": _("DLQ: %s") % sanitize_error(exc)},
            )
            _logger.warning("Inbox %s -> DLQ após %s tentativas.", self.id, self.attempts)
        else:
            vals.update(
                {
                    "status": "failed",
                    "error": sanitize_error("%s: %s" % (type(exc).__name__, exc)),
                    "next_attempt_at": fields.Datetime.add(
                        fields.Datetime.now(), seconds=backoff_seconds(self.attempts)
                    ),
                }
            )
        self.write(vals)

    def action_requeue(self):
        """Reprocessa itens da DLQ/falha (ação administrativa auditável no chatter do log)."""
        for rec in self:
            _logger.info("Inbox %s reenfileirado por usuário %s.", rec.id, self.env.uid)
            rec.write(
                {
                    "status": "pending",
                    "attempts": 0,
                    "next_attempt_at": fields.Datetime.now(),
                    "lease_until": False,
                    "error": False,
                }
            )
        return True
