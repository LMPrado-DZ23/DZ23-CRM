# Templates oficiais por canal (ADR-009). Mensagem fora da janela de 24 h (Meta/
# Twilio) só pode sair como template APROVADO pelo provedor; o CRM nunca tenta
# contornar essa política. Meta: sincronização via Graph API.
import logging
import re

import requests
from odoo import api, fields, models
from odoo.tools.translate import _

from .provider_errors import ProviderPermanentError, ProviderTransientError, classify_http_error

_logger = logging.getLogger(__name__)
_PLACEHOLDER_RE = re.compile(r"\{\{\s*(\d+)\s*\}\}")
_TIMEOUT = 15


class DZ23MessageTemplate(models.Model):
    _name = "dz23.message.template"
    _description = "DZ23 — Template oficial de mensagem (por canal)"
    _order = "channel_id, name"

    channel_id = fields.Many2one("dz23.channel", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one(
        related="channel_id.company_id", store=True, index=True, readonly=True
    )
    name = fields.Char("Nome no provedor", required=True)
    language_code = fields.Char("Idioma", required=True, default="pt_BR")
    category = fields.Selection(
        [("utility", "Utilidade"), ("marketing", "Marketing"), ("authentication", "Autenticação")],
        default="utility",
    )
    body = fields.Text("Corpo (referência)", help="Use {{1}}, {{2}}… para as variáveis.")
    variable_count = fields.Integer(compute="_compute_variable_count", store=True)
    status = fields.Selection(
        [
            ("approved", "Aprovado"),
            ("pending", "Em análise"),
            ("rejected", "Rejeitado"),
            ("paused", "Pausado"),
            ("disabled", "Desativado"),
        ],
        default="pending",
        required=True,
        index=True,
    )
    provider_template_id = fields.Char(
        "Id no provedor", help="Meta: id do template. Twilio: ContentSid."
    )
    last_sync_at = fields.Datetime(readonly=True)

    _template_uniq = models.Constraint(
        "unique(channel_id, name, language_code)", "Template já cadastrado para este canal/idioma."
    )

    @api.depends("body")
    def _compute_variable_count(self):
        for template in self:
            template.variable_count = len(
                {int(n) for n in _PLACEHOLDER_RE.findall(template.body or "")}
            )

    def _check_sendable(self, params):
        self.ensure_one()
        if self.status != "approved":
            raise ProviderPermanentError(
                _("Template '%s' não está aprovado pelo provedor.") % self.name
            )
        if len(params or []) != self.variable_count:
            raise ProviderPermanentError(
                _("Template '%(name)s' exige %(n)s variável(is).")
                % {"name": self.name, "n": self.variable_count}
            )

    def _render(self, params):
        self.ensure_one()
        values = [str(p) for p in (params or [])]

        def _replace(match):
            index = int(match.group(1)) - 1
            return values[index] if 0 <= index < len(values) else match.group(0)

        return _PLACEHOLDER_RE.sub(_replace, self.body or self.name)

    @api.model
    def _sync_meta(self, channel):
        """Importa/atualiza os templates da conta WhatsApp Business (Meta)."""
        channel = channel.sudo()
        if channel.provider != "meta_cloud" or not (channel.meta_waba_id and channel.meta_token):
            raise ProviderPermanentError(
                _("Canal Meta sem WABA id/token para sincronizar templates.")
            )
        url = "https://graph.facebook.com/%s/%s/message_templates" % (
            channel.meta_api_version or "v20.0",
            channel.meta_waba_id,
        )
        try:
            resp = requests.get(
                url,
                headers={"Authorization": "Bearer %s" % channel.meta_token},
                params={"limit": 200},
                timeout=_TIMEOUT,
            )
        except requests.exceptions.RequestException as error:
            raise ProviderTransientError(
                _("Falha de rede ao sincronizar templates (%s).") % type(error).__name__
            ) from None
        if resp.status_code >= 400:
            try:
                body = resp.json()
            except ValueError:
                body = {}
            raise classify_http_error(resp.status_code, resp.headers, body)
        now = fields.Datetime.now()
        count = 0
        for item in (resp.json() or {}).get("data") or []:
            body_text = next(
                (
                    c.get("text")
                    for c in item.get("components") or []
                    if str(c.get("type")).upper() == "BODY"
                ),
                "",
            )
            vals = {
                "channel_id": channel.id,
                "name": item.get("name"),
                "language_code": item.get("language") or "pt_BR",
                "category": str(item.get("category") or "utility").lower()
                if str(item.get("category") or "").lower()
                in ("utility", "marketing", "authentication")
                else "utility",
                "body": body_text,
                "status": str(item.get("status") or "pending").lower()
                if str(item.get("status") or "").lower()
                in ("approved", "pending", "rejected", "paused", "disabled")
                else "pending",
                "provider_template_id": item.get("id") or False,
                "last_sync_at": now,
            }
            if not vals["name"]:
                continue
            existing = self.sudo().search(
                [
                    ("channel_id", "=", channel.id),
                    ("name", "=", vals["name"]),
                    ("language_code", "=", vals["language_code"]),
                ],
                limit=1,
            )
            if existing:
                existing.write(vals)
            else:
                self.sudo().create(vals)
            count += 1
        _logger.info("Canal %s: %s template(s) sincronizado(s).", channel.id, count)
        return count
