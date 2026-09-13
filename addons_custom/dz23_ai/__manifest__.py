# DZ23 CRM — IA integrada (texto + visão), com opção GRÁTIS/local (Ollama)
# e provedores free-tier/pagos, COM GOVERNANÇA: configuração e limites por
# empresa, consentimento LGPD, circuit breaker, timeout e registro de custo.
# Chaves ficam em Ajustes (servidor), nunca no código.
{
    "name": "DZ23 CRM — IA",
    "version": "19.0.2.0.0",
    "summary": "IA integrada com governança: local grátis (Ollama), Groq/Gemini, OpenAI/Anthropic.",
    "author": "DZ23 (LEANDRO MARCOS PRADO LTDA)",
    "website": "https://www.dz23.com.br",
    "license": "Other OSI approved licence",
    "category": "Productivity",
    "depends": ["base", "mail", "crm"],
    "data": [
        "security/ir.model.access.csv",
        "security/ai_rules.xml",
        "data/config_params.xml",
        "data/ai_usage_cron.xml",
        "views/res_config_settings_views.xml",
        "views/crm_lead_views.xml",
        "views/ai_governance_views.xml",
    ],
    "installable": True,
    "application": False,
}
