# Caixa de atendimento humano (ADR-010), sem rede: conversa criada na mensagem
# recebida, SLA, reabertura, assumir/devolver, resposta pela outbox, nota interna
# nunca enviada, janela de 24 h, bloqueio, opt-out, transferência, busca e
# isolamento por empresa com o grupo Atendente.
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install", "dz23")
class TestConversation(TransactionCase):
    def setUp(self):
        super().setUp()
        Company = self.env["res.company"]
        self.company_a = self.env.company
        self.company_b = Company.create({"name": "Empresa B Atendimento"})
        Channel = self.env["dz23.channel"]
        self.channel = Channel.create(
            {
                "name": "Canal Atendimento",
                "company_id": self.company_a.id,
                "provider": "evolution",
                "evo_base": "http://evo.local:8080",
                "evo_instance": "atend_a",
                "evo_apikey": "K",
                "agent_autoreply": True,
            }
        )
        self.channel_b = Channel.create(
            {
                "name": "Canal B",
                "company_id": self.company_b.id,
                "provider": "evolution",
                "evo_base": "http://evo.local:8080",
                "evo_instance": "atend_b",
                "evo_apikey": "K",
            }
        )
        self.meta = Channel.create(
            {
                "name": "Meta Atendimento",
                "company_id": self.company_a.id,
                "provider": "meta_cloud",
                "meta_phone_id": "PHONE-ATD",
                "meta_token": "T",
            }
        )
        group = self.env.ref("dz23_whatsapp.group_dz23_attendant")
        self.attendant = self.env["res.users"].create(
            {
                "name": "Atendente A",
                "login": "atendente_a_dz23",
                "company_id": self.company_a.id,
                "company_ids": [(6, 0, [self.company_a.id])],
                "group_ids": [(6, 0, [group.id])],
            }
        )
        self.Inbox = self.env["dz23.message.inbox"]
        self.Outbox = self.env["dz23.message.outbox"]
        self.Conversation = self.env["dz23.conversation"]
        self.number = "5561977770001"

    def _inbound(self, channel, mid, text="oi", number=None):
        event = {
            "provider_message_id": mid,
            "sender": number or self.number,
            "text": text,
            "message_type": "text",
            "payload": {"id": mid},
            "occurred_at": None,
        }
        return self.Inbox._enqueue_event(channel, event)[0]

    def test_inbound_creates_conversation_with_sla(self):
        before = fields.Datetime.now()
        conversation = self._inbound(self.channel, "C-1").conversation_id
        self.assertTrue(conversation)
        self.assertEqual(conversation.state, "bot_active")
        self.assertEqual(conversation.phone, self.number)
        self.assertGreaterEqual(conversation.first_response_due_at, before + timedelta(minutes=14))
        self.assertEqual(
            self._inbound(self.channel, "C-2").conversation_id,
            conversation,
            "uma conversa por contato",
        )

    def test_resolved_reopens_but_blocked_does_not(self):
        conversation = self._inbound(self.channel, "R-1").conversation_id
        conversation.action_resolve()
        self._inbound(self.channel, "R-2")
        self.assertEqual(conversation.state, "bot_active")
        conversation.action_block()
        self._inbound(self.channel, "R-3")
        self.assertEqual(conversation.state, "blocked")

    def test_waiting_customer_answer_returns_to_attendant(self):
        # Auditoria C-4: resposta do cliente volta para quem pediu, não para o robô.
        conversation = self._inbound(self.channel, "W-1").conversation_id
        conversation.with_user(self.attendant).action_take()
        conversation.action_wait_customer()
        self._inbound(self.channel, "W-2", text="segue o documento")
        self.assertEqual(conversation.state, "human_active")
        self.assertFalse(conversation.bot_can_reply())

    def test_plain_user_cannot_read_contacts_or_compose(self):
        # Auditoria B-4: contato e composição de WhatsApp só para Atendente.
        contact = self._inbound(self.channel, "U-1").conversation_id.contact_id
        plain = self.env["res.users"].create(
            {
                "name": "Usuário comum",
                "login": "comum_contatos_qa12",
                "company_id": self.company_a.id,
                "company_ids": [(6, 0, [self.company_a.id])],
                "group_ids": [(6, 0, [self.env.ref("base.group_user").id])],
            }
        )
        with self.assertRaises(AccessError):
            contact.with_user(plain).read(["provider_user_id"])
        with self.assertRaises(AccessError):
            self.env["dz23.whatsapp.compose"].with_user(plain).create({})

    def test_release_to_bot_requires_autoreply(self):
        self.channel_b.agent_autoreply = False
        conversation = self._inbound(self.channel_b, "RB-1").conversation_id
        conversation.action_take()
        with self.assertRaises(UserError):
            conversation.action_release_to_bot()

    def test_reply_wizard_refuses_templates_that_would_fail(self):
        # Auditoria C-6: opt-out e número de variáveis são conferidos antes de enfileirar.
        conversation = self._inbound(self.meta, "TPL-1", number="5561977770009").conversation_id
        template = self.env["dz23.message.template"].create(
            {
                "channel_id": self.meta.id,
                "name": "boas_vindas_qa12",
                "body": "Olá {{1}}, tudo bem?",
                "status": "approved",
            }
        )
        Reply = self.env["dz23.conversation.reply"].with_user(self.attendant)
        wrong_count = Reply.create(
            {"conversation_id": conversation.id, "template_id": template.id, "template_params": ""}
        )
        with self.assertRaises(UserError):
            wrong_count.action_send()
        conversation.opt_out = True
        opted_out = Reply.create(
            {
                "conversation_id": conversation.id,
                "template_id": template.id,
                "template_params": "Ana",
            }
        )
        with self.assertRaises(UserError):
            opted_out.action_send()
        self.assertFalse(self.Outbox.search([("conversation_id", "=", conversation.id)]))

    def test_take_and_release(self):
        conversation = self._inbound(self.channel, "T-1").conversation_id
        conversation.with_user(self.attendant).action_take()
        self.assertEqual(conversation.state, "human_active")
        self.assertEqual(conversation.user_id, self.attendant)
        self.assertFalse(conversation.bot_can_reply())
        conversation.with_user(self.attendant).action_release_to_bot()
        self.assertTrue(conversation.bot_can_reply())

    def test_reply_goes_through_outbox_and_clears_sla(self):
        conversation = self._inbound(self.channel, "P-1").conversation_id
        wizard = (
            self.env["dz23.conversation.reply"]
            .with_user(self.attendant)
            .create({"conversation_id": conversation.id, "body": "Olá, sou a Ana!"})
        )
        wizard.action_send()
        outbox = self.Outbox.search([("conversation_id", "=", conversation.id)])
        self.assertEqual(len(outbox), 1)
        self.assertEqual(outbox.body, "Olá, sou a Ana!")
        self.assertEqual(conversation.state, "human_active")
        with patch.object(
            type(self.env["dz23.channel"]), "send_text", return_value={"key": {"id": "EVO-R"}}
        ):
            outbox._process_one()
        self.assertEqual(outbox.status, "sent")
        self.assertTrue(conversation.last_agent_message_at)
        self.assertFalse(conversation.first_response_due_at)

    def test_internal_note_is_never_sent(self):
        conversation = self._inbound(self.channel, "N-1").conversation_id
        conversation.with_user(self.attendant).message_post(
            body="Cliente irritado, oferecer desconto só com gerente", subtype_xmlid="mail.mt_note"
        )
        self.assertFalse(self.Outbox.search([("conversation_id", "=", conversation.id)]))

    def test_reply_outside_window_requires_template(self):
        conversation = self.Conversation._for_number(self.meta, self.number)
        wizard = self.env["dz23.conversation.reply"].create(
            {"conversation_id": conversation.id, "body": "oi"}
        )
        self.assertFalse(wizard.window_open)
        with self.assertRaises(UserError):
            wizard.action_send()

    def test_blocked_conversation_refuses_sends(self):
        conversation = self._inbound(self.channel, "B-1").conversation_id
        outbox = self.Outbox._enqueue(self.channel, self.number, "resposta do robô")
        conversation.action_block()
        with patch.object(type(self.env["dz23.channel"]), "send_text") as send:
            outbox._process_one()
        self.assertEqual((outbox.status, outbox.dlq_reason), ("dead", "permanent_error"))
        self.assertEqual(send.call_count, 0)
        with self.assertRaises(UserError):
            conversation._send_human(body="oi")

    def test_opt_out_blocks_templates(self):
        conversation = self._inbound(self.channel, "O-1").conversation_id
        conversation.opt_out = True
        template = self.env["dz23.message.template"].create(
            {
                "channel_id": self.channel.id,
                "name": "promo",
                "body": "Promoção!",
                "status": "approved",
            }
        )
        outbox = self.Outbox._enqueue(self.channel, self.number, None, template=template)
        outbox._process_one()
        self.assertEqual(outbox.status, "dead")
        self.assertIn("opt-out", outbox.error)

    def test_sla_breach_cron(self):
        conversation = self._inbound(self.channel, "S-1").conversation_id
        conversation.write({"first_response_due_at": fields.Datetime.now() - timedelta(minutes=1)})
        self.assertEqual(self.Conversation._cron_update_sla(), 1)
        self.assertTrue(conversation.sla_breached)

    def test_transfer_to_team_with_internal_note(self):
        conversation = self._inbound(self.channel, "X-1").conversation_id
        team = self.env["crm.team"].create({"name": "Financeiro QA"})
        self.env["dz23.conversation.transfer"].create(
            {"conversation_id": conversation.id, "team_id": team.id, "note": "verificar boleto"}
        ).action_transfer()
        self.assertEqual(conversation.team_id, team)
        self.assertEqual(conversation.state, "waiting_internal")
        self.assertIn("verificar boleto", "\n".join(conversation.message_ids.mapped("body")))

    def test_search_by_text_and_phone(self):
        conversation = self._inbound(
            self.channel, "Q-1", text="preciso do orçamento da reforma"
        ).conversation_id
        self.assertIn(
            conversation,
            self.Conversation.search([("message_text_search", "ilike", "orçamento da reforma")]),
        )
        self.assertIn(conversation, self.Conversation.search([("phone", "ilike", "77770001")]))

    def test_attendant_is_isolated_by_company(self):
        mine = self._inbound(self.channel, "I-1").conversation_id
        other = self._inbound(self.channel_b, "I-2", number="5561966660002").conversation_id
        visible = self.Conversation.with_user(self.attendant).search([])
        self.assertIn(mine, visible)
        self.assertNotIn(other, visible)
        with self.assertRaises(AccessError):
            other.with_user(self.attendant).read(["name"])

    def test_regular_user_cannot_read_messages(self):
        self._inbound(self.channel, "U-1")
        regular = self.env["res.users"].create(
            {
                "name": "Usuário comum",
                "login": "usuario_comum_dz23",
                "group_ids": [(6, 0, [self.env.ref("base.group_user").id])],
            }
        )
        with self.assertRaises(AccessError):
            self.Inbox.with_user(regular).search([])
