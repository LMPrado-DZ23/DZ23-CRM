# Identidade de conversa POR CANAL: liga o identificador do usuário no provedor
# (provider_user_id, ex. o número E.164 no WhatsApp) a um lead/parceiro, SEMPRE
# no escopo do canal e da empresa. Substitui a busca global por cauda de telefone.
from odoo import api, fields, models


class DZ23ChannelContact(models.Model):
    _name = "dz23.channel.contact"
    _description = "DZ23 — Identidade de contato por canal de mensageria"
    _rec_name = "provider_user_id"

    channel_id = fields.Many2one("dz23.channel", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one(
        related="channel_id.company_id", store=True, index=True, readonly=True
    )
    provider_user_id = fields.Char(
        required=True, index=True, help="Identificador do usuário no provedor (E.164 no WhatsApp)."
    )
    phone_e164 = fields.Char(index=True)
    partner_id = fields.Many2one("res.partner", check_company=True, index=True)
    last_inbound_at = fields.Datetime(
        "Última mensagem do cliente",
        readonly=True,
        index=True,
        help="Abre a janela de atendimento de 24 h dos provedores oficiais (ADR-009).",
    )
    # lead_id é adicionado por dz23_agent (que depende de crm) via _inherit.

    _provider_user_uniq = models.Constraint(
        "unique(channel_id, provider_user_id)",
        "Já existe um contato com esse identificador neste canal.",
    )

    @api.model
    def _get_or_create(self, channel, provider_user_id, phone_e164=None):
        """Acha/cria a identidade escopada ao canal (nunca busca global)."""
        rec = self.search(
            [("channel_id", "=", channel.id), ("provider_user_id", "=", provider_user_id)], limit=1
        )
        if rec:
            return rec
        return self.create(
            {
                "channel_id": channel.id,
                "provider_user_id": provider_user_id,
                "phone_e164": phone_e164 or provider_user_id,
            }
        )

    @api.model
    def _touch_inbound(self, channel, number, when=None):
        """Registra a última mensagem recebida do cliente (janela de 24 h)."""
        from .whatsapp_channel import _e164_br

        e164 = _e164_br(number)
        if not e164:
            return self.browse()
        contact = self.sudo()._get_or_create(channel, e164, e164)
        when = when or fields.Datetime.now()
        if not contact.last_inbound_at or when > contact.last_inbound_at:
            contact.last_inbound_at = when
        return contact
