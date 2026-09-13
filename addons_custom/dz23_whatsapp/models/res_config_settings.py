from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    # Provedor ativo
    dz23_whatsapp_provider = fields.Selection(
        selection=[
            ("meta_cloud", "Meta WhatsApp Cloud API (oficial)"),
            ("twilio", "Twilio (BSP oficial)"),
            ("evolution", "Evolution API (não-oficial)"),
        ],
        string="Provedor de WhatsApp",
        default="meta_cloud",
        config_parameter="dz23.whatsapp_provider",
    )

    # Meta Cloud API
    dz23_wa_meta_phone_id = fields.Char(
        "Meta phone_number_id", config_parameter="dz23.whatsapp.meta_phone_id"
    )
    dz23_wa_meta_token = fields.Char(
        "Meta token (permanente)", config_parameter="dz23.whatsapp.meta_token"
    )
    dz23_wa_meta_version = fields.Char(
        "Meta API version", default="v20.0", config_parameter="dz23.whatsapp.meta_api_version"
    )
    dz23_wa_meta_app_secret = fields.Char(
        "Meta App Secret (validação do webhook)",
        config_parameter="dz23.whatsapp.meta_app_secret",
    )
    dz23_wa_webhook_verify_token = fields.Char(
        "Webhook verify token",
        config_parameter="dz23.whatsapp.webhook_verify_token",
    )

    # Twilio
    dz23_wa_twilio_sid = fields.Char(
        "Twilio Account SID", config_parameter="dz23.whatsapp.twilio_sid"
    )
    dz23_wa_twilio_token = fields.Char(
        "Twilio Auth Token", config_parameter="dz23.whatsapp.twilio_token"
    )
    dz23_wa_twilio_from = fields.Char(
        "Twilio número 'from'", config_parameter="dz23.whatsapp.twilio_from"
    )

    # Evolution API
    dz23_wa_evo_base = fields.Char(
        "Evolution base URL", config_parameter="dz23.whatsapp.evolution_base"
    )
    dz23_wa_evo_instance = fields.Char(
        "Evolution instância", config_parameter="dz23.whatsapp.evolution_instance"
    )
    dz23_wa_evo_apikey = fields.Char(
        "Evolution apikey", config_parameter="dz23.whatsapp.evolution_apikey"
    )

    # Privacidade e retenção (ADR-013) — por empresa
    dz23_message_retention_days = fields.Integer(
        related="company_id.dz23_message_retention_days", readonly=False
    )
    dz23_event_retention_days = fields.Integer(
        related="company_id.dz23_event_retention_days", readonly=False
    )
    dz23_access_log_retention_days = fields.Integer(
        related="company_id.dz23_access_log_retention_days", readonly=False
    )
    dz23_media_retention_days = fields.Integer(
        "Retenção de anexos recebidos (dias)",
        default=180,
        config_parameter="dz23.whatsapp.media_retention_days",
    )

    def action_dz23_evolution_connect(self):
        """Cria a instância Evolution e abre o assistente com o QR Code."""
        self.ensure_one()
        wiz = self.env["dz23.whatsapp.evolution"].create({})
        wiz._load_qr()
        return {
            "type": "ir.actions.act_window",
            "name": "Conectar WhatsApp (Evolution)",
            "res_model": "dz23.whatsapp.evolution",
            "res_id": wiz.id,
            "view_mode": "form",
            "target": "new",
        }
