from odoo import fields, models
from odoo.exceptions import UserError
from odoo.tools.translate import _


class DZ23WhatsAppCompose(models.TransientModel):
    _name = "dz23.whatsapp.compose"
    _description = "DZ23 — Enviar WhatsApp"

    number = fields.Char("Número (WhatsApp)", required=True)
    body = fields.Text("Mensagem", required=True)
    res_model = fields.Char()
    res_id = fields.Integer()

    def action_send(self):
        """Envio manual pelo canal padrão da empresa, SEMPRE pela outbox (retry, DLQ,
        status, janela de 24 h) e registrado na conversa de atendimento (ADR-010)."""
        self.ensure_one()
        channel = self.env["dz23.whatsapp"]._default_channel()
        if not channel:
            raise UserError(_("Nenhum canal de WhatsApp configurado para esta empresa."))
        if not channel.sudo()._service_window_open(self.number):
            raise UserError(
                _(
                    "Fora da janela de 24 h do WhatsApp: responda pela conversa de "
                    "atendimento usando um template aprovado."
                )
            )
        conversation = self.env["dz23.conversation"].sudo()._for_number(channel, self.number)
        if not conversation:
            raise UserError(_("Número de WhatsApp inválido."))
        conversation._send_human(body=self.body)
        if self.res_model and self.res_id and self.res_model in self.env.registry:
            rec = self.env[self.res_model].browse(self.res_id)
            if rec.exists() and hasattr(rec, "message_post"):
                rec.message_post(
                    body=_("WhatsApp enfileirado para %(number)s: %(body)s")
                    % {"number": self.number, "body": self.body}
                )
        return {"type": "ir.actions.act_window_close"}
