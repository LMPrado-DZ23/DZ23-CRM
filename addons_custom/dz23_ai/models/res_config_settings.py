from odoo import fields, models

from .ai_service import PROVIDERS


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    # ----- padrão global (usado quando a empresa não define o seu) -----
    dz23_ai_provider = fields.Selection(
        selection=PROVIDERS,
        string="Provedor de IA",
        default="ollama",
        config_parameter="dz23.ai_provider",
    )
    dz23_ai_model = fields.Char(
        "Modelo",
        help="Ex.: llama3.1 (Ollama), llama-3.3-70b-versatile (Groq), gpt-4o-mini…",
        config_parameter="dz23.ai_model",
    )
    # Gate de privacidade (LGPD): provedores EXTERNOS só com consentimento.
    dz23_ai_external_allowed = fields.Boolean(
        "Permitir enviar dados a IA EXTERNA (consentimento/LGPD)",
        help="Ao ligar, você confirma ter base legal para enviar dados (com PII "
        "redigida) a provedores externos. Deixe desligado para usar só IA local.",
        config_parameter="dz23.ai.external_allowed",
    )
    dz23_ai_ollama_base = fields.Char(
        "Ollama base URL (local, grátis)",
        default="http://host.docker.internal:11434",
        config_parameter="dz23.ai.ollama_base",
    )
    dz23_ai_groq_key = fields.Char("Groq API key", config_parameter="dz23.ai.groq_key")
    dz23_ai_google_key = fields.Char(
        "Google (Gemini) API key", config_parameter="dz23.ai.google_key"
    )
    dz23_ai_openai_key = fields.Char("OpenAI API key", config_parameter="dz23.ai.openai_key")
    dz23_ai_anthropic_key = fields.Char(
        "Anthropic API key", config_parameter="dz23.ai.anthropic_key"
    )

    # ----- por empresa (Fase 8) -----
    dz23_company_ai_provider = fields.Selection(
        related="company_id.dz23_ai_provider", readonly=False
    )
    dz23_company_ai_model = fields.Char(related="company_id.dz23_ai_model", readonly=False)
    dz23_company_ai_external_policy = fields.Selection(
        related="company_id.dz23_ai_external_policy", readonly=False
    )
    dz23_company_ai_daily_call_limit = fields.Integer(
        related="company_id.dz23_ai_daily_call_limit", readonly=False
    )
    dz23_company_ai_monthly_cost_limit_usd = fields.Float(
        related="company_id.dz23_ai_monthly_cost_limit_usd", readonly=False
    )
