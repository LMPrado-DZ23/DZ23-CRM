# Privacidade e retenção (Fase 10, ADR-013): prazos por empresa, anonimização de
# mensagens finalizadas, supressão de contato (opt-out / titular anonimizado) e
# auditoria de acesso append-only. Documentos fiscais (pedidos, faturas, pagamentos)
# NUNCA são tocados aqui.
import hashlib
import hmac
import logging

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.tools.translate import _

from .queue_utils import log_event, sanitize_error

_logger = logging.getLogger(__name__)

REMOVED = "[removido por retenção]"
ERASED = "[removido a pedido do titular]"
EXPORT_TAG = "dz23_privacy_export"
_BATCH = 1000
_EXPORT_TTL_DAYS = 7
ANON_PREFIX = "anon-"


def subject_hash(env, company_id, identifier):
    """Pseudônimo estável do titular POR EMPRESA (HMAC com o segredo do banco): permite
    honrar opt-out sem guardar o telefone e não correlaciona empresas diferentes."""
    secret = env["ir.config_parameter"].sudo().get_param("database.secret") or ""
    key = ("%s:%s" % (secret, company_id)).encode()
    digest = hmac.new(key, (identifier or "").encode(), hashlib.sha256).hexdigest()
    return ANON_PREFIX + digest[:32]


def _pseudonym(env, company_id, value):
    if not value or value.startswith(ANON_PREFIX):
        return value
    return subject_hash(env, company_id, value)


class ResCompanyPrivacy(models.Model):
    _inherit = "res.company"

    dz23_message_retention_days = fields.Integer(
        "Retenção de mensagens (dias)",
        default=365,
        help="Mensagens finalizadas têm texto, payload e telefone anonimizados após o "
        "prazo. 0 = não anonimizar automaticamente.",
    )
    dz23_event_retention_days = fields.Integer(
        "Retenção do payload de status (dias)",
        default=180,
        help="Prévia do payload e mensagem de erro dos eventos de status. 0 = manter.",
    )
    dz23_access_log_retention_days = fields.Integer(
        "Retenção da auditoria de acesso (dias)", default=730, help="0 = manter."
    )


class DZ23PrivacySuppression(models.Model):
    _name = "dz23.privacy.suppression"
    _description = "DZ23 — Supressão de contato (opt-out / titular anonimizado)"
    _order = "id desc"

    company_id = fields.Many2one(
        "res.company", required=True, index=True, ondelete="cascade", readonly=True
    )
    subject_hash = fields.Char("Pseudônimo", required=True, index=True, readonly=True)
    reason = fields.Selection(
        [("opt_out", "Opt-out"), ("erasure", "Titular anonimizado")], required=True, readonly=True
    )

    _subject_uniq = models.Constraint(
        "unique(company_id, subject_hash)", "Titular já suprimido nesta empresa."
    )

    @api.model
    def _is_suppressed(self, company, identifier):
        if not identifier:
            return False
        return bool(
            self.sudo().search_count(
                [
                    ("company_id", "=", company.id),
                    ("subject_hash", "=", subject_hash(self.env, company.id, identifier)),
                ],
                limit=1,
            )
        )

    @api.model
    def _add(self, company, identifier, reason):
        if not identifier:
            return self.browse()
        digest = subject_hash(self.env, company.id, identifier)
        Suppression = self.sudo()
        existing = Suppression.search(
            [("company_id", "=", company.id), ("subject_hash", "=", digest)], limit=1
        )
        return existing or Suppression.create(
            {"company_id": company.id, "subject_hash": digest, "reason": reason}
        )


ACCESS_ACTIONS = [
    ("view_conversation", "Abriu conversa"),
    ("view_message", "Abriu mensagem"),
    ("export_subject", "Exportou dados do titular"),
    ("anonymize_subject", "Anonimizou titular"),
    ("requeue", "Reenfileirou item da DLQ"),
    ("credential_change", "Alterou credencial do canal"),
]


class DZ23AccessLog(models.Model):
    _name = "dz23.access.log"
    _description = "DZ23 — Auditoria de acesso a dados pessoais (append-only)"
    _order = "id desc"

    user_id = fields.Many2one("res.users", "Usuário", required=True, index=True, readonly=True)
    company_id = fields.Many2one("res.company", required=True, index=True, readonly=True)
    action = fields.Selection(ACCESS_ACTIONS, "Ação", required=True, index=True, readonly=True)
    res_model = fields.Char("Modelo", readonly=True)
    res_ids = fields.Char("Registros", readonly=True)
    detail = fields.Char("Detalhe", readonly=True, help="Nunca contém conteúdo pessoal.")

    def write(self, vals):
        raise UserError(_("A auditoria de acesso é append-only."))

    def unlink(self):
        if not self.env.context.get("dz23_retention"):
            raise UserError(_("A auditoria de acesso só é removida pela política de retenção."))
        return super().unlink()

    @api.model
    def _log(self, action, records=None, detail=None, company=None):
        if company is None:
            company = self.env.company
            if records and "company_id" in records._fields and records[:1].company_id:
                company = records[:1].company_id
        return self.sudo().create(
            {
                "user_id": self.env.uid,
                "company_id": company.id,
                "action": action,
                "res_model": records._name if records else False,
                "res_ids": ",".join(str(i) for i in records.ids)[:200] if records else False,
                "detail": sanitize_error(detail) if detail else False,
            }
        )


class DZ23MessageInboxPrivacy(models.Model):
    _inherit = "dz23.message.inbox"

    anonymized = fields.Boolean("Anonimizada", readonly=True, index=True)

    def _dz23_anonymize(self, marker):
        for message in self:
            message.write(
                {
                    "text": marker,
                    "caption": False,
                    "payload": False,
                    "payload_preview": False,
                    "media_ref": False,
                    "reply_to": False,
                    "error": False,
                    "sender": _pseudonym(self.env, message.company_id.id, message.sender),
                    "anonymized": True,
                }
            )

    def web_read(self, specification):
        result = super().web_read(specification)
        if len(self) == 1 and not self.env.su:
            self.env["dz23.access.log"]._log("view_message", records=self)
        return result

    def action_requeue(self):
        result = super().action_requeue()
        self.env["dz23.access.log"]._log("requeue", records=self)
        return result


class DZ23MessageOutboxPrivacy(models.Model):
    _inherit = "dz23.message.outbox"

    anonymized = fields.Boolean("Anonimizada", readonly=True, index=True)

    def _dz23_anonymize(self, marker):
        for message in self:
            message.write(
                {
                    "body": marker,
                    "template_params": False,
                    "provider_error_message": False,
                    "error": False,
                    "recipient": _pseudonym(self.env, message.company_id.id, message.recipient),
                    "anonymized": True,
                }
            )

    def web_read(self, specification):
        result = super().web_read(specification)
        if len(self) == 1 and not self.env.su:
            self.env["dz23.access.log"]._log("view_message", records=self)
        return result

    def action_requeue(self):
        result = super().action_requeue()
        self.env["dz23.access.log"]._log("requeue", records=self)
        return result


class DZ23ConversationPrivacy(models.Model):
    _inherit = "dz23.conversation"

    def web_read(self, specification):
        result = super().web_read(specification)
        if len(self) == 1 and not self.env.su:
            self.env["dz23.access.log"]._log("view_conversation", records=self)
        return result

    def write(self, vals):
        result = super().write(vals)
        if vals.get("opt_out"):
            Suppression = self.env["dz23.privacy.suppression"]
            for conversation in self:
                Suppression._add(conversation.company_id, conversation.phone, "opt_out")
        return result


class DZ23PrivacyRetention(models.AbstractModel):
    _name = "dz23.privacy.retention"
    _description = "DZ23 — Política de retenção e anonimização"

    @api.model
    def _cron_apply(self):
        now = fields.Datetime.now()
        totals = {"inbox": 0, "outbox": 0, "events": 0, "access_logs": 0}
        for company in self.env["res.company"].sudo().search([]):
            for key, value in self._apply_company(company, now).items():
                totals[key] = totals.get(key, 0) + value
        exports = (
            self.env["ir.attachment"]
            .sudo()
            .search(
                [
                    ("description", "=", EXPORT_TAG),
                    ("create_date", "<", fields.Datetime.subtract(now, days=_EXPORT_TTL_DAYS)),
                ]
            )
        )
        totals["exports"] = len(exports)
        exports.unlink()
        log_event(_logger, "retention_applied", **totals)
        return totals

    def _apply_company(self, company, now):
        counts = {}
        Inbox = self.env["dz23.message.inbox"].sudo()
        Outbox = self.env["dz23.message.outbox"].sudo()
        if company.dz23_message_retention_days > 0:
            cutoff = fields.Datetime.subtract(now, days=company.dz23_message_retention_days)
            inbox = Inbox.search(
                [
                    ("company_id", "=", company.id),
                    ("anonymized", "=", False),
                    ("status", "in", ("done", "dead")),
                    ("received_at", "<", cutoff),
                ],
                limit=_BATCH,
            )
            inbox._dz23_anonymize(REMOVED)
            outbox = Outbox.search(
                [
                    ("company_id", "=", company.id),
                    ("anonymized", "=", False),
                    ("status", "in", ("sent", "dead")),
                    ("create_date", "<", cutoff),
                ],
                limit=_BATCH,
            )
            outbox._dz23_anonymize(REMOVED)
            counts.update(inbox=len(inbox), outbox=len(outbox))
            counts.update(self._retention_extra(company, cutoff))
        if company.dz23_event_retention_days > 0:
            cutoff = fields.Datetime.subtract(now, days=company.dz23_event_retention_days)
            events = (
                self.env["dz23.message.event"]
                .sudo()
                .search(
                    [
                        ("company_id", "=", company.id),
                        ("received_at", "<", cutoff),
                        "|",
                        ("payload_preview", "!=", False),
                        ("error_message", "!=", False),
                    ],
                    limit=_BATCH,
                )
            )
            events.with_context(dz23_retention=True).write(
                {"payload_preview": False, "error_message": False}
            )
            counts["events"] = len(events)
        if company.dz23_access_log_retention_days > 0:
            cutoff = fields.Datetime.subtract(now, days=company.dz23_access_log_retention_days)
            logs = (
                self.env["dz23.access.log"]
                .sudo()
                .search([("company_id", "=", company.id), ("create_date", "<", cutoff)])
            )
            counts["access_logs"] = len(logs)
            logs.with_context(dz23_retention=True).unlink()
        return counts

    def _retention_extra(self, company, cutoff):
        """Gancho para módulos com dados derivados (dz23_agent: chatter do robô)."""
        return {}
