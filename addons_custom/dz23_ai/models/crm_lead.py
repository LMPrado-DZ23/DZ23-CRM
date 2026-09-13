from odoo import models
from odoo.tools.translate import _


class CrmLead(models.Model):
    _inherit = "crm.lead"

    def _dz23_ai_summary_prompt(self, external):
        """Contexto por ALLOWLIST: para IA externa não vão e-mail nem telefone."""
        self.ensure_one()
        lines = [
            _("Resuma este lead de vendas em português, com próximos passos objetivos:"),
            _("Nome: %s") % (self.contact_name or self.name or ""),
            _("Empresa: %s") % (self.partner_name or ""),
            _("Descrição: %s") % (self.description or ""),
        ]
        if not external:
            lines.insert(3, _("E-mail: %s") % (self.email_from or ""))
            lines.insert(4, _("Telefone: %s") % (self.phone or ""))
        return "\n".join(lines)

    def action_dz23_ai_summary(self):
        """Resume o lead com IA e registra no chatter."""
        self.ensure_one()
        ai = self.env["dz23.ai"]
        company = self.company_id or self.env.company
        prompt = self._dz23_ai_summary_prompt(ai._is_external(company))
        text = ai.chat(
            prompt,
            system=_("Você é um assistente de vendas do DZ23 CRM."),
            company=company,
            purpose="lead_summary",
        )
        self.message_post(body=text or _("(sem resposta da IA)"))
        return True
