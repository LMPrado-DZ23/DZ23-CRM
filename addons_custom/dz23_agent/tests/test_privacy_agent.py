# LGPD no agente (Fase 10, ADR-013), sem rede: exportação traz lead, pedido e IA;
# anonimização limpa lead, chatter e textos de IA mas mantém o pedido de venda;
# retenção apaga as notas automáticas antigas do robô no lead.
import json
import uuid
from datetime import timedelta

from odoo import fields
from odoo.addons.dz23_whatsapp.models.privacy import ERASED, REMOVED
from odoo.tests import TransactionCase, new_test_user, tagged


@tagged("post_install", "-at_install", "dz23")
class TestPrivacyAgent(TransactionCase):
    def setUp(self):
        super().setUp()
        self.env["ir.config_parameter"].sudo().set_param("dz23.ai_provider", "ollama")
        self.env.company.dz23_ai_provider = False
        self.channel = self.env["dz23.channel"].create(
            {
                "name": "Canal Privacidade Agente",
                "company_id": self.env.company.id,
                "provider": "evolution",
                "evo_base": "http://privagente.local:8080",
                "evo_instance": "privagente_%s" % uuid.uuid4().hex[:8],
                "evo_apikey": "K",
                "agent_autoreply": True,
            }
        )
        self.env["product.template"].create(
            {
                "name": "Servico Privacidade QA10",
                "type": "service",
                "list_price": 50.0,
                "sale_ok": True,
                "taxes_id": [(6, 0, [])],
            }
        )
        self.patch(type(self.env["dz23.ai"]), "chat", lambda *_a, **_k: "Olá! 😊")
        self.number = "5561900001010"
        self.supervisor = new_test_user(
            self.env,
            login="supervisor_privagente_qa10",
            groups="dz23_whatsapp.group_dz23_supervisor",
            company_id=self.env.company.id,
            company_ids=[(6, 0, [self.env.company.id])],
        )

    def _talk(self):
        self.channel.handle_inbound(self.number, "oi, meu email é maria.qa10@example.com")
        self.env["dz23.ai.request"]._cron_process()
        self.channel.handle_inbound(self.number, "quero comprar Servico Privacidade QA10")
        self.channel.handle_inbound(self.number, "sim")
        return self.channel._agent_contact(self.number).lead_id

    def _request(self, **vals):
        return (
            self.env["dz23.privacy.request"]
            .with_user(self.supervisor)
            .create({"phone": self.number, **vals})
        )

    def test_export_includes_lead_orders_and_ai(self):
        self._talk()
        action = self._request().action_export()
        attachment_id = int(action["url"].split("/")[3].split("?")[0])
        exported = json.loads(self.env["ir.attachment"].browse(attachment_id).raw.decode("utf-8"))
        self.assertEqual(len(exported["leads"]), 1)
        self.assertEqual(len(exported["sale_orders"]), 1)
        self.assertIn("maria.qa10@example.com", exported["ai_requests"][0]["message"])

    def test_anonymize_keeps_sale_order(self):
        lead = self._talk()
        order = self.env["sale.order"].search([("dz23_channel_id", "=", self.channel.id)])
        self.assertEqual(len(order), 1)
        self._request(reason="Protocolo 10").action_anonymize()
        lead = lead.with_context(active_test=False)
        self.assertFalse(lead.active)
        self.assertFalse(lead.contact_name)
        self.assertEqual(lead.name, "Lead anonimizado")
        bodies = " ".join(lead.message_ids.mapped("body"))
        self.assertNotIn("maria.qa10@example.com", bodies)
        requests = self.env["dz23.ai.request"].search([("lead_id", "=", lead.id)])
        self.assertTrue(requests)
        self.assertTrue(all(request.prompt_text == ERASED for request in requests))
        self.assertTrue(order.exists(), "pedido de venda (fiscal) permanece")

    def test_retention_clears_old_bot_notes_on_lead(self):
        lead = self.channel._agent_contact(self.number).lead_id
        note = lead.message_post(body="📩 WhatsApp recebido de %s: segredo antigo" % self.number)
        recent = lead.message_post(body="📩 WhatsApp recebido de %s: recente" % self.number)
        manual = lead.message_post(body="Ligação feita pelo vendedor")
        (note | manual).sudo().write({"date": fields.Datetime.now() - timedelta(days=400)})
        self.env["dz23.privacy.retention"]._cron_apply()
        self.assertIn(REMOVED, note.body)
        self.assertIn("recente", recent.body)
        self.assertIn("Ligação feita", manual.body, "nota humana não é apagada pela retenção")
