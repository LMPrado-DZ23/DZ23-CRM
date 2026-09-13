# Outbox DURÁVEL de respostas a enviar. O agente/atendente ENFILEIRA aqui; um
# worker envia com claim + lease (ADR-003), retry exponencial com jitter e DLQ.
#
# GARANTIA: entrega *at-least-once*. Sucesso exige id de mensagem no corpo da
# resposta do provedor. Se o worker morrer entre o aceite do provedor e o commit,
# o lease vence e o item é reenviado (o cliente pode receber 2x) — registrado no
# erro do item. O CICLO DE VIDA após o envio (delivered/read/failed) vem dos
# callbacks de status via dz23.message.event, com transições monotônicas (ADR-006).
import logging
import time
import uuid

from odoo import api, fields, models
from odoo.tools.translate import _

from .message_event import MESSAGE_STATUSES, STATUS_RANK
from .provider_errors import ProviderError, ProviderTransientError
from .queue_utils import (
    backoff_seconds,
    can_commit,
    claim_due,
    expired_leases,
    sanitize_error,
)

_logger = logging.getLogger(__name__)

_MAX_ATTEMPTS = 6
_BATCH = 20
_LEASE_SECONDS = 300
_TIME_BUDGET_SECONDS = 50
_MARKER_FIELDS = {
    "sent": "sent_at",
    "delivered": "delivered_at",
    "read": "read_at",
}
_FAILURE_STATUSES = ("failed", "undelivered", "expired", "cancelled")
_DEFAULT_PER_CHANNEL_BATCH = 10
_RATE_LIMIT_FLOOR_SECONDS = 60
DLQ_REASONS = [
    ("permanent_error", "Erro permanente do provedor"),
    ("max_attempts", "Tentativas esgotadas"),
    ("lease_expired", "Lease expirado"),
]


def _extract_provider_message_id(data):
    data = data if isinstance(data, dict) else {}
    return (
        (data.get("key") or {}).get("id")
        or (data.get("messages") or [{}])[0].get("id")
        or data.get("sid")
        or False
    )


class DZ23MessageOutbox(models.Model):
    _name = "dz23.message.outbox"
    _description = "DZ23 — Outbox durável de respostas (retry + DLQ + ciclo de vida)"
    _order = "id"

    channel_id = fields.Many2one("dz23.channel", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one(
        related="channel_id.company_id", store=True, index=True, readonly=True
    )
    recipient = fields.Char(required=True, help="Número E.164 do destinatário")
    body = fields.Text(required=True)
    # Estado do NOSSO envio (fila).
    status = fields.Selection(
        [
            ("pending", "Pendente"),
            ("sending", "Enviando"),
            ("sent", "Enviada"),
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
    duration_ms = fields.Integer(readonly=True, help="Duração da última tentativa de envio.")
    error = fields.Char()
    dlq_reason = fields.Selection(DLQ_REASONS, readonly=True, index=True)
    provider_message_id = fields.Char(
        readonly=True, index=True, help="Id da mensagem confirmado pelo provedor."
    )
    correlation_id = fields.Char(
        index=True, readonly=True, copy=False, default=lambda self: uuid.uuid4().hex
    )
    client_message_id = fields.Char(
        readonly=True, copy=False, help="Id enviado ao provedor quando ele suporta."
    )
    # Ciclo de vida da mensagem NO PROVEDOR (monotônico, ADR-006).
    current_status = fields.Selection(
        MESSAGE_STATUSES, default="queued", required=True, index=True, readonly=True
    )
    sent_at = fields.Datetime(readonly=True)
    delivered_at = fields.Datetime(readonly=True)
    read_at = fields.Datetime(readonly=True)
    failed_at = fields.Datetime(readonly=True)
    last_status_at = fields.Datetime(readonly=True)
    provider_error_code = fields.Char(readonly=True)
    provider_error_message = fields.Char(readonly=True)
    event_ids = fields.One2many("dz23.message.event", "outbox_id", readonly=True)

    # ---------- enfileirar ----------
    @api.model
    def _enqueue(self, channel, recipient, body):
        """Persiste a resposta a enviar. Retorna o record (ou vazio se sem corpo)."""
        if not body or not recipient:
            return self.browse()
        return self.sudo().create(
            {
                "channel_id": channel.id,
                "recipient": recipient,
                "body": body,
                "status": "pending",
                "next_attempt_at": fields.Datetime.now(),
            }
        )

    # ---------- worker (cron) ----------
    @api.model
    def _cron_process(self, limit=_BATCH):
        """Recupera leases vencidos, reivindica itens devidos e envia um a um."""
        self.env.flush_all()  # o claim é SQL: grava antes o que está pendente no ORM
        self._recover_expired_leases()
        # A recuperação escreve via ORM: grava ANTES do UPDATE do claim, senão o
        # flush tardio sobrescreveria o estado 'sending' do claim.
        self.env.flush_all()
        per_channel = int(
            self.env["ir.config_parameter"]
            .sudo()
            .get_param("dz23.whatsapp.outbox_per_channel_batch", _DEFAULT_PER_CHANNEL_BATCH)
            or _DEFAULT_PER_CHANNEL_BATCH
        )
        ids = claim_due(
            self.env.cr,
            self._table,
            ("pending", "failed"),
            "sending",
            _LEASE_SECONDS,
            limit,
            per_channel_limit=max(1, per_channel),
            skip_rate_limited=True,
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
            # Commit por item: um envio confirmado fica 'sent' mesmo que o
            # worker morra no item seguinte (reduz reenvio).
            if can_commit():
                self.env.cr.commit()

    @api.model
    def _recover_expired_leases(self):
        """'sending' com lease vencido: com id do provedor => enviado; sem id =>
        retry (possível reenvio, at-least-once) ou DLQ se esgotou tentativas."""
        self.flush_model()
        for rec in self.browse(expired_leases(self.env.cr, self._table, "sending")):
            if rec.provider_message_id:
                rec.write({"status": "sent", "lease_until": False, "error": False})
            elif rec.attempts >= rec.max_attempts:
                rec.write(
                    {
                        "status": "dead",
                        "dlq_reason": "lease_expired",
                        "lease_until": False,
                        "error": _("DLQ: lease expirado após %s tentativas") % rec.attempts,
                    }
                )
                rec._apply_status("failed", error_message=rec.error)
            else:
                rec.write(
                    {
                        "status": "failed",
                        "lease_until": False,
                        "next_attempt_at": fields.Datetime.now(),
                        "error": _(
                            "Lease expirado sem confirmação do provedor; reenviando "
                            "(possível mensagem duplicada)."
                        ),
                    }
                )
                _logger.warning("Outbox %s: lease expirado sem id do provedor.", rec.id)

    def _claim_one(self):
        self.ensure_one()
        self.write(
            {
                "status": "sending",
                "attempts": self.attempts + 1,
                "lease_until": fields.Datetime.add(fields.Datetime.now(), seconds=_LEASE_SECONDS),
            }
        )

    def _process_one(self):
        self.ensure_one()
        # Guarda: já enviado (id do provedor gravado) não reenvia.
        if self.status == "sent" or self.provider_message_id:
            if self.status != "sent":
                self.write({"status": "sent", "lease_until": False})
            return
        if self.status != "sending":
            self._claim_one()
        started = time.monotonic()
        try:
            with self.env.cr.savepoint():
                data = self.channel_id._processing_self().send_text(
                    self.recipient, self.body, correlation_id=self.correlation_id
                )
            now = fields.Datetime.now()
            self.write(
                {
                    "status": "sent",
                    "error": False,
                    "dlq_reason": False,
                    "lease_until": False,
                    "provider_message_id": _extract_provider_message_id(data),
                    # Só a Meta devolve o correlation_id nos callbacks de status.
                    "client_message_id": (
                        self.correlation_id if self.channel_id.provider == "meta_cloud" else False
                    ),
                    "duration_ms": int((time.monotonic() - started) * 1000),
                }
            )
            self._apply_status("sent", occurred_at=now)
        except Exception as e:  # noqa: BLE001 - qualquer falha vira retry/DLQ
            self._register_failure(e, started)

    def _register_failure(self, exc, started):
        """Erro permanente => DLQ imediata; transitório => retry com backoff
        (piso = Retry-After); 429 pausa o canal inteiro (ADR-008)."""
        now = fields.Datetime.now()
        permanent = isinstance(exc, ProviderError) and exc.permanent
        code = getattr(exc, "code", None) or getattr(exc, "status", None)
        vals = {
            "lease_until": False,
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
        if permanent or self.attempts >= self.max_attempts:
            reason = "permanent_error" if permanent else "max_attempts"
            vals.update(
                {
                    "status": "dead",
                    "dlq_reason": reason,
                    "error": _("DLQ (%(reason)s): %(error)s")
                    % {"reason": reason, "error": sanitize_error(exc)},
                }
            )
            self.write(vals)
            self._apply_status("failed", error_code=code, error_message=vals["error"])
            _logger.warning(
                "Outbox %s -> DLQ (%s) após %s tentativa(s).", self.id, reason, self.attempts
            )
            return
        retry_after = getattr(exc, "retry_after", None) or 0
        delay = max(backoff_seconds(self.attempts), retry_after)
        vals.update(
            {
                "status": "failed",
                "error": sanitize_error("%s: %s" % (type(exc).__name__, exc)),
                "next_attempt_at": fields.Datetime.add(now, seconds=delay),
            }
        )
        self.write(vals)
        if isinstance(exc, ProviderTransientError) and exc.status == 429:
            pause = max(retry_after, _RATE_LIMIT_FLOOR_SECONDS)
            self.channel_id.sudo().write(
                {"rate_limited_until": fields.Datetime.add(now, seconds=pause)}
            )
            _logger.warning("Canal %s pausado %ss por rate limit (429).", self.channel_id.id, pause)

    def _reconcile_provider_id(self, provider_message_id):
        """Ack perdido: o callback do provedor (id + correlation_id) prova o envio,
        então o item não é reenviado (ADR-008 §4)."""
        self.ensure_one()
        vals = {"provider_message_id": provider_message_id}
        if self.status in ("pending", "failed", "sending", "dead"):
            vals.update(
                {"status": "sent", "lease_until": False, "error": False, "dlq_reason": False}
            )
        self.write(vals)
        _logger.info("Outbox %s reconciliado pelo callback do provedor.", self.id)

    # ---------- ciclo de vida (ADR-006) ----------
    def _apply_status(self, status, occurred_at=None, error_code=None, error_message=None):
        """Aplica um status normalizado de forma MONOTÔNICA, sob lock de linha.

        - só avança quando o rank do novo status é maior que o atual;
        - falha depois de `delivered` não regride, mas grava o erro;
        - marcos (sent/delivered/read_at) são fatos: gravados uma vez, mesmo que
          cheguem fora de ordem.
        Retorna True se `current_status` mudou.
        """
        self.ensure_one()
        if status not in STATUS_RANK:
            status = "unknown"
        self.env.cr.execute(
            "SELECT id FROM dz23_message_outbox WHERE id = %s FOR UPDATE", (self.id,)
        )
        self.invalidate_recordset()
        occurred = occurred_at or fields.Datetime.now()
        current = self.current_status or "queued"
        vals = {}
        changed = False
        if status != "unknown" and STATUS_RANK[status] > STATUS_RANK.get(current, 0):
            vals["current_status"] = status
            changed = True
            if status in _FAILURE_STATUSES and not self.failed_at:
                vals["failed_at"] = occurred
        marker = _MARKER_FIELDS.get(status)
        if marker and not self[marker]:
            vals[marker] = occurred
        if status in _FAILURE_STATUSES:
            if error_code:
                vals["provider_error_code"] = str(error_code)[:64]
            if error_message:
                vals["provider_error_message"] = sanitize_error(error_message)
        if not self.last_status_at or occurred > self.last_status_at:
            vals["last_status_at"] = occurred
        if vals:
            self.write(vals)
        return changed

    def action_requeue(self):
        """Reenvia itens da DLQ/falha (ação administrativa; registrada em log)."""
        for rec in self:
            _logger.info("Outbox %s reenfileirado por usuário %s.", rec.id, self.env.uid)
            rec.write(
                {
                    "status": "pending",
                    "attempts": 0,
                    "next_attempt_at": fields.Datetime.now(),
                    "lease_until": False,
                    "error": False,
                    "dlq_reason": False,
                }
            )
        return True
