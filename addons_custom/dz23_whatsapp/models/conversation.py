# Caixa de atendimento humano (ADR-010): uma conversa por contato de canal, com
# estados, responsável, equipe, prioridade, SLA de 1ª resposta, opt-out/bloqueio e
# trilha de auditoria (mail.thread). Notas internas ficam no chatter e NUNCA vão ao
# provedor: só o assistente de resposta enfileira mensagens na outbox.
import logging
from datetime import timedelta

import psycopg2
from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.tools.translate import _

_logger = logging.getLogger(__name__)

CONVERSATION_STATES = [
    ("open", "Aberta"),
    ("bot_active", "Robô atendendo"),
    ("human_active", "Atendimento humano"),
    ("waiting_customer", "Aguardando cliente"),
    ("waiting_internal", "Aguardando interno"),
    ("resolved", "Resolvida"),
    ("blocked", "Bloqueada"),
]
# O robô só responde nestes estados (humano no controle => robô em silêncio).
BOT_ALLOWED_STATES = ("open", "bot_active", "waiting_customer", "resolved")
_REOPEN_STATES = ("resolved", "waiting_customer")


class DZ23Conversation(models.Model):
    _name = "dz23.conversation"
    _description = "DZ23 — Conversa de atendimento"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "sla_breached desc, priority desc, last_customer_message_at desc, id desc"

    name = fields.Char(compute="_compute_name", store=True)
    contact_id = fields.Many2one(
        "dz23.channel.contact", required=True, ondelete="cascade", index=True, readonly=True
    )
    channel_id = fields.Many2one(related="contact_id.channel_id", store=True, index=True)
    company_id = fields.Many2one(related="contact_id.company_id", store=True, index=True)
    phone = fields.Char("Telefone", related="contact_id.provider_user_id", store=True, index=True)
    partner_id = fields.Many2one(related="contact_id.partner_id")
    state = fields.Selection(
        CONVERSATION_STATES, default="open", required=True, index=True, tracking=True
    )
    user_id = fields.Many2one("res.users", string="Responsável", index=True, tracking=True)
    team_id = fields.Many2one("crm.team", string="Equipe", index=True, tracking=True)
    priority = fields.Selection(
        [("0", "Normal"), ("1", "Alta"), ("2", "Urgente"), ("3", "Crítica")],
        default="0",
        tracking=True,
    )
    opt_out = fields.Boolean(
        "Não receber mensagens proativas",
        tracking=True,
        help="Bloqueia templates/mensagens proativas para este contato.",
    )
    blocked_reason = fields.Char("Motivo do bloqueio", tracking=True)
    last_customer_message_at = fields.Datetime(
        "Última mensagem do cliente", readonly=True, index=True
    )
    last_agent_message_at = fields.Datetime("Última resposta enviada", readonly=True)
    first_response_due_at = fields.Datetime("Responder até", readonly=True, index=True)
    sla_breached = fields.Boolean("SLA estourado", readonly=True, index=True)
    inbox_ids = fields.One2many("dz23.message.inbox", "conversation_id", string="Recebidas")
    outbox_ids = fields.One2many("dz23.message.outbox", "conversation_id", string="Enviadas")
    message_text_search = fields.Char(
        "Texto das mensagens",
        compute="_compute_message_text_search",
        search="_search_message_text",
    )

    _contact_uniq = models.Constraint(
        "unique(contact_id)", "Já existe uma conversa para este contato."
    )

    @api.depends(
        "contact_id.provider_user_id", "contact_id.partner_id.name", "contact_id.channel_id.name"
    )
    def _compute_name(self):
        for conversation in self:
            contact = conversation.contact_id
            who = contact.partner_id.name or contact.provider_user_id or _("Contato")
            conversation.name = "%s — %s" % (who, contact.channel_id.name or "")

    def _compute_message_text_search(self):
        self.message_text_search = False

    def _search_message_text(self, operator, value):
        if operator not in ("ilike", "like", "=ilike", "=like"):
            raise UserError(_("Busca por texto só aceita 'contém'."))
        inbox = self.env["dz23.message.inbox"].search(
            ["|", ("text", operator, value), ("caption", operator, value)]
        )
        outbox = self.env["dz23.message.outbox"].search([("body", operator, value)])
        return [("id", "in", (inbox.conversation_id | outbox.conversation_id).ids)]

    # ---------- obtenção ----------
    @api.model
    def _for_contact(self, contact):
        Conversation = self.sudo()
        conversation = Conversation.search([("contact_id", "=", contact.id)], limit=1)
        if conversation:
            return conversation
        vals = {
            "contact_id": contact.id,
            "state": "bot_active" if contact.channel_id.agent_autoreply else "open",
        }
        # Titular com opt-out ou anonimizado (ADR-013): nova conversa já nasce sem
        # mensagens proativas.
        if self.env["dz23.privacy.suppression"]._is_suppressed(
            contact.company_id, contact.provider_user_id
        ):
            vals["opt_out"] = True
        try:
            with self.env.cr.savepoint():
                return Conversation.create(vals)
        except psycopg2.IntegrityError:
            return Conversation.search([("contact_id", "=", contact.id)], limit=1)

    @api.model
    def _for_number(self, channel, number, create=True):
        from .whatsapp_channel import _e164_br

        e164 = _e164_br(number)
        if not e164:
            return self.browse()
        Contact = self.env["dz23.channel.contact"].sudo()
        contact = Contact.search(
            [("channel_id", "=", channel.id), ("provider_user_id", "=", e164)], limit=1
        )
        if not contact:
            if not create:
                return self.browse()
            contact = Contact._get_or_create(channel, e164, e164)
        if create:
            return self._for_contact(contact)
        return self.sudo().search([("contact_id", "=", contact.id)], limit=1)

    def write(self, vals):
        if "contact_id" in vals and any(
            conversation.contact_id.id != vals["contact_id"] for conversation in self
        ):
            raise UserError(
                _("O contato de uma conversa não pode ser alterado (isolamento entre empresas).")
            )
        return super().write(vals)

    # ---------- ciclo de vida ----------
    def bot_can_reply(self):
        self.ensure_one()
        return self.state in BOT_ALLOWED_STATES

    def _on_inbound(self, when=None):
        now = when or fields.Datetime.now()
        for conversation in self:
            vals = {"last_customer_message_at": now}
            if conversation.state == "waiting_customer" and conversation.user_id:
                # O atendente pediu algo ao cliente: a resposta volta para ele, não ao robô.
                vals["state"] = "human_active"
            elif conversation.state in _REOPEN_STATES:
                vals["state"] = "bot_active" if conversation.channel_id.agent_autoreply else "open"
            minutes = conversation.channel_id.sla_first_response_minutes or 0
            if minutes and not conversation.first_response_due_at:
                vals["first_response_due_at"] = now + timedelta(minutes=minutes)
            conversation.write(vals)

    def _on_outbound_sent(self, when=None):
        now = when or fields.Datetime.now()
        # Aguardando atendimento interno (ex.: robô transferiu): a mensagem automática de
        # transferência NÃO conta como primeira resposta — o SLA continua correndo.
        waiting = self.filtered(lambda conversation: conversation.state == "waiting_internal")
        waiting.write({"last_agent_message_at": now})
        (self - waiting).write(
            {
                "last_agent_message_at": now,
                "first_response_due_at": False,
                "sla_breached": False,
            }
        )

    @api.model
    def _cron_update_sla(self):
        overdue = self.sudo().search(
            [
                ("state", "not in", ("resolved", "blocked")),
                ("sla_breached", "=", False),
                ("first_response_due_at", "!=", False),
                ("first_response_due_at", "<", fields.Datetime.now()),
            ]
        )
        overdue.write({"sla_breached": True})
        return len(overdue)

    def _note(self, body):
        for conversation in self:
            conversation.message_post(body=body, subtype_xmlid="mail.mt_note")

    # ---------- ações do atendente ----------
    def action_take(self):
        for conversation in self:
            if conversation.state == "blocked":
                raise UserError(_("Conversa bloqueada: desbloqueie antes de assumir."))
            conversation.write({"state": "human_active", "user_id": self.env.user.id})
            conversation._note(_("%s assumiu o atendimento (robô pausado).") % self.env.user.name)
        return True

    def action_release_to_bot(self):
        if any(not conversation.channel_id.agent_autoreply for conversation in self):
            raise UserError(
                _("A auto-resposta está desligada neste canal: ninguém responderia o cliente.")
            )
        self.write({"state": "bot_active"})
        self._note(_("Atendimento devolvido ao robô."))
        return True

    def action_wait_customer(self):
        self.write({"state": "waiting_customer"})
        return True

    def action_wait_internal(self):
        self.write({"state": "waiting_internal"})
        return True

    def action_resolve(self):
        self.write({"state": "resolved", "first_response_due_at": False, "sla_breached": False})
        self._note(_("Conversa resolvida."))
        return True

    def action_block(self):
        # Bloquear já impede qualquer envio; NÃO marca opt-out (isso é pedido do cliente
        # e sobreviveria ao desbloqueio).
        self.write({"state": "blocked", "first_response_due_at": False, "sla_breached": False})
        self._note(_("Contato bloqueado: sem respostas automáticas e sem envios."))
        return True

    def action_unblock(self):
        self.write({"state": "open", "blocked_reason": False})
        self._note(_("Contato desbloqueado."))
        return True

    def action_open_reply(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Responder cliente"),
            "res_model": "dz23.conversation.reply",
            "view_mode": "form",
            "target": "new",
            "context": {"default_conversation_id": self.id},
        }

    def action_open_transfer(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Transferir conversa"),
            "res_model": "dz23.conversation.transfer",
            "view_mode": "form",
            "target": "new",
            "context": {"default_conversation_id": self.id},
        }

    def _send_human(self, body=None, template=None, params=None):
        """Mensagem do atendente ao cliente: SEMPRE pela outbox (retry, DLQ, status)."""
        self.ensure_one()
        if self.state == "blocked":
            raise UserError(_("Conversa bloqueada: envio não permitido."))
        outbox = (
            self.env["dz23.message.outbox"]
            .sudo()
            ._enqueue(self.channel_id, self.phone, body, template=template, params=params)
        )
        if not outbox:
            raise UserError(_("Escreva a mensagem ou escolha um template."))
        vals = {}
        if self.state in ("open", "bot_active", "waiting_internal", "resolved"):
            vals["state"] = "human_active"
        if not self.user_id:
            vals["user_id"] = self.env.user.id
        if vals:
            self.write(vals)
        self._note(_("Mensagem enfileirada para o cliente: %s") % outbox.body)
        return outbox


class DZ23ConversationReply(models.TransientModel):
    _name = "dz23.conversation.reply"
    _description = "DZ23 — Responder conversa"

    conversation_id = fields.Many2one("dz23.conversation", required=True, ondelete="cascade")
    channel_id = fields.Many2one(related="conversation_id.channel_id")
    window_open = fields.Boolean("Janela de 24 h aberta", compute="_compute_window_open")
    body = fields.Text("Mensagem")
    template_id = fields.Many2one(
        "dz23.message.template",
        domain="[('channel_id', '=', channel_id), ('status', '=', 'approved')]",
    )
    template_params = fields.Char("Variáveis do template", help="Separe as variáveis com |")

    @api.depends("conversation_id")
    def _compute_window_open(self):
        for wizard in self:
            conversation = wizard.conversation_id.sudo()
            wizard.window_open = bool(
                conversation and conversation.channel_id._service_window_open(conversation.phone)
            )

    def action_send(self):
        self.ensure_one()
        params = (
            [p.strip() for p in self.template_params.split("|")] if self.template_params else []
        )
        if not self.template_id and not self.window_open:
            raise UserError(_("Fora da janela de 24 h do WhatsApp: escolha um template aprovado."))
        if self.template_id:
            if self.conversation_id.sudo().opt_out:
                raise UserError(
                    _(
                        "O cliente pediu para não receber mensagens proativas (opt-out): "
                        "templates não podem ser enviados. Aguarde o cliente escrever."
                    )
                )
            expected = self.template_id.variable_count
            if len(params) != expected:
                raise UserError(
                    _("O template exige %(expected)s variável(is); você informou %(given)s.")
                    % {"expected": expected, "given": len(params)}
                )
        self.conversation_id._send_human(
            body=None if self.template_id else self.body,
            template=self.template_id or None,
            params=params,
        )
        return {"type": "ir.actions.act_window_close"}


class DZ23ConversationTransfer(models.TransientModel):
    _name = "dz23.conversation.transfer"
    _description = "DZ23 — Transferir conversa"

    conversation_id = fields.Many2one("dz23.conversation", required=True, ondelete="cascade")
    team_id = fields.Many2one("crm.team", string="Equipe")
    user_id = fields.Many2one(
        "res.users", string="Responsável", domain=lambda self: self._domain_attendants()
    )
    note = fields.Text("Nota interna")

    @api.model
    def _domain_attendants(self):
        group = self.env.ref("dz23_whatsapp.group_dz23_attendant", raise_if_not_found=False)
        return [("share", "=", False)] + ([("all_group_ids", "in", group.id)] if group else [])

    def action_transfer(self):
        self.ensure_one()
        conversation = self.conversation_id
        user = self.user_id
        if user and (
            conversation.company_id not in user.company_ids
            or not user.has_group("dz23_whatsapp.group_dz23_attendant")
        ):
            raise UserError(
                _("Transfira apenas para atendentes com acesso à empresa desta conversa.")
            )
        conversation.write(
            {
                "team_id": (self.team_id or conversation.team_id).id,
                "user_id": self.user_id.id or False,
                "state": "human_active" if self.user_id else "waiting_internal",
            }
        )
        target = self.user_id.name or self.team_id.name or _("fila da equipe")
        conversation._note(_("Conversa transferida para %s.") % target)
        if self.note:
            conversation._note(self.note)
        return {"type": "ir.actions.act_window_close"}
