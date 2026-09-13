# DZ23 CRM — Agente WhatsApp com IA: responde mensagens recebidas e agenda.
# Recebe (Meta ou Evolution) -> IA responde -> envia de volta; se detectar
# intenção de horário, cria evento na Agenda e confirma. Provedor-agnóstico.
{
    "name": "DZ23 CRM — Agente IA (WhatsApp + Agenda)",
    "version": "19.0.6.0.0",
    "summary": "Auto-resposta de WhatsApp por IA + agendamento automático na Agenda.",
    "author": "DZ23 (LEANDRO MARCOS PRADO LTDA)",
    "website": "https://www.dz23.com.br",
    "license": "Other OSI approved licence",
    "category": "Marketing",
    "depends": [
        "dz23_whatsapp",
        "dz23_ai",
        "crm",
        "calendar",
        "phone_validation",
        "resource",
        "sale_management",
    ],
    "data": [
        "security/ir.model.access.csv",
        "security/dz23_agent_rules.xml",
        "data/config_params.xml",
        "data/ai_request_cron.xml",
        "views/res_config_settings_views.xml",
        "views/dz23_channel_agenda_views.xml",
        "views/ai_request_views.xml",
        "views/conversation_agent_views.xml",
        "views/metrics_agent_views.xml",
    ],
    "installable": True,
    "application": False,
}
