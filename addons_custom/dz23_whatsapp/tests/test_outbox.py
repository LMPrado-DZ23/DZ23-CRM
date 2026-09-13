# Outbox durável: enqueue, envio (sucesso pelo corpo), retry/backoff, DLQ,
# requeue e o enfileiramento da resposta pelo agente (HIGH-01).
from unittest.mock import patch

from odoo import fields
from odoo.tests import TransactionCase, tagged

_SEND = "odoo.addons.dz23_whatsapp.models.whatsapp_channel.DZ23Channel.send_text"


@tagged("post_install", "-at_install", "dz23")
class TestMessageOutbox(TransactionCase):
    def setUp(self):
        super().setUp()
        self.channel = self.env["dz23.channel"].create(
            {
                "name": "Canal Outbox",
                "company_id": self.env.company.id,
                "provider": "evolution",
                "evo_base": "http://nao-existe.local:8080",
                "evo_instance": "outbox_inst",
                "evo_apikey": "K",
            }
        )
        self.Outbox = self.env["dz23.message.outbox"]

    def test_enqueue_creates_pending(self):
        rec = self.Outbox._enqueue(self.channel, "5561999990000", "olá")
        self.assertTrue(rec)
        self.assertEqual(rec.status, "pending")
        self.assertEqual(rec.recipient, "5561999990000")

    def test_enqueue_empty_is_noop(self):
        self.assertFalse(self.Outbox._enqueue(self.channel, "5561999990000", ""))
        self.assertFalse(self.Outbox._enqueue(self.channel, "", "oi"))

    def test_process_success_marks_sent(self):
        rec = self.Outbox._enqueue(self.channel, "5561999990000", "oi")
        with patch(_SEND, return_value={"key": {"id": "X"}}):
            rec._process_one()
        self.assertEqual(rec.status, "sent")
        self.assertEqual(rec.provider_message_id, "X")

    def test_status_callback_before_send_commit_is_applied(self):
        # Auditoria A-2: o "entregue" chegou antes de a outbox gravar o id do provedor.
        self.env["dz23.message.event"]._record(
            self.channel,
            {
                "provider": "evolution",
                "provider_message_id": "EARLY-ACK-1",
                "direction": "outbound",
                "status": "delivered",
                "provider_status": "DELIVERY_ACK",
                "occurred_at": fields.Datetime.now(),
                "payload": {},
            },
        )
        rec = self.Outbox._enqueue(self.channel, "5561999990000", "oi")
        with patch(_SEND, return_value={"key": {"id": "EARLY-ACK-1"}}):
            rec._process_one()
        self.assertEqual(rec.status, "sent")
        self.assertEqual(rec.current_status, "delivered")
        self.assertTrue(rec.event_ids.processed)

    def test_provider_accept_without_message_id_is_not_sent(self):
        # 2xx sem id de mensagem não prova o envio: fica em retry (nunca "sent").
        rec = self.Outbox._enqueue(self.channel, "5561999990000", "oi")
        with patch.object(type(self.channel), "_post", return_value={"status": "PENDING"}):
            rec._process_one()
        self.assertEqual(rec.status, "failed")
        self.assertFalse(rec.provider_message_id)
        self.assertEqual(rec.current_status, "queued")

    def test_process_failure_retries_then_dlq(self):
        rec = self.Outbox._enqueue(self.channel, "5561999990000", "oi")
        rec.max_attempts = 2
        with patch(_SEND, side_effect=Exception("boom")):
            rec._process_one()
            self.assertEqual(rec.status, "failed")
            self.assertEqual(rec.attempts, 1)
            self.assertGreater(rec.next_attempt_at, fields.Datetime.now())
            rec.next_attempt_at = fields.Datetime.now()
            rec._process_one()
            self.assertEqual(rec.status, "dead", "vai para DLQ após max_attempts")
            self.assertEqual(rec.attempts, 2)

    def test_cron_sends_once_no_duplicate(self):
        # Envio EXATAMENTE UMA vez: após o cron marcar 'sent' e commitar, uma
        # segunda passada NÃO reenvia (evita duplicidade).
        self.Outbox._enqueue(self.channel, "5561999990000", "oi")
        calls = {"n": 0}

        def _ok(*a, **k):
            calls["n"] += 1
            return {"key": {"id": "X"}}

        with patch(_SEND, side_effect=_ok):
            self.Outbox._cron_process()
            self.Outbox._cron_process()
        self.assertEqual(calls["n"], 1, "o cron não pode reenviar um item já 'sent'")

    def test_requeue_from_dlq(self):
        rec = self.Outbox._enqueue(self.channel, "5561999990000", "oi")
        rec.write({"status": "dead", "attempts": 6})
        rec.action_requeue()
        self.assertEqual(rec.status, "pending")
        self.assertEqual(rec.attempts, 0)

    def test_handle_inbound_enqueues_reply(self):
        # "quero agendar" (sem data) => resposta determinística (sem rede/IA),
        # que deve ser ENFILEIRADA na outbox (não enviada síncrono).
        before = self.Outbox.search_count([("channel_id", "=", self.channel.id)])
        self.channel._processing_self().handle_inbound("5561988887777", "quero agendar")
        after = self.Outbox.search_count([("channel_id", "=", self.channel.id)])
        self.assertGreater(after, before, "handle_inbound deve enfileirar a resposta na outbox")
