# O robô respeita a caixa de atendimento (ADR-010): com humano no controle ele só
# registra; com o robô desligado a mensagem continua visível no lead; "SAIR" faz
# opt-out; a conversa é encontrada pelo lead e pelo pedido de venda. Sem rede/IA.
import uuid

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, new_test_user, tagged


def _ai_offline(*_args, **_kwargs):
    raise UserError("IA desligada no teste")


@tagged("post_install", "-at_install", "dz23")
class TestConversationBot(TransactionCase):
    def setUp(self):
        super().setUp()
        self.patch(type(self.env["dz23.ai"]), "chat", _ai_offline)
        self.channel = self.env["dz23.channel"].create(
            {
                "name": "Canal Bot Atendimento",
                "company_id": self.env.company.id,
                "provider": "evolution",
                "evo_base": "http://evo.local:8080",
                "evo_instance": "bot_atend_%s" % uuid.uuid4().hex[:8],
                "evo_apikey": "K",
                "agent_autoreply": True,
            }
        )
        self.number = "5561900000123"
        self.product = self.env["product.template"].create(
            {
                "name": "Servico Gama QA7",
                "type": "service",
                "list_price": 80.0,
                "sale_ok": True,
                "taxes_id": [(6, 0, [])],
            }
        )
        self.Outbox = self.env["dz23.message.outbox"]

    def _outbox_count(self):
        return self.Outbox.search_count([("channel_id", "=", self.channel.id)])

    def _conversation(self):
        return self.env["dz23.conversation"]._for_number(self.channel, self.number)

    def test_bot_is_silent_when_human_is_active(self):
        self.channel.handle_inbound(self.number, "quanto custa o Servico Gama QA7?")
        self.assertEqual(self._outbox_count(), 1, "robô responde enquanto é ele que atende")
        self._conversation().action_take()
        self.channel.handle_inbound(self.number, "quero comprar Servico Gama QA7")
        self.assertEqual(self._outbox_count(), 1, "com humano no controle o robô não responde")
        self.assertFalse(self.env["dz23.ai.request"].search([("channel_id", "=", self.channel.id)]))
        lead = self.channel._agent_contact(self.number).lead_id
        self.assertIn("quero comprar", "\n".join(lead.message_ids.mapped("body")))

    def test_autoreply_off_still_registers_lead(self):
        self.channel.agent_autoreply = False
        self.channel.handle_inbound(self.number, "bom dia, alguém aí?")
        lead = self.channel._agent_contact(self.number).lead_id
        self.assertTrue(lead)
        self.assertIn("alguém aí", "\n".join(lead.message_ids.mapped("body")))
        self.assertEqual(self._outbox_count(), 0)

    def test_opt_out_keyword(self):
        self.channel.handle_inbound(self.number, "SAIR")
        self.assertTrue(self._conversation().opt_out)
        self.assertEqual(self._outbox_count(), 1, "confirma o opt-out ao cliente")

    def test_conversation_found_by_lead_and_sale_order(self):
        self.channel.handle_inbound(self.number, "quero comprar Servico Gama QA7")
        self.channel.handle_inbound(self.number, "sim")
        conversation = self._conversation()
        order = self.env["sale.order"].search([("dz23_channel_id", "=", self.channel.id)])
        self.assertEqual(len(order), 1)
        Conversation = self.env["dz23.conversation"]
        self.assertIn(conversation, Conversation.search([("sale_order_ids", "ilike", order.name)]))
        self.assertIn(
            conversation, Conversation.search([("lead_id", "=", conversation.lead_id.id)])
        )
        self.assertIn(order, conversation.sale_order_ids)

    def test_attendant_without_sales_rights_opens_conversation(self):
        # Auditoria C-1: o formulário não pode quebrar para quem só é Atendente.
        self.channel.handle_inbound(self.number, "quanto custa o Servico Gama QA7?")
        conversation = self._conversation()
        attendant = new_test_user(
            self.env,
            login="atendente_sem_vendas_qa12",
            groups="dz23_whatsapp.group_dz23_attendant",
        )
        Conversation = self.env["dz23.conversation"].with_user(attendant)
        views = Conversation.get_views([(False, "form")])
        spec = {name: {} for name in views["models"]["dz23.conversation"]["fields"]}
        self.assertNotIn("sale_order_ids", spec)
        self.assertNotIn("lead_id", spec)
        record = conversation.with_user(attendant).web_read(spec)
        self.assertEqual(record[0]["id"], conversation.id)
