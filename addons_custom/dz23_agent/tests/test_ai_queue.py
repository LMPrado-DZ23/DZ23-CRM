# Fila de IA fora do caminho crítico (ADR-008): a mensagem livre não chama a IA no
# item da inbox; o worker responde, tenta de novo e, esgotado, entrega o fallback.
# Política: IA externa não autorizada responde o fallback na hora. Sem rede.
import uuid

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install", "dz23")
class TestAIRequestQueue(TransactionCase):
    def setUp(self):
        super().setUp()
        self.channel = self.env["dz23.channel"].create(
            {
                "name": "Canal IA",
                "company_id": self.env.company.id,
                "provider": "evolution",
                "evo_base": "http://x.local:8080",
                "evo_instance": "ia_%s" % uuid.uuid4().hex[:8],
                "evo_apikey": "K",
            }
        )
        self.number = "5561900000099"
        self.Req = self.env["dz23.ai.request"]
        self.Outbox = self.env["dz23.message.outbox"]
        self.ai_cls = type(self.env["dz23.ai"])
        self.env["ir.config_parameter"].sudo().set_param("dz23.ai_provider", "ollama")
        self.calls = 0

    def _replies(self):
        return self.Outbox.search([("channel_id", "=", self.channel.id)], order="id").mapped("body")

    def _requests(self):
        return self.Req.search([("channel_id", "=", self.channel.id)])

    def test_free_text_is_queued_not_answered_inline(self):
        def _count(*_a, **_k):
            self.calls += 1
            return "não deveria ser chamado"

        self.patch(self.ai_cls, "chat", _count)
        self.channel.handle_inbound(self.number, "oi, tudo bem com vocês?")
        self.assertEqual(self.calls, 0, "o item da inbox não chama a IA")
        self.assertEqual(len(self._requests()), 1)
        self.assertEqual(self._requests().status, "pending")
        self.assertFalse(self._replies())

    def test_worker_delivers_ai_reply(self):
        self.patch(self.ai_cls, "chat", lambda *_a, **_k: "Olá! Como posso ajudar? 😊")
        self.channel.handle_inbound(self.number, "oi, tudo bem com vocês?")
        self.Req._cron_process()
        req = self._requests()
        self.assertEqual(req.status, "done")
        self.assertFalse(req.used_fallback)
        self.assertEqual(req.provider, "ollama")
        self.assertTrue(req.prompt_version)
        self.assertEqual(self._replies(), ["Olá! Como posso ajudar? 😊"])

    def test_failure_falls_back_after_max_attempts(self):
        def _boom(*_a, **_k):
            raise UserError("IA fora do ar")

        self.patch(self.ai_cls, "chat", _boom)
        self.channel.handle_inbound(self.number, "oi, tudo bem com vocês?")
        req = self._requests()
        req.max_attempts = 2
        self.Req._cron_process()
        self.assertEqual(req.status, "failed")
        self.assertFalse(self._replies(), "ainda tentando: sem resposta duplicada")
        req.next_attempt_at = fields.Datetime.now()
        self.Req._cron_process()
        self.assertEqual(req.status, "done")
        self.assertTrue(req.used_fallback)
        self.assertEqual(len(self._replies()), 1)
        self.assertIn("mensagem", self._replies()[0])

    def test_empty_ai_answer_is_a_failure(self):
        self.patch(self.ai_cls, "chat", lambda *_a, **_k: "   ")
        self.channel.handle_inbound(self.number, "oi, tudo bem com vocês?")
        self.Req._cron_process()
        self.assertEqual(self._requests().status, "failed")

    def test_blocked_external_ai_answers_fallback_immediately(self):
        params = self.env["ir.config_parameter"].sudo()
        params.set_param("dz23.ai_provider", "openai")
        params.set_param("dz23.ai.external_allowed", "0")
        self.channel.handle_inbound(self.number, "oi, tudo bem com vocês?")
        self.assertFalse(self._requests(), "sem IA permitida não há fila")
        self.assertEqual(len(self._replies()), 1)
