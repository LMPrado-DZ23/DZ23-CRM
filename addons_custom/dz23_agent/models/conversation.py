# Conversa de atendimento ligada ao CRM: lead do contato e busca por pedido de venda.
from odoo import api, fields, models


class DZ23ConversationCrm(models.Model):
    _inherit = "dz23.conversation"

    lead_id = fields.Many2one(related="contact_id.lead_id", store=True, index=True, string="Lead")
    sale_order_ids = fields.Many2many(
        "sale.order",
        string="Pedidos",
        compute="_compute_sale_order_ids",
        search="_search_sale_order_ids",
    )

    @api.depends("lead_id.partner_id")
    def _compute_sale_order_ids(self):
        Order = self.env["sale.order"]
        for conversation in self:
            partner = conversation.lead_id.partner_id
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
