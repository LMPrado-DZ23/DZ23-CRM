# Origem rastreável dos pedidos abertos pelo atendimento de WhatsApp.
from odoo import fields, models


class SaleOrder(models.Model):
    _inherit = "sale.order"

    dz23_channel_id = fields.Many2one(
        "dz23.channel", string="Canal de origem", readonly=True, copy=False, index=True
    )
    dz23_source_correlation_id = fields.Char(
        "Mensagem de origem", readonly=True, copy=False, index=True
    )
