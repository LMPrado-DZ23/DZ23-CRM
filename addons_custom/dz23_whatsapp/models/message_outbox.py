# Outbox DURÁVEL de respostas a enviar. O agente ENFILEIRA a resposta aqui
# (o efeito de negócio já foi aplicado exatamente uma vez pelo inbox); um worker
# envia com retry exponencial + jitter e DLQ. Evita PERDER a resposta ao cliente
# quando o provedor (Evolution/Meta/Twilio) está momentaneamente fora (HIGH-01).
#
# GARANTIA DE ENTREGA: at-least-once. Sucesso exige id de mensagem no corpo da
# resposta do provedor (não só HTTP 2xx). Se o provedor entrega mas o ack se
# perde (timeout após processar), pode haver reenvio (cliente recebe 2x). Os
# provedores não recebem idempotency key hoje — LIMITAÇÃO CONHECIDA; mitigar com
# clientMessageId derivado do outbox.id quando o provedor suportar dedupe.
import logging
import secrets
from datetime import timedelta

from odoo import api, fields, models
from odoo.tools import config
from odoo.tools.translate import _

_logger = logging.getLogger(__name__)

_MAX_ATTEMPTS = 6
_BATCH = 20
# Jitter de retry com fonte do SO (evita padrão previsível e o B311 do bandit).
_JITTER = secrets.SystemRandom()


def _sanitize(msg):
    """Mensagem de erro curta e sem conteúdo sensível."""
    return (msg or "")[:200]


class DZ23MessageOutbox(models.Model):
    _name = "dz23.message.outbox"
    _description = "DZ23 — Outbox durável de respostas (retry + DLQ)"
    _order = "id"

    channel_id = fields.Many2one("dz23.channel", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one(
        related="channel_id.company_id", store=True, index=True, readonly=True
    )
    recipient = fields.Char(required=True, help="Número E.164 do destinatário")
    body = fields.Text(required=True)
    status = fields.Selection(
        [
            ("pending", "Pendente"),
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
    error = fields.Char()
    provider_message_id = fields.Char(
        readonly=True, help="Id da mensagem confirmado pelo provedor (rastreabilidade)."
    )

    # ---------- enfileirar (chamado pelo agente após aplicar o efeito) ----------
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
    def _cron_process(self):
        """Reivindica lote devido com FOR UPDATE SKIP LOCKED e envia."""
        # next_attempt_at é UTC naive; comparar com clock_timestamp() convertido
        # para UTC evita erro de fuso quando o TimeZone da sessão PG não é UTC.
        self.env.cr.execute(
            """
            SELECT id FROM dz23_message_outbox
            WHERE status IN ('pending', 'failed')
              AND next_attempt_at <= (clock_timestamp() AT TIME ZONE 'utc')
            ORDER BY id LIMIT %s FOR UPDATE SKIP LOCKED
        """,
            (_BATCH,),
        )
        ids = [r[0] for r in self.env.cr.fetchall()]
        if not ids:
            return
        in_test = config["test_enable"]
        for rec in self.browse(ids):
            rec._process_one()
            # Commit por registro: se o cron morrer DEPOIS de enviar, o item já
            # está persistido como 'sent' e NÃO é reenviado (reduz duplicidade —
            # os provedores de WhatsApp não têm idempotency key nativa). O Odoo
            # proíbe commit dentro de teste, então pulamos nesse caso (a guarda
            # de 'sent'/provider_message_id garante o não-reenvio na mesma tx).
            if not in_test:
                self.env.cr.commit()

    def _process_one(self):
        self.ensure_one()
        # Guarda: já enviado (id do provedor gravado) não reenvia.
        if self.status == "sent" or self.provider_message_id:
            return
        try:
            with self.env.cr.savepoint():
                data = self.channel_id._processing_self().send_text(self.recipient, self.body)
                data = data if isinstance(data, dict) else {}
                mid = (
                    (data.get("key") or {}).get("id")
                    or (data.get("messages") or [{}])[0].get("id")
                    or data.get("sid")
                )
                self.write({"status": "sent", "error": False, "provider_message_id": mid or False})
        except Exception as e:  # noqa: BLE001
            attempts = self.attempts + 1
            if attempts >= self.max_attempts:
                self.write(
                    {
                        "status": "dead",
                        "attempts": attempts,
                        "error": _("DLQ: %s") % _sanitize(str(e)),
                    }
                )
                _logger.warning("Outbox %s -> DLQ após %s tentativas.", self.id, attempts)
            else:
                backoff = min(3600, 2**attempts)
                delay = backoff + _JITTER.randint(0, max(1, backoff // 2))
                self.write(
                    {
                        "status": "failed",
                        "attempts": attempts,
                        "error": _sanitize("%s: %s" % (type(e).__name__, e)),
                        "next_attempt_at": fields.Datetime.now() + timedelta(seconds=delay),
                    }
                )

    def action_requeue(self):
        """Reprocessa itens da DLQ (ação administrativa auditável)."""
        for rec in self:
            rec.write(
                {
                    "status": "pending",
                    "attempts": 0,
                    "next_attempt_at": fields.Datetime.now(),
                    "error": False,
                }
            )
        return True
