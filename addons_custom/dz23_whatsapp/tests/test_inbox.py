# Inbox durável: dedupe (idempotência), exatamente-um-efeito, retry/backoff e DLQ.
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install", "dz23")
class TestMessageInbox(TransactionCase):
    def setUp(self):
        super().setUp()
        self.channel = self.env["dz23.channel"].create(
            {
                "name": "Canal Inbox",
                "company_id": self.env.company.id,
                "provider": "evolution",
                "evo_base": "http://nao-existe.local:8080",
                "evo_instance": "inbox_inst",
                "evo_apikey": "K",
            }
        )
        self.Inbox = self.env["dz23.message.inbox"]

    def _payload(self, mid, num, text):
        return {
            "instance": "inbox_inst",
            "data": {
                "key": {"id": mid, "remoteJid": "%s@s.whatsapp.net" % num},
                "message": {"conversation": text},
            },
        }

    def test_enqueue_dedupe_sequential(self):
        p = self._payload("MID1", "5561911112222", "oi")
        rec1, created1 = self.Inbox._enqueue(self.channel, "MID1", "5561911112222", "oi", p)
        rec2, created2 = self.Inbox._enqueue(self.channel, "MID1", "5561911112222", "oi", p)
        self.assertTrue(created1)
        self.assertFalse(created2, "segundo enqueue do mesmo message_id não cria novo")
        self.assertEqual(rec1, rec2)
        self.assertEqual(self.Inbox.search_count([("message_id", "=", "MID1")]), 1)

    def test_exactly_one_effect_on_reprocess(self):
        # Data futura num dia útil (terça) dentro do expediente padrão.
        day = fields.Date.today() + timedelta(days=400)
        day += timedelta(days=(1 - day.weekday()) % 7)
        text = "quero agendar %s as 14:00" % day.strftime("%d/%m/%Y")
        p = self._payload("MID2", "5561933334444", text)
        self.Inbox._enqueue(self.channel, "MID2", "5561933334444", text, p)
        self.Inbox._cron_process()
        rec = self.Inbox.search([("message_id", "=", "MID2")])
        self.assertEqual(rec.status, "done")
        events1 = self.env["calendar.event"].search_count([("name", "ilike", "33334444")])
        # roda o cron de novo: item 'done' não é reprocessado
        self.Inbox._cron_process()
        events2 = self.env["calendar.event"].search_count([("name", "ilike", "33334444")])
        self.assertEqual(events1, events2, "reprocesso não pode duplicar efeito")
        self.assertGreaterEqual(events1, 1)

    def test_retry_backoff_then_dlq(self):
        p = self._payload("MID3", "5561955556666", "oi")
        rec, _c = self.Inbox._enqueue(self.channel, "MID3", "5561955556666", "oi", p)
        rec.max_attempts = 2
        with patch(
            "odoo.addons.dz23_agent.models.whatsapp_agent.DZ23ChannelAgent.handle_inbound",
            side_effect=Exception("boom"),
        ):
            rec._process_one()
            self.assertEqual(rec.status, "failed")
            self.assertEqual(rec.attempts, 1)
            self.assertGreater(rec.next_attempt_at, fields.Datetime.now())
            rec.next_attempt_at = fields.Datetime.now()
            rec._process_one()
            self.assertEqual(rec.status, "dead", "vai para DLQ após max_attempts")
            self.assertEqual(rec.attempts, 2)

    def test_requeue_from_dlq(self):
        p = self._payload("MID4", "5561977778888", "oi")
        rec, _c = self.Inbox._enqueue(self.channel, "MID4", "5561977778888", "oi", p)
        rec.write({"status": "dead", "attempts": 6})
        rec.action_requeue()
        self.assertEqual(rec.status, "pending")
        self.assertEqual(rec.attempts, 0)
