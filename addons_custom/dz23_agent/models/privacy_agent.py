# LGPD no agente (Fase 10, ADR-013): notas automáticas do robô no chatter do lead
# carregam o texto do cliente — seguem a retenção de mensagens; no pedido do titular
# entram na exportação (leads, pedidos, IA) e são anonimizados junto com o lead.
# Pedidos de venda NÃO são alterados (obrigação fiscal).
from odoo import models
from odoo.addons.dz23_whatsapp.models.privacy import ERASED, REMOVED
from odoo.fields import Datetime
from odoo.tools.translate import _

_BOT_MARKERS = ("WhatsApp recebido de", "Resposta enfileirada", "Arquivo recebido pelo WhatsApp")


def _dt(value):
    return Datetime.to_string(value) if value else None


class DZ23PrivacyRetentionAgent(models.AbstractModel):
    _inherit = "dz23.privacy.retention"

    def _retention_extra(self, company, cutoff):
        counts = super()._retention_extra(company, cutoff)
        leads = (
            self.env["crm.lead"]
            .sudo()
            .with_context(active_test=False)
            .search([("company_id", "=", company.id)])
        )
        if not leads:
            return counts
        markers = [("body", "ilike", marker) for marker in _BOT_MARKERS]
        domain = [
            ("model", "=", "crm.lead"),
            ("res_id", "in", leads.ids),
            ("date", "<", cutoff),
        ]
        domain += ["|"] * (len(markers) - 1) + markers
        messages = self.env["mail.message"].sudo().search(domain, limit=1000)
        messages.write({"body": "<p>%s</p>" % REMOVED})
        counts["lead_notes"] = len(messages)
        return counts


class DZ23PrivacyRequestAgent(models.TransientModel):
    _inherit = "dz23.privacy.request"

    def _collect_extra(self, contacts, data):
        data = super()._collect_extra(contacts, data)
        leads = contacts.lead_id.sudo()
        orders = self.env["sale.order"].sudo()
        if leads.partner_id:
            orders = orders.search([("partner_id", "in", leads.partner_id.ids)])
        requests = self.env["dz23.ai.request"].sudo().search([("lead_id", "in", leads.ids)])
        data["leads"] = [
            {
                "id": lead.id,
                "name": lead.name,
                "stage": lead.stage_id.name,
                "created_at": _dt(lead.create_date),
            }
            for lead in leads
        ]
        data["sale_orders"] = [
            {
                "name": order.name,
                "state": order.state,
                "amount_total": order.amount_total,
                "date_order": _dt(order.date_order),
            }
            for order in orders
        ]
        data["ai_requests"] = [
            {
                "processed_at": _dt(request.processed_at),
                "message": request.prompt_text,
                "reply": request.response_text or "",
                "used_fallback": request.used_fallback,
                "provider": request.provider or "",
            }
            for request in requests
        ]
        return data

    def _anonymize_extra(self, contact):
        result = super()._anonymize_extra(contact)
        lead = contact.lead_id.sudo()
        if not lead:
            return result
        self.env["dz23.ai.request"].sudo().search([("lead_id", "=", lead.id)]).write(
            {"prompt_text": ERASED, "response_text": False, "fallback_text": False}
        )
        lead.message_ids.filtered("body").write({"body": "<p>%s</p>" % ERASED})
        lead.write(
            {
                "name": _("Lead anonimizado"),
                "contact_name": False,
                "email_from": False,
                "phone": False,
                "description": False,
                "active": False,
            }
        )
        return result
