# Fila `ai_request` (ADR-008): a conversa livre com a IA roda FORA do item da
# inbox. O item da inbox só enfileira o pedido e termina rápido; este worker chama
# a IA com claim + lease, tenta de novo com backoff e, se esgotar as tentativas,
# entrega a resposta determinística de fallback (o cliente nunca fica sem retorno).
import logging
import time

from odoo import api, fields, models
from odoo.addons.dz23_ai.models.ai_service import (
    AICircuitOpenError,
    AIConfigError,
    AILimitExceededError,
)
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
# Sem retry: breaker aberto, limite da empresa ou configuração ausente => humano já.
_NON_RETRYABLE = (AICircuitOpenError, AIConfigError, AILimitExceededError)
_PURGED_TEXT = "[removido por retenção]"


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
    guard_triggered = fields.Boolean(
        "Guarda acionada",
        readonly=True,
        help="A resposta da IA falava de preço/desconto/pagamento e foi trocada por texto fixo.",
    )

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
                rec._deliver_handoff(_("lease expirado"), time.monotonic())
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

    def _conversation(self):
        self.ensure_one()
        Conversation = self.env["dz23.conversation"].sudo()
        if not self.lead_id:
            return Conversation
        contact = self.channel_id.sudo()._agent_contact_for_lead(self.lead_id)
        if not contact:
            return Conversation
        return Conversation.search([("contact_id", "=", contact.id)], limit=1)

    def _process_one(self):
        self.ensure_one()
        if self.status == "done":
            return
        if self.status != "processing":
            self._claim_one()
        started = time.monotonic()
        conversation = self._conversation()
        if conversation and not conversation.bot_can_reply():
            # Humano assumiu (ou o robô transferiu) depois do enfileiramento: silêncio.
            self.write(
                {
                    "status": "done",
                    "lease_until": False,
                    "processed_at": fields.Datetime.now(),
                    "duration_ms": 0,
                    "error": _("Conversa com atendimento humano; resposta do robô descartada."),
                }
            )
            return
        try:
            with self.env.cr.savepoint():
                channel = self.channel_id._processing_self()
                lead = self.lead_id.with_env(channel.env) if self.lead_id else None
                system = channel._agent_system_prompt(lead) if lead else None
                reply = (
                    channel.env["dz23.ai"].chat(
                        self.prompt_text,
                        system=system,
                        company=channel.company_id,
                        purpose="whatsapp_reply",
                    )
                    or ""
                ).strip()
                if not reply:
                    raise ValueError("IA retornou resposta vazia")
                reply, guarded = channel._agent_guard_ai_reply(reply)
                self._deliver(reply, used_fallback=False, started=started, guard_triggered=guarded)
        except _NON_RETRYABLE as e:
            self._deliver_handoff(sanitize_error("%s: %s" % (type(e).__name__, e)), started)
        except Exception as e:  # noqa: BLE001 - retry e, no fim, transferência
            self._register_failure(e, started)

    def _deliver_handoff(self, error, started):
        """IA indisponível: avisa o cliente e passa a conversa para um humano. Uma falha
        aqui não derruba o lote do cron (o item volta para retry)."""
        self.ensure_one()
        try:
            with self.env.cr.savepoint():
                conversation = self._conversation()
                if conversation and conversation.bot_can_reply():
                    conversation._agent_handoff("ai_unavailable")
                    text = self.channel_id._agent_handoff_text()
                else:
                    text = self.fallback_text or self._generic_fallback()
                self._deliver(text, used_fallback=True, started=started, error=error)
        except Exception as exc:  # noqa: BLE001 - isola o item
            _logger.warning(
                "IA request %s: transferência falhou (%s).", self.id, type(exc).__name__
            )
            self.write(
                {
                    "status": "failed",
                    "lease_until": False,
                    "error": sanitize_error("%s: %s" % (type(exc).__name__, exc)),
                    "next_attempt_at": fields.Datetime.add(
                        fields.Datetime.now(), seconds=_MAX_BACKOFF_SECONDS
                    ),
                }
            )

    def _register_failure(self, exc, started):
        if self.attempts >= self.max_attempts:
            _logger.warning(
                "IA request %s: transferência após %s tentativa(s) (%s).",
                self.id,
                self.attempts,
                type(exc).__name__,
            )
            self._deliver_handoff(sanitize_error("%s: %s" % (type(exc).__name__, exc)), started)
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

    def _deliver(self, text, used_fallback, started, error=None, guard_triggered=False):
        """Enfileira a resposta na outbox e fecha o pedido (provedor/modelo auditados)."""
        self.ensure_one()
        self.env["dz23.message.outbox"].sudo()._enqueue(self.channel_id, self.recipient, text)
        if self.lead_id:
            self.lead_id.sudo().message_post(
                body=_("🤖 Resposta enfileirada para envio: %s") % text
            )
        ai = self.env["dz23.ai"]
        company = self.channel_id.company_id
        self.write(
            {
                "status": "done",
                "lease_until": False,
                "processed_at": fields.Datetime.now(),
                "duration_ms": int((time.monotonic() - started) * 1000),
                "response_text": text,
                "used_fallback": used_fallback,
                "guard_triggered": guard_triggered,
                "provider": ai._provider(company),
                "model_name": ai._model(company),
                "prompt_version": self.channel_id._agent_prompt_version(),
                "error": error or False,
            }
        )

    @api.model
    def _cron_purge_texts(self):
        """Retenção (ADR-011): textos de conversa saem do pedido após N dias; os
        metadados (provedor, modelo, versão do prompt, duração) ficam para auditoria."""
        param = (
            self.env["ir.config_parameter"].sudo().get_param("dz23.ai.request_retention_days", "90")
        )
        try:
            days = int(param or 0)
        except ValueError:
            days = 90
        if days <= 0:
            return 0
        cutoff = fields.Datetime.subtract(fields.Datetime.now(), days=days)
        old = self.sudo().search(
            [
                ("status", "=", "done"),
                ("processed_at", "<", cutoff),
                ("prompt_text", "!=", _PURGED_TEXT),
            ]
        )
        old.write({"prompt_text": _PURGED_TEXT, "response_text": False, "fallback_text": False})
        return len(old)
