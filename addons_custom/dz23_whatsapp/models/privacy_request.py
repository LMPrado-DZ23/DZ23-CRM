# Pedido do titular (Fase 10, ADR-013): exportar (JSON privado, expira em 7 dias) ou
# anonimizar os dados de WhatsApp/atendimento de um telefone NA EMPRESA escolhida.
# Pedidos, faturas e pagamentos permanecem (obrigação fiscal); o titular anonimizado
# fica suprimido (novas conversas nascem com opt-out).
import json

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.tools.translate import _

from .privacy import ERASED, EXPORT_TAG, subject_hash
from .queue_utils import sanitize_error

_PENDING_OUTBOX = ("pending", "sending", "failed")


def _dt(value):
    return fields.Datetime.to_string(value) if value else None


class DZ23PrivacyRequest(models.TransientModel):
    _name = "dz23.privacy.request"
    _description = "DZ23 — Pedido do titular (exportar / anonimizar)"

    company_id = fields.Many2one(
        "res.company",
        "Empresa",
        required=True,
        default=lambda self: self.env.company,
        domain="[('id', 'in', allowed_company_ids)]",
    )
    phone = fields.Char("Telefone do titular", required=True)
    reason = fields.Text("Motivo / protocolo")
    contact_count = fields.Integer("Contatos encontrados", compute="_compute_contact_count")

    # ---------- localização ----------
    def _identifiers(self):
        from .whatsapp_channel import _digits, _e164_br

        self.ensure_one()
        return {value for value in (_e164_br(self.phone), _digits(self.phone)) if value}

    def _contacts(self):
        self.ensure_one()
        identifiers = self._identifiers()
        Contact = self.env["dz23.channel.contact"].sudo()
        if not identifiers or not self.company_id:
            return Contact
        return Contact.search(
            [
                ("company_id", "=", self.company_id.id),
                ("provider_user_id", "in", list(identifiers)),
            ]
        )

    @api.depends("phone", "company_id")
    def _compute_contact_count(self):
        for request in self:
            request.contact_count = len(request._contacts()) if request.phone else 0

    def _check_subject_request_access(self):
        self.ensure_one()
        user = self.env.user
        if not (user.has_group("dz23_whatsapp.group_dz23_supervisor") or self.env.is_system()):
            raise UserError(_("Somente supervisores de atendimento atendem pedidos do titular."))
        if self.company_id not in user.company_ids:
            raise UserError(_("Você não tem acesso a esta empresa."))

    def _messages(self, contact):
        keys = list(self._identifiers() | {contact.provider_user_id})
        channel = [("channel_id", "=", contact.channel_id.id)]
        inbox = self.env["dz23.message.inbox"].sudo().search(channel + [("sender", "in", keys)])
        outbox = (
            self.env["dz23.message.outbox"].sudo().search(channel + [("recipient", "in", keys)])
        )
        return inbox, outbox

    # ---------- exportação ----------
    def _collect(self, contacts):
        Conversation = self.env["dz23.conversation"].sudo()
        Media = self.env["dz23.message.media"].sudo()
        data = {
            "generated_at": _dt(fields.Datetime.now()),
            "company": self.company_id.name,
            "subject_phone": self.phone,
            "contacts": [],
        }
        for contact in contacts:
            inbox, outbox = self._messages(contact)
            conversation = Conversation.search([("contact_id", "=", contact.id)], limit=1)
            media = Media.search([("inbox_id", "in", inbox.ids)])
            data["contacts"].append(
                {
                    "channel": contact.channel_id.name,
                    "provider": contact.channel_id.provider,
                    "identifier": contact.provider_user_id,
                    "last_inbound_at": _dt(contact.last_inbound_at),
                    "conversation": {
                        "state": conversation.state,
                        "opt_out": conversation.opt_out,
                        "created_at": _dt(conversation.create_date),
                    }
                    if conversation
                    else None,
                    "received": [
                        {
                            "received_at": _dt(message.received_at),
                            "type": message.message_type,
                            "text": message.text or "",
                            "caption": message.caption or "",
                            "status": message.status,
                        }
                        for message in inbox
                    ],
                    "sent": [
                        {
                            "created_at": _dt(message.create_date),
                            "text": message.body,
                            "status": message.current_status,
                            "sent_at": _dt(message.sent_at),
                            "delivered_at": _dt(message.delivered_at),
                            "read_at": _dt(message.read_at),
                        }
                        for message in outbox
                    ],
                    "media": [
                        {
                            "filename": item.filename,
                            "size": item.file_size,
                            "status": item.status,
                            "downloaded_at": _dt(item.downloaded_at),
                        }
                        for item in media
                    ],
                }
            )
        self._collect_extra(contacts, data)
        return data

    def _collect_extra(self, contacts, data):
        """Gancho: dz23_agent acrescenta leads, pedidos e pedidos de IA."""
        return data

    def action_export(self):
        self.ensure_one()
        self._check_subject_request_access()
        contacts = self._contacts()
        if not contacts:
            raise UserError(_("Nenhum contato com este telefone nesta empresa."))
        payload = json.dumps(
            self._collect(contacts), ensure_ascii=False, indent=2, sort_keys=True, default=str
        )
        attachment = self.env["ir.attachment"].create(
            {
                "name": "titular-%s.json" % fields.Date.today(),
                "raw": payload.encode("utf-8"),
                "mimetype": "application/json",
                "description": EXPORT_TAG,
                "public": False,
            }
        )
        self.env["dz23.access.log"]._log(
            "export_subject",
            records=contacts,
            detail="contatos=%s" % len(contacts),
            company=self.company_id,
        )
        return {
            "type": "ir.actions.act_url",
            "url": "/web/content/%s?download=true" % attachment.id,
            "target": "self",
        }

    # ---------- anonimização ----------
    def _anonymize_extra(self, contact):
        """Gancho: dz23_agent anonimiza lead e textos enviados à IA."""
        return True

    def action_anonymize(self):
        self.ensure_one()
        self._check_subject_request_access()
        if not (self.reason or "").strip():
            raise UserError(_("Informe o motivo ou protocolo do pedido do titular."))
        contacts = self._contacts()
        if not contacts:
            raise UserError(_("Nenhum contato com este telefone nesta empresa."))
        Conversation = self.env["dz23.conversation"].sudo()
        Media = self.env["dz23.message.media"].sudo()
        Event = self.env["dz23.message.event"].sudo()
        Suppression = self.env["dz23.privacy.suppression"]
        identifiers = self._identifiers()
        erased_body = "<p>%s</p>" % ERASED
        for contact in contacts:
            inbox, outbox = self._messages(contact)
            outbox.filtered(lambda message: message.status in _PENDING_OUTBOX).write(
                {"status": "dead", "error": _("Cancelado: titular anonimizado.")}
            )
            inbox._dz23_anonymize(ERASED)
            outbox._dz23_anonymize(ERASED)
            Media.search([("inbox_id", "in", inbox.ids)])._dz23_purge_files()
            for identifier in identifiers | {contact.provider_user_id}:
                Suppression._add(self.company_id, identifier, "erasure")
            events = Event.search(
                ["|", ("outbox_id", "in", outbox.ids), ("inbox_id", "in", inbox.ids)]
            )
            events.with_context(dz23_retention=True).write(
                {"payload_preview": False, "error_message": False}
            )
            conversation = Conversation.search([("contact_id", "=", contact.id)], limit=1)
            if conversation:
                conversation.message_ids.filtered("body").write({"body": erased_body})
                self.env["mail.tracking.value"].sudo().search(
                    [("mail_message_id", "in", conversation.message_ids.ids)]
                ).unlink()
                conversation.with_context(tracking_disable=True).write(
                    {
                        "opt_out": True,
                        "state": "blocked",
                        "blocked_reason": _("Titular anonimizado (LGPD)"),
                    }
                )
            self._anonymize_extra(contact)
            contact.write(
                {
                    "provider_user_id": subject_hash(
                        self.env, self.company_id.id, contact.provider_user_id
                    ),
                    "phone_e164": False,
                }
            )
            if conversation:
                conversation._note(
                    _("Dados do titular anonimizados. Motivo: %s") % sanitize_error(self.reason)
                )
        self.env["dz23.access.log"]._log(
            "anonymize_subject",
            records=contacts,
            detail="contatos=%s" % len(contacts),
            company=self.company_id,
        )
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Titular anonimizado"),
                "message": _("Textos e identificação removidos; documentos fiscais mantidos."),
                "type": "success",
                "next": {"type": "ir.actions.act_window_close"},
            },
        }
