# Fila `ai_request` (ADR-008): a conversa livre com a IA roda FORA do item da
# inbox. O item da inbox só enfileira o pedido e termina rápido; este worker chama
# a IA com claim + lease, tenta de novo com backoff e, se esgotar as tentativas,
# entrega a resposta determinística de fallback (o cliente nunca fica sem retorno).
import logging
import time

from odoo import api, fields, models
from odoo.addons.dz23_whatsapp.models.queue_utils import (
    backoff_seconds,
    can_commit,
    claim_due,
    expired_leases,
    sanitize_error,
)
from odoo.tools.translate import _

_logger = logging.getLogger(__name__)

_MAX_ATTEMPTS = 3
_BATCH = 10
_LEASE_SECONDS = 300  # acima do timeout da IA
_TIME_BUDGET_SECONDS = 50
_MAX_BACKOFF_SECONDS = 120  # conversa: não deixar o cliente esperando demais


class DZ23AIRequest(models.Model):
    _name = "dz23.ai.request"
    _description = "DZ23 — Pedido de resposta por IA (fila fora do caminho crítico)"
    _order = "id"

    channel_id = fields.Many2one("dz23.channel", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one(
        related="channel_id.company_id", store=True, index=True, readonly=True
    )
    lead_id = fields.Many2one("crm.lead", ondelete="set null", index=True, readonly=True)
    recipient = fields.Char(required=True, readonly=True)
    prompt_text = fields.Text(required=True, readonly=True, help="Mensagem do cliente.")
    fallback_text = fields.Text(readonly=True)
    correlation_id = fields.Char(index=True, readonly=True)
    status = fields.Selection(
        [
            ("pending", "Pendente"),
            ("processing", "Processando"),
            ("done", "Respondido"),
            ("failed", "Falha (retry)"),
        ],
        default="pending",
        required=True,
        index=True,
    )
    attempts = fields.Integer(default=0)
    max_attempts = fields.Integer(default=_MAX_ATTEMPTS)
    next_attempt_at = fields.Datetime(default=fields.Datetime.now, index=True)
    lease_until = fields.Datetime(index=True, readonly=True)
    duration_ms = fields.Integer(readonly=True)
    processed_at = fields.Datetime(readonly=True)
    response_text = fields.Text(readonly=True)
    used_fallback = fields.Boolean(readonly=True)
    provider = fields.Char(readonly=True)
    model_name = fields.Char("Modelo", readonly=True)
    prompt_version = fields.Char(readonly=True)
    error = fields.Char(readonly=True)

    @api.model
    def _enqueue(self, channel, lead, recipient, text, fallback, correlation_id=None):
        return self.sudo().create(
            {
                "channel_id": channel.id,
                "lead_id": lead.id if lead else False,
                "recipient": recipient,
                "prompt_text": text,
                "fallback_text": fallback or False,
                "correlation_id": correlation_id or False,
                "status": "pending",
                "next_attempt_at": fields.Datetime.now(),
            }
        )

    # ---------- worker ----------
    @api.model
    def _cron_process(self, limit=_BATCH):
        self.env.flush_all()
        self._recover_expired_leases()
        self.env.flush_all()
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
        for rec in self.browse(expired_leases(self.env.cr, self._table, "processing")):
            if rec.attempts >= rec.max_attempts:
                rec._deliver(
                    rec.fallback_text or rec._generic_fallback(),
                    used_fallback=True,
                    started=time.monotonic(),
                    error=_("lease expirado"),
                )
            else:
                rec.write(
                    {
                        "status": "failed",
                        "lease_until": False,
                        "next_attempt_at": fields.Datetime.now(),
                        "error": _("Lease expirado (worker interrompido); tentando de novo."),
                    }
                )

    def _claim_one(self):
        self.ensure_one()
        self.write(
            {
                "status": "processing",
                "attempts": self.attempts + 1,
                "lease_until": fields.Datetime.add(fields.Datetime.now(), seconds=_LEASE_SECONDS),
            }
        )

    @staticmethod
    def _generic_fallback():
        return _("Oi! Já vi sua mensagem 😊 Um atendente vai te responder em instantes.")

    def _process_one(self):
        self.ensure_one()
        if self.status == "done":
            return
        if self.status != "processing":
            self._claim_one()
        started = time.monotonic()
        try:
            with self.env.cr.savepoint():
                channel = self.channel_id._processing_self()
                lead = self.lead_id.with_env(channel.env) if self.lead_id else None
                system = channel._agent_system_prompt(lead) if lead else None
                reply = (channel.env["dz23.ai"].chat(self.prompt_text, system=system) or "").strip()
                if not reply:
                    raise ValueError("IA retornou resposta vazia")
                self._deliver(reply, used_fallback=False, started=started)
        except Exception as e:  # noqa: BLE001 - retry e, no fim, fallback
            self._register_failure(e, started)

    def _register_failure(self, exc, started):
        if self.attempts >= self.max_attempts:
            _logger.warning(
                "IA request %s: fallback após %s tentativa(s) (%s).",
                self.id,
                self.attempts,
                type(exc).__name__,
            )
            self._deliver(
                self.fallback_text or self._generic_fallback(),
                used_fallback=True,
                started=started,
                error=sanitize_error("%s: %s" % (type(exc).__name__, exc)),
            )
            return
        delay = min(_MAX_BACKOFF_SECONDS, backoff_seconds(self.attempts))
        self.write(
            {
                "status": "failed",
                "lease_until": False,
                "duration_ms": int((time.monotonic() - started) * 1000),
                "error": sanitize_error("%s: %s" % (type(exc).__name__, exc)),
                "next_attempt_at": fields.Datetime.add(fields.Datetime.now(), seconds=delay),
            }
        )

    def _deliver(self, text, used_fallback, started, error=None):
        """Enfileira a resposta na outbox e fecha o pedido (provedor/modelo auditados)."""
        self.ensure_one()
        self.env["dz23.message.outbox"].sudo()._enqueue(self.channel_id, self.recipient, text)
        if self.lead_id:
            self.lead_id.sudo().message_post(
                body=_("🤖 Resposta enfileirada para envio: %s") % text
            )
        ai = self.env["dz23.ai"]
        self.write(
            {
                "status": "done",
                "lease_until": False,
                "processed_at": fields.Datetime.now(),
                "duration_ms": int((time.monotonic() - started) * 1000),
                "response_text": text,
                "used_fallback": used_fallback,
                "provider": ai._provider(),
                "model_name": ai._model(),
                "prompt_version": self.channel_id._agent_prompt_version(),
                "error": error or False,
            }
        )
