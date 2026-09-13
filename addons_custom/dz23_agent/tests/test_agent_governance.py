# Governança do robô (Fase 8, ADR-011), sem rede: assunto sensível e limite de
# respostas livres transferem para humano; breaker aberto não faz retry; resposta
# descartada se o humano assumiu; guarda troca preço/desconto inventado pela IA;
# regras fixas no prompt e histórico só para IA local; retenção dos textos.
import uuid
from datetime import timedelta

from odoo import fields
from odoo.addons.dz23_ai.models.ai_service import AICircuitOpenError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install", "dz23")
class TestAgentGovernance(TransactionCase):
    def setUp(self):
        super().setUp()
        self.env["ir.config_parameter"].sudo().set_param("dz23.ai_provider", "ollama")
        self.env.company.dz23_ai_provider = False
        self.channel = self.env["dz23.channel"].create(
            {
                "name": "Canal Governança",
                "company_id": self.env.company.id,
                "provider": "evolution",
                "evo_base": "http://gov.local:8080",
                "evo_instance": "gov_%s" % uuid.uuid4().hex[:8],
                "evo_apikey": "K",
                "agent_autoreply": True,
                "agent_max_bot_turns": 2,
            }
        )
        self.number = "5561900000321"
        self.ai_cls = type(self.env["dz23.ai"])
        self.Req = self.env["dz23.ai.request"]
        self.Outbox = self.env["dz23.message.outbox"]

    def _conversation(self):
        return self.env["dz23.conversation"]._for_number(self.channel, self.number)

    def _replies(self):
        return self.Outbox.search([("channel_id", "=", self.channel.id)], order="id").mapped("body")

    def _requests(self):
        return self.Req.search([("channel_id", "=", self.channel.id)], order="id")

    def test_sensitive_detection(self):
        channel = self.channel
        self.assertTrue(channel._agent_is_sensitive("Quero cancelar meu pedido"))
        self.assertTrue(channel._agent_is_sensitive("quero falar com um atendente"))
        self.assertTrue(channel._agent_is_sensitive("fui cobrado indevidamente"))
        self.assertFalse(channel._agent_is_sensitive("quero cancelar meu agendamento"))
        self.assertFalse(channel._agent_is_sensitive("quero agendar um horário"))

    def test_sensitive_subject_hands_off_to_human(self):
        self.channel.handle_inbound(self.number, "Quero fazer uma reclamação no Procon")
        conversation = self._conversation()
        self.assertEqual(
            (conversation.state, conversation.handoff_reason), ("waiting_internal", "sensitive")
        )
        self.assertEqual(conversation.priority, "1")
        self.assertFalse(self._requests(), "assunto sensível não vai para a IA")
        self.assertEqual(len(self._replies()), 1)
        self.assertIn("equipe", self._replies()[0])
        self.channel.handle_inbound(self.number, "alô?")
        self.assertEqual(len(self._replies()), 1, "transferida: o robô fica em silêncio")

    def test_cancel_appointment_stays_with_bot(self):
        self.channel.handle_inbound(self.number, "quero cancelar meu agendamento")
        conversation = self._conversation()
        self.assertFalse(conversation.handoff_reason)
        self.assertNotEqual(conversation.state, "waiting_internal")

    def test_free_text_turn_limit_hands_off(self):
        self.patch(self.ai_cls, "chat", lambda *_a, **_k: "Claro!")
        for text in ("oi", "tudo bem?", "e aí?"):
            self.channel.handle_inbound(self.number, text)
        conversation = self._conversation()
        self.assertEqual(len(self._requests()), 2)
        self.assertEqual(
            (conversation.state, conversation.handoff_reason), ("waiting_internal", "bot_limit")
        )
        self.Req._cron_process()
        self.assertEqual(len(self._replies()), 1, "respostas de IA pendentes são descartadas")
        self.assertTrue(all(r.status == "done" and r.error for r in self._requests()))

    def test_business_intent_resets_turns(self):
        self.env["product.template"].create(
            {
                "name": "Servico Gov QA8",
                "type": "service",
                "list_price": 70.0,
                "sale_ok": True,
                "taxes_id": [(6, 0, [])],
            }
        )
        self.channel.handle_inbound(self.number, "oi")
        self.assertEqual(self._conversation().bot_turns, 1)
        self.channel.handle_inbound(self.number, "quanto custa o Servico Gov QA8?")
        self.assertEqual(self._conversation().bot_turns, 0)

    def test_circuit_open_hands_off_without_retry(self):
        def _open(*_a, **_k):
            raise AICircuitOpenError("breaker aberto")

        self.patch(self.ai_cls, "chat", _open)
        self.channel.handle_inbound(self.number, "oi")
        self.Req._cron_process()
        req = self._requests()
        self.assertEqual((req.status, req.used_fallback, req.attempts), ("done", True, 1))
        self.assertEqual(self._conversation().handoff_reason, "ai_unavailable")
        self.assertEqual(len(self._replies()), 1)

    def test_reply_is_dropped_when_human_takes_over(self):
        self.patch(self.ai_cls, "chat", lambda *_a, **_k: "Olá!")
        self.channel.handle_inbound(self.number, "oi")
        self._conversation().action_take()
        self.Req._cron_process()
        req = self._requests()
        self.assertEqual(req.status, "done")
        self.assertTrue(req.error)
        self.assertFalse(self._replies())

    def test_ai_reply_about_price_or_discount_is_replaced(self):
        self.patch(self.ai_cls, "chat", lambda *_a, **_k: "Fica R$ 50,00 com 10% de desconto!")
        self.channel.handle_inbound(self.number, "oi")
        self.Req._cron_process()
        req = self._requests()
        self.assertTrue(req.guard_triggered)
        self.assertNotIn("50,00", self._replies()[0])
        self.assertIn("tabela oficial", self._replies()[0])

    def test_system_prompt_rules_and_history_only_for_local_ai(self):
        lead = self.channel._agent_contact(self.number).lead_id
        lead.message_post(body="nota interna secreta QA8")
        prompt = self.channel._agent_system_prompt(lead)
        self.assertIn("nunca informe", prompt)
        self.assertIn("nota interna secreta QA8", prompt)
        self.env["ir.config_parameter"].sudo().set_param("dz23.ai_provider", "openai")
        prompt = self.channel._agent_system_prompt(lead)
        self.assertIn("nunca informe", prompt)
        self.assertNotIn("nota interna secreta QA8", prompt)

    def test_request_texts_are_purged_after_retention(self):
        self.patch(self.ai_cls, "chat", lambda *_a, **_k: "Olá!")
        self.channel.handle_inbound(self.number, "oi, meu nome é Maria")
        self.Req._cron_process()
        req = self._requests()
        req.processed_at = fields.Datetime.now() - timedelta(days=100)
        self.assertEqual(self.Req._cron_purge_texts(), 1)
        self.assertEqual(req.prompt_text, "[removido por retenção]")
        self.assertFalse(req.response_text)
        self.assertEqual(req.provider, "ollama", "metadados de auditoria ficam")
