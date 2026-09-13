# Pagamento isolado por empresa (Fase 11): a transação PIX e o evento Woovi de uma
# empresa não aparecem para o administrador restrito a outra empresa. Sem rede.
from odoo.tests import TransactionCase, new_test_user, tagged


@tagged("post_install", "-at_install", "dz23")
class TestWooviTenancy(TransactionCase):
    def setUp(self):
        super().setUp()
        brl = self.env.ref("base.BRL")
        brl.active = True
        Company = self.env["res.company"]
        self.company_a = Company.create({"name": "Tenant A PIX QA11", "currency_id": brl.id})
        self.company_b = Company.create({"name": "Tenant B PIX QA11", "currency_id": brl.id})
        base_provider = self.env.ref("dz23_payment_woovi.payment_provider_woovi")
        self.provider_b = base_provider.copy(
            {
                "company_id": self.company_b.id,
                "state": "test",
                "woovi_app_id": "app-id-de-teste-b",
            }
        )
        method = self.env.ref("dz23_payment_woovi.payment_method_woovi")
        partner = self.env["res.partner"].create(
            {"name": "Cliente PIX B QA11", "company_id": self.company_b.id}
        )
        self.tx_b = self.env["payment.transaction"].create(
            {
                "provider_id": self.provider_b.id,
                "payment_method_id": method.id,
                "amount": 25.0,
                "currency_id": brl.id,
                "partner_id": partner.id,
                "reference": "WOOVI-TENANT-B-QA11",
                "operation": "online_direct",
            }
        )
        self.event_b, _created = self.env["dz23.woovi.event"]._ingest_payload(
            {
                "event": "OPENPIX:CHARGE_COMPLETED",
                "charge": {
                    "correlationID": self.tx_b.reference,
                    "status": "COMPLETED",
                    "value": 2500,
                },
                "pix": {"value": 2500, "endToEndId": "E2E-TENANT-B"},
            }
        )
        self.admin_a = new_test_user(
            self.env,
            login="admin_pix_a_qa11",
            groups="base.group_system",
            company_id=self.company_a.id,
            company_ids=[(6, 0, [self.company_a.id])],
        )

    def test_event_belongs_to_transaction_company(self):
        self.assertEqual(self.event_b.transaction_id, self.tx_b)
        self.assertEqual(self.event_b.company_id, self.company_b)

    def test_payment_and_event_hidden_from_other_company(self):
        Event = self.env["dz23.woovi.event"].with_user(self.admin_a)
        Tx = self.env["payment.transaction"].with_user(self.admin_a)
        self.assertNotIn(self.event_b, Event.search([]))
        self.assertFalse(Tx.search([("id", "=", self.tx_b.id)]))
