# Conversa de atendimento ligada ao CRM: lead do contato, busca por pedido de venda e
# transferência do robô para humano (ADR-011).
from datetime import timedelta

from odoo import api, fields, models
from odoo.tools.translate import _

HANDOFF_REASONS = [
    ("sensitive", "Assunto sensível ou pedido de atendente"),
    ("bot_limit", "Limite de respostas do robô"),
    ("ai_unavailable", "IA indisponível"),
]


class DZ23ConversationCrm(models.Model):
    _inherit = "dz23.conversation"

    # Lead e pedidos só para quem tem acesso a Vendas: o atendente puro abre a conversa
    # sem erro de permissão (auditoria C-1).
    lead_id = fields.Many2one(
        related="contact_id.lead_id",
        store=True,
        index=True,
        string="Lead",
        groups="sales_team.group_sale_salesman",
    )
    bot_turns = fields.Integer("Respostas livres seguidas do robô", default=0, readonly=True)
    handoff_reason = fields.Selection(
        HANDOFF_REASONS, string="Transferida pelo robô", readonly=True, tracking=True
    )
    handoff_at = fields.Datetime("Transferida em", readonly=True)

    def _agent_handoff(self, reason):
        """Robô passa a conversa para um humano: fica em silêncio até alguém devolver.
        O SLA de 1ª resposta humana começa agora e o responsável recebe uma atividade."""
        label = dict(HANDOFF_REASONS)[reason]
        now = fields.Datetime.now()
        for conversation in self:
            vals = {
                "state": "waiting_internal",
                "handoff_reason": reason,
                "handoff_at": now,
                "bot_turns": 0,
            }
            minutes = conversation.channel_id.sla_first_response_minutes or 0
            if minutes:
                vals.update(first_response_due_at=now + timedelta(minutes=minutes))
                vals.update(sla_breached=False)
            if reason == "sensitive" and conversation.priority == "0":
                vals["priority"] = "1"
            conversation.write(vals)
            conversation._note(_("Robô transferiu para atendimento humano: %s.") % label)
            responsible = conversation.user_id or conversation.channel_id.agenda_user_id
            if responsible:
                conversation.activity_schedule(
                    "mail.mail_activity_data_todo",
                    summary=_("Atender conversa transferida pelo robô"),
                    note=label,
                    user_id=responsible.id,
                )
        return True

    def action_take(self):
        result = super().action_take()
        self.write({"bot_turns": 0})
        return result

    def action_release_to_bot(self):
        result = super().action_release_to_bot()
        self.write({"bot_turns": 0, "handoff_reason": False, "handoff_at": False})
        return result

    sale_order_ids = fields.Many2many(
        "sale.order",
        string="Pedidos",
        compute="_compute_sale_order_ids",
        search="_search_sale_order_ids",
        groups="sales_team.group_sale_salesman",
    )

    @api.depends("lead_id.partner_id")
    def _compute_sale_order_ids(self):
        Order = self.env["sale.order"]
        for conversation in self:
            partner = conversation.sudo().lead_id.partner_id
            conversation.sale_order_ids = (
                Order.search(
                    [
                        ("partner_id", "=", partner.id),
                        ("company_id", "=", conversation.company_id.id),
                    ]
                )
                if partner
                else Order
            )

    def _search_sale_order_ids(self, operator, value):
        """Busca conversas por pedido. No Odoo 19 um `ilike` em relacional chega como
        `any` com um domínio; também aceita nome (texto) ou ids."""
        negative = {
            "not any": "any",
            "not in": "in",
            "!=": "=",
            "not ilike": "ilike",
            "not like": "like",
        }
        positive = negative.get(operator, operator)
        Order = self.env["sale.order"]
        if positive == "any":
            orders = Order.search(value)
        elif isinstance(value, str):
            orders = Order.search([("name", positive, value)])
        else:
            ids = value if isinstance(value, (list, tuple)) else [value]
            orders = Order.browse([i for i in ids if i])
        partners = orders.partner_id.ids
        return [("lead_id.partner_id", "not in" if operator in negative else "in", partners)]
