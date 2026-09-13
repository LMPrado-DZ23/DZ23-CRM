# Inbox DURÁVEL de mensagens recebidas. O webhook só autentica, valida e
# PERSISTE aqui (dedupe por (provider, channel_id, message_id)) e responde
# rápido; um worker (cron) processa depois, com retry exponencial + jitter e
# DLQ. Garante EXATAMENTE UM efeito por message_id (idempotência).
import json
import logging
import secrets
from datetime import timedelta

from odoo import api, fields, models
from odoo.tools.translate import _

_logger = logging.getLogger(__name__)

_MAX_ATTEMPTS = 6
_BATCH = 20
# Jitter de retry com fonte do SO (evita padrão previsível e o B311 do bandit).
_JITTER = secrets.SystemRandom()


def _sanitize(msg):
    """Mensagem de erro curta e sem conteúdo sensível."""
    return (msg or "")[:200]


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
    payload = fields.Text(help="Envelope minimizado (JSON).")
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
    error = fields.Char()

    _uniq = models.Constraint(
        "unique(provider, channel_id, message_id)", "Mensagem já recebida (idempotência)."
    )

    # ---------- enfileirar (chamado pelo webhook) ----------
    @api.model
    def _enqueue(self, channel, message_id, sender, text, payload_dict):
        """Persiste (dedupe). Retorna (record, created?). Duplicatas concorrentes
        são tratadas via savepoint + unique constraint."""
        Inbox = self.sudo()
        existing = Inbox.search(
            [("channel_id", "=", channel.id), ("message_id", "=", message_id)], limit=1
        )
        if existing:
            return existing, False
        try:
            with self.env.cr.savepoint():
                rec = Inbox.create(
                    {
                        "channel_id": channel.id,
                        "provider": channel.provider,
                        "message_id": message_id,
                        "sender": sender,
                        "text": text,
                        "payload": json.dumps(payload_dict or {})[:20000],
                        "status": "pending",
                        "next_attempt_at": fields.Datetime.now(),
                    }
                )
            return rec, True
        except Exception:  # noqa: BLE001 - corrida: outro request já inseriu
            dup = Inbox.search(
                [("channel_id", "=", channel.id), ("message_id", "=", message_id)], limit=1
            )
            return dup, False

    # ---------- worker (cron) ----------
    @api.model
    def _cron_process(self):
        """Reivindica lote pendente/devido com FOR UPDATE SKIP LOCKED e processa."""
        # clock_timestamp() = hora REAL (não o transaction_timestamp de now()),
        # correto para "está vencido agora" mesmo em transações longas (testes).
        self.env.cr.execute(
            """
            SELECT id FROM dz23_message_inbox
            WHERE status IN ('pending', 'failed')
              AND next_attempt_at <= (clock_timestamp() AT TIME ZONE 'utc')
            ORDER BY id LIMIT %s FOR UPDATE SKIP LOCKED
        """,
            (_BATCH,),
        )
        ids = [r[0] for r in self.env.cr.fetchall()]
        if not ids:
            return
        for rec in self.browse(ids):
            rec._process_one()

    def _process_one(self):
        self.ensure_one()
        try:
            with self.env.cr.savepoint():
                channel = self.channel_id
                payload = json.loads(self.payload or "{}")
                channel._processing_self().handle_inbound(self.sender, self.text, payload)
                self.write({"status": "done", "error": False})
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
                _logger.warning("Inbox %s -> DLQ após %s tentativas.", self.id, attempts)
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
