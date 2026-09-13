# Serviço legado/compat de WhatsApp. O envio e o processamento reais vivem em
# dz23.channel (multi-tenant, por empresa). Aqui ficam: os parsers de payload
# (stateless), o gancho _on_inbound legado e delegações de compat (send_text /
# evolution_connect) para o canal padrão da empresa atual.
import logging

from odoo import api, models
from odoo.exceptions import UserError
from odoo.tools.translate import _

_logger = logging.getLogger(__name__)


class DZ23WhatsApp(models.AbstractModel):
    _name = "dz23.whatsapp"
    _description = "DZ23 — Serviço de envio de WhatsApp (plugável)"

    def _param(self, key, default=""):
        return self.env["ir.config_parameter"].sudo().get_param(key, default)

    def _provider(self):
        return (self._param("dz23.whatsapp_provider", "meta_cloud") or "meta_cloud").strip()

    def _default_channel(self):
        """Canal padrão da empresa atual (compat p/ chamadas legadas sem canal)."""
        return self.env["dz23.channel"].search([("company_id", "=", self.env.company.id)], limit=1)

    # ---------- Entrada (inbound) ----------
    @api.model
    def _parse_meta_inbound(self, data):
        """Extrai (número, texto) de um payload de webhook da Meta Cloud API."""
        try:
            value = data["entry"][0]["changes"][0]["value"]
            msg = (value.get("messages") or [{}])[0]
            number = msg.get("from")
            text = (msg.get("text") or {}).get("body")
            return number, text
        except Exception:  # noqa: BLE001
            return None, None

    def _on_inbound(self, number, text, raw=None):
        """Gancho legado. O processamento real é via dz23.channel.handle_inbound
        acionado pelo worker do inbox. Mantido por compatibilidade."""
        _logger.info("WhatsApp inbound (legado) de %s", (number or "")[-4:])
        return False

    @api.model
    def _extract_message_id(self, provider, data):
        """ID único da mensagem no provedor (para dedupe do inbox)."""
        try:
            if provider == "evolution":
                mid = ((data.get("data") or {}).get("key") or {}).get("id")
            else:  # meta_cloud
                value = data["entry"][0]["changes"][0]["value"]
                mid = (value.get("messages") or [{}])[0].get("id")
        except (AttributeError, IndexError, KeyError, TypeError):
            mid = None  # envelope fora do formato: cai no hash abaixo
        if mid:
            return str(mid)
        # fallback determinístico: hash do envelope (evita perder mensagem sem id)
        import hashlib
        import json as _json

        return (
            "h:"
            + hashlib.sha256(_json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()[
                :32
            ]
        )

    # ---------- API pública ----------
    @api.model
    def send_text(self, number, body):
        """Compat: envia via o canal padrão da empresa atual (dz23.channel)."""
        channel = self._default_channel()
        if not channel:
            raise UserError(_("Nenhum canal de WhatsApp configurado para esta empresa."))
        return channel.send_text(number, body)

    @api.model
    def evolution_connect(self):
        """Compat: delega ao canal padrão da empresa (webhook tokenizado)."""
        channel = self._default_channel()
        if not channel:
            raise UserError(_("Crie um Canal de WhatsApp (menu DZ23 WhatsApp) primeiro."))
        return channel._evolution_provision()

    # ---------- Evolution: leitura de mensagem recebida ----------
    @api.model
    def _parse_evolution_inbound(self, data):
        """Extrai (número, texto) de um evento MESSAGES_UPSERT da Evolution."""
        try:
            d = data.get("data") or {}
            key = d.get("key") or {}
            if key.get("fromMe"):
                return None, None  # ignora o que nós mesmos enviamos
            jid = key.get("remoteJid") or ""
            number = jid.split("@")[0]
            msg = d.get("message") or {}
            text = msg.get("conversation") or (msg.get("extendedTextMessage") or {}).get("text")
            return number, text
        except Exception:  # noqa: BLE001
            return None, None
