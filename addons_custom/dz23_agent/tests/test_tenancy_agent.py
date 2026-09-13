# Multi-tenant no agente (Fase 11): o mesmo telefone em canais de empresas
# diferentes gera leads separados, cada um na empresa do canal, e o pedido aberto
# pelo atendimento de uma empresa não aparece para o vendedor da outra. Sem rede/IA.
import uuid

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, new_test_user, tagged


def _ai_offline(*_args, **_kwargs):
    raise UserError("IA desligada no teste")


@tagged("post_install", "-at_install", "dz23")
class TestAgentTenancy(TransactionCase):
    def setUp(self):
        super().setUp()
        self.patch(type(self.env["dz23.ai"]), "chat", _ai_offline)
        Company = self.env["res.company"]
        self.company_a = Company.create({"name": "Tenant A Agente QA11"})
        self.company_b = Company.create({"name": "Tenant B Agente QA11"})
        self.channel_a = self._channel("Canal A QA11", self.company_a)
        self.channel_b = self._channel("Canal B QA11", self.company_b)
        self.number = "5561900001111"
        self.env["product.template"].create(
            {
                "name": "Servico Tenancy QA11",
                "type": "service",
                "list_price": 40.0,
                "sale_ok": True,
                "taxes_id": [(6, 0, [])],
            }
        )

    def _channel(self, name, company):
        return self.env["dz23.channel"].create(
            {
                "name": name,
                "company_id": company.id,
                "provider": "evolution",
                "evo_base": "http://tenancy.local:8080",
                "evo_instance": "tenancy_%s" % uuid.uuid4().hex[:8],
                "evo_apikey": "K",
                "agent_autoreply": True,
            }
        )

    def test_agenda_lock_covers_the_whole_company(self):
        # Auditoria A-3: dois canais da mesma empresa disputam o MESMO lock de agenda.
        sibling = self._channel("Canal A2 QA11", self.company_a)
        taken = []
        self.patch(
            type(self.channel_a),
            "_agent_lock",
            lambda channel, *parts: taken.append((channel.company_id.id, parts)),
        )
        self.channel_a._agent_agenda_lock()
        sibling._agent_agenda_lock()
        self.assertEqual(taken, [(self.company_a.id, ("agenda",))] * 2)

    def test_same_phone_creates_one_lead_per_company(self):
        lead_a = self.channel_a._agent_contact(self.number).lead_id
        lead_b = self.channel_b._agent_contact(self.number).lead_id
        self.assertTrue(lead_a and lead_b)
        self.assertNotEqual(lead_a, lead_b)
        self.assertEqual(lead_a.company_id, self.company_a)
        self.assertEqual(lead_b.company_id, self.company_b)
        self.assertEqual(self.channel_a._agent_contact(self.number).lead_id, lead_a)

    def test_sale_order_is_isolated_by_company(self):
        self.channel_a.handle_inbound(self.number, "quero comprar Servico Tenancy QA11")
        self.channel_a.handle_inbound(self.number, "sim")
        order = self.env["sale.order"].search([("dz23_channel_id", "=", self.channel_a.id)])
        self.assertEqual(len(order), 1)
        self.assertEqual(order.company_id, self.company_a)
        seller_b = new_test_user(
            self.env,
            login="vendedor_b_qa11",
            groups="sales_team.group_sale_salesman_all_leads",
            company_id=self.company_b.id,
            company_ids=[(6, 0, [self.company_b.id])],
        )
        visible = self.env["sale.order"].with_user(seller_b).search([("id", "=", order.id)])
        self.assertFalse(visible, "vendedor da empresa B não enxerga pedido da empresa A")
