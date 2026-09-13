# DLQ isolada por empresa (Fase 11): o atendente da empresa A vê os itens mortos
# (entrada e saída) só da própria empresa e não consegue ler os da empresa B.
import uuid

from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, new_test_user, tagged


@tagged("post_install", "-at_install", "dz23")
class TestQueueTenancy(TransactionCase):
    def setUp(self):
        super().setUp()
        Company = self.env["res.company"]
        self.company_a = Company.create({"name": "Tenant A Filas QA11"})
        self.company_b = Company.create({"name": "Tenant B Filas QA11"})
        self.channel_a = self._channel("Canal A Filas", self.company_a)
        self.channel_b = self._channel("Canal B Filas", self.company_b)
        Outbox = self.env["dz23.message.outbox"]
        Inbox = self.env["dz23.message.inbox"]
        self.out_a = Outbox._enqueue(self.channel_a, "5561900002222", "saida A")
        self.out_b = Outbox._enqueue(self.channel_b, "5561900002222", "saida B")
        (self.out_a | self.out_b).write({"status": "dead", "dlq_reason": "max_attempts"})
        self.in_a, _created = Inbox._enqueue(self.channel_a, "MID-FILA-A", "5561900002222", "a", {})
        self.in_b, _created = Inbox._enqueue(self.channel_b, "MID-FILA-B", "5561900002222", "b", {})
        (self.in_a | self.in_b).write({"status": "dead", "dlq_reason": "max_attempts"})
        self.attendant_a = new_test_user(
            self.env,
            login="atendente_filas_a_qa11",
            groups="dz23_whatsapp.group_dz23_attendant",
            company_id=self.company_a.id,
            company_ids=[(6, 0, [self.company_a.id])],
        )

    def _channel(self, name, company):
        return self.env["dz23.channel"].create(
            {
                "name": name,
                "company_id": company.id,
                "provider": "evolution",
                "evo_base": "http://filas.local:8080",
                "evo_instance": "filas_%s" % uuid.uuid4().hex[:8],
                "evo_apikey": "K",
            }
        )

    def test_dlq_search_is_isolated_by_company(self):
        dead = [("status", "=", "dead")]
        outbox = self.env["dz23.message.outbox"].with_user(self.attendant_a).search(dead)
        inbox = self.env["dz23.message.inbox"].with_user(self.attendant_a).search(dead)
        self.assertIn(self.out_a, outbox)
        self.assertNotIn(self.out_b, outbox)
        self.assertIn(self.in_a, inbox)
        self.assertNotIn(self.in_b, inbox)

    def test_reading_other_company_dlq_is_denied(self):
        with self.assertRaises(AccessError):
            self.out_b.with_user(self.attendant_a).read(["body"])
        with self.assertRaises(AccessError):
            self.in_b.with_user(self.attendant_a).read(["text"])
