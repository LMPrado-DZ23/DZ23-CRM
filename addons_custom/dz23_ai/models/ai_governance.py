# Governança de IA (Fase 8): configuração por empresa, circuit breaker por
# empresa/provedor e registro de uso (tokens, custo estimado, duração) — sem texto
# de conversa (nada de PII no registro de uso).
from datetime import timedelta

import psycopg2
from odoo import api, fields, models

from .ai_service import PROVIDERS

_BREAKER_FAILURES = 3
_BREAKER_COOLDOWN_SECONDS = 60
_BREAKER_MAX_COOLDOWN_SECONDS = 3600


class ResCompany(models.Model):
    _inherit = "res.company"

    dz23_ai_provider = fields.Selection(
        PROVIDERS, string="Provedor de IA da empresa", help="Vazio: usa o padrão global."
    )
    dz23_ai_model = fields.Char("Modelo de IA da empresa", help="Vazio: usa o padrão global.")
    dz23_ai_external_policy = fields.Selection(
        [
            ("inherit", "Usar o padrão global"),
            ("allow", "Permitir (consentimento/base legal registrados)"),
            ("deny", "Negar"),
        ],
        string="IA externa (LGPD)",
        default="inherit",
    )
    dz23_ai_daily_call_limit = fields.Integer(
        "Limite diário de chamadas de IA", default=0, help="0 = sem limite."
    )
    dz23_ai_monthly_cost_limit_usd = fields.Float(
        "Limite mensal de custo de IA (US$)", default=0.0, help="0 = sem limite."
    )


class DZ23AIBreaker(models.Model):
    _name = "dz23.ai.breaker"
    _description = "DZ23 — Circuit breaker de provedor de IA (por empresa)"
    _order = "company_id, provider"

    company_id = fields.Many2one(
        "res.company", "Empresa", required=True, ondelete="cascade", index=True
    )
    provider = fields.Char("Provedor", required=True)
    state = fields.Selection(
        [("closed", "Fechado (normal)"), ("open", "Aberto (bloqueado)"), ("half_open", "Teste")],
        string="Estado",
        default="closed",
        required=True,
    )
    failure_count = fields.Integer("Falhas seguidas", default=0)
    open_until = fields.Datetime("Bloqueado até")
    last_error = fields.Char("Último erro")
    last_failure_at = fields.Datetime("Última falha")
    last_success_at = fields.Datetime("Último sucesso")

    _breaker_uniq = models.Constraint(
        "unique(company_id, provider)", "Já existe breaker para esta empresa/provedor."
    )

    @api.model
    def _get(self, company, provider):
        Breaker = self.sudo()
        domain = [("company_id", "=", company.id), ("provider", "=", provider)]
        breaker = Breaker.search(domain, limit=1)
        if breaker:
            return breaker
        try:
            with self.env.cr.savepoint():
                return Breaker.create({"company_id": company.id, "provider": provider})
        except psycopg2.IntegrityError:
            return Breaker.search(domain, limit=1)

    def _lock(self):
        """Lock de linha (no cursor de governança): chamadas concorrentes não perdem
        contagem de falhas nem liberam vários testes ao mesmo tempo."""
        self.env.cr.execute("SELECT id FROM dz23_ai_breaker WHERE id = %s FOR UPDATE", (self.id,))
        self.invalidate_recordset()

    def _acquire(self):
        """True se a chamada pode sair. Não levanta: roda no cursor de governança, que
        precisa terminar com commit (ver `dz23.ai._governance`). Depois do cooldown, UMA
        chamada de teste (half-open) passa por janela; as outras esperam o resultado."""
        self.ensure_one()
        self._lock()
        if self.state == "closed":
            return True
        now = fields.Datetime.now()
        if self.open_until and self.open_until > now:
            return False
        self.write(
            {
                "state": "half_open",
                "open_until": now + timedelta(seconds=_BREAKER_COOLDOWN_SECONDS),
            }
        )
        return True

    def _record_failure(self, retry_after=None, error=None):
        self.ensure_one()
        self._lock()
        count = self.failure_count + 1
        now = fields.Datetime.now()
        vals = {"failure_count": count, "last_failure_at": now, "last_error": error or False}
        if retry_after or count >= _BREAKER_FAILURES or self.state == "half_open":
            backoff = _BREAKER_COOLDOWN_SECONDS * 2 ** max(0, count - _BREAKER_FAILURES)
            cooldown = min(_BREAKER_MAX_COOLDOWN_SECONDS, max(int(retry_after or 0), backoff))
            vals.update({"state": "open", "open_until": now + timedelta(seconds=cooldown)})
        self.write(vals)

    def _record_success(self):
        self.ensure_one()
        self._lock()
        if self.state != "closed" or self.failure_count:
            self.write({"state": "closed", "failure_count": 0, "open_until": False})
        self.last_success_at = fields.Datetime.now()


class DZ23AIUsage(models.Model):
    _name = "dz23.ai.usage"
    _description = "DZ23 — Uso de IA (tokens, custo, duração)"
    _order = "id desc"

    company_id = fields.Many2one(
        "res.company", "Empresa", required=True, ondelete="cascade", index=True
    )
    provider = fields.Char("Provedor", required=True, index=True)
    model_name = fields.Char("Modelo")
    purpose = fields.Char("Finalidade", index=True)
    status = fields.Selection(
        [
            ("ok", "Sucesso"),
            ("error", "Erro"),
            ("rate_limited", "Limitado (429)"),
            ("blocked", "Bloqueado por limite"),
        ],
        string="Resultado",
        required=True,
        index=True,
    )
    input_tokens = fields.Integer("Tokens de entrada")
    output_tokens = fields.Integer("Tokens de saída")
    cost_usd = fields.Float("Custo estimado (US$)", digits=(12, 6))
    duration_ms = fields.Integer("Duração (ms)")
    error = fields.Char("Erro")

    _company_date_idx = models.Index("(company_id, create_date)")

    @api.model
    def _log(self, company, provider, model, purpose, status, **values):
        return self.sudo().create(
            {
                "company_id": company.id,
                "provider": provider,
                "model_name": model,
                "purpose": purpose,
                "status": status,
                **values,
            }
        )

    @api.model
    def _cron_purge(self):
        """Retenção do registro de uso (sem texto de conversa)."""
        param = (
            self.env["ir.config_parameter"].sudo().get_param("dz23.ai.usage_retention_days", "400")
        )
        try:
            days = int(param or 0)
        except ValueError:
            days = 400
        if days <= 0:
            return 0
        cutoff = fields.Datetime.subtract(fields.Datetime.now(), days=days)
        old = self.sudo().search([("create_date", "<", cutoff)], limit=5000)
        count = len(old)
        old.unlink()
        return count
