# Mídia recebida copiada para o lead (dz23_agent): o vínculo com a cópia é guardado para
# que retenção e anonimização do titular apaguem o arquivo em TODOS os lugares.
from odoo import fields, models


class DZ23MessageMediaAgent(models.Model):
    _inherit = "dz23.message.media"

    lead_attachment_id = fields.Many2one(
        "ir.attachment", "Cópia no lead", ondelete="set null", readonly=True, copy=False
    )

    def _dz23_purge_files(self):
        self.lead_attachment_id.sudo().unlink()
        return super()._dz23_purge_files()
