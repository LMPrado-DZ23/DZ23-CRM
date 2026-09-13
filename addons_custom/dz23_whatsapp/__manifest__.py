# DZ23 CRM — WhatsApp plugável (adaptador por provedor).
# Provedores: Meta WhatsApp Cloud API, Twilio, Evolution API.
# Credenciais ficam no dz23.channel (restritas a admin), NUNCA no código.
{
    "name": "DZ23 CRM — WhatsApp",
    "version": "19.0.11.0.0",
    "summary": "WhatsApp multi-tenant por canal (Meta Cloud API / Twilio / Evolution).",
    "author": "DZ23 (LEANDRO MARCOS PRADO LTDA)",
    "website": "https://www.dz23.com.br",
    "license": "Other OSI approved licence",
    "category": "Marketing",
    "depends": ["mail", "phone_validation", "sales_team"],
    "data": [
        "security/ir.model.access.csv",
        "security/dz23_channel_rules.xml",
        "data/config_params.xml",
        "data/inbox_cron.xml",
        "data/outbox_cron.xml",
        "data/event_cron.xml",
        "views/dz23_channel_views.xml",
        "views/message_queue_views.xml",
        "views/business_action_views.xml",
        "views/res_config_settings_views.xml",
        "wizard/whatsapp_compose_views.xml",
        "wizard/evolution_connect_views.xml",
    ],
    "installable": True,
    "application": False,
}
