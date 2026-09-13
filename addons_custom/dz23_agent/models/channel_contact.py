# Identidade de canal (dz23.channel.contact) ganha o vínculo com crm.lead e o
# ESTADO da conversa de compra em andamento (resumo apresentado aguardando
# confirmação explícita — ADR-005). Fica aqui porque dz23_agent depende de crm/sale.
from odoo import fields, models


class DZ23ChannelContact(models.Model):
    _inherit = "dz23.channel.contact"

    lead_id = fields.Many2one("crm.lead", check_company=True, index=True)
    agent_pending_action = fields.Selection(
        [("buy", "Compra aguardando confirmação")], readonly=True
    )
    agent_pending_product_id = fields.Many2one("product.product", readonly=True)
    agent_pending_qty = fields.Float(readonly=True, digits="Product Unit")
    # Referência (nonce) do resumo apresentado: compõe a chave idempotente do orçamento.
    agent_pending_ref = fields.Char(readonly=True, copy=False)
    agent_pending_expires_at = fields.Datetime(readonly=True)

    def _agent_clear_pending(self):
        self.write(
            {
                "agent_pending_action": False,
                "agent_pending_product_id": False,
                "agent_pending_qty": 0.0,
                "agent_pending_ref": False,
                "agent_pending_expires_at": False,
            }
        )

    def _agent_pending_buy(self):
        """Compra pendente ainda válida (não expirada) ou False."""
        self.ensure_one()
        if (
            self.agent_pending_action == "buy"
            and self.agent_pending_product_id
            and self.agent_pending_expires_at
            and self.agent_pending_expires_at > fields.Datetime.now()
        ):
            return True
        return False
