# Claim + lease das filas (ADR-003): payload completo (sem truncar), erro de
# persistência propaga, tentativa contada no claim, recuperação de lease vencido
# e claim concorrente disjunto entre dois cursores reais (SKIP LOCKED).
import json
import uuid
from datetime import timedelta
from unittest.mock import patch

from odoo import SUPERUSER_ID, api, fields
from odoo.addons.dz23_whatsapp.models.queue_utils import claim_due
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install", "dz23")
class TestQueueClaimLease(TransactionCase):
    def setUp(self):
        super().setUp()
        self.channel = self.env["dz23.channel"].create(
            {
                "name": "Canal Claim",
                "company_id": self.env.company.id,
                "provider": "evolution",
                "evo_base": "http://nao-existe.local:8080",
                "evo_instance": "claim_inst",
                "evo_apikey": "K",
            }
        )
        self.Inbox = self.env["dz23.message.inbox"]
        self.Outbox = self.env["dz23.message.outbox"]
        self.ChannelCls = type(self.env["dz23.channel"])

    def _past(self, seconds=120):
        return fields.Datetime.now() - timedelta(seconds=seconds)

    def _inbox(self, mid, text="oi"):
        rec, _created = self.Inbox._enqueue(
            self.channel, mid, "5561911112222", text, {"data": {"key": {"id": mid}}}
        )
        return rec

    # ----- inbox -----
    def test_large_payload_is_stored_whole_and_processed(self):
        big = {"data": {"key": {"id": "BIG1"}, "blob": "x" * 50000}}
        rec, created = self.Inbox._enqueue(self.channel, "BIG1", "5561911112222", "oi", big)
        self.assertTrue(created)
        self.assertEqual(json.loads(rec.payload), big, "payload grande não pode ser truncado")
        self.assertEqual(len(rec.payload_hash), 64)
        self.assertLessEqual(len(rec.payload_preview), 500)
        with patch.object(self.ChannelCls, "handle_inbound", return_value=True):
            self.Inbox._cron_process()
        self.assertEqual(rec.status, "done")
        self.assertTrue(rec.processed_at)

    def test_enqueue_unexpected_error_propagates(self):
        with patch.object(type(self.Inbox), "create", side_effect=RuntimeError("db down")):
            with self.assertRaises(RuntimeError):
                self.Inbox._enqueue(self.channel, "ERR1", "5561911112222", "oi", {})

    def test_enqueue_requires_message_id(self):
        with self.assertRaises(ValueError):
            self.Inbox._enqueue(self.channel, "", "5561911112222", "oi", {})

    def test_attempt_is_counted_at_claim(self):
        rec = self._inbox("CLAIM1")
        with patch.object(self.ChannelCls, "handle_inbound", side_effect=Exception("boom")):
            self.Inbox._cron_process()
        self.assertEqual(rec.status, "failed")
        self.assertEqual(rec.attempts, 1)
        self.assertFalse(rec.lease_until)

    def test_inbox_expired_lease_is_recovered_and_processed(self):
        rec = self._inbox("LEASE1")
        # simula worker morto no meio do processamento
        rec.write({"status": "processing", "attempts": 1, "lease_until": self._past()})
        with patch.object(self.ChannelCls, "handle_inbound", return_value=True):
            self.Inbox._cron_process()
        self.assertEqual(rec.status, "done")
        self.assertEqual(rec.attempts, 2)

    def test_inbox_expired_lease_at_max_attempts_goes_to_dlq(self):
        rec = self._inbox("LEASE2")
        rec.write({"status": "processing", "attempts": 6, "lease_until": self._past()})
        self.Inbox._recover_expired_leases()
        self.assertEqual(rec.status, "dead")

    def test_active_lease_is_not_claimed(self):
        rec = self._inbox("LEASE3")
        future = fields.Datetime.now() + timedelta(minutes=5)
        rec.write({"status": "processing", "attempts": 1, "lease_until": future})
        calls = {"n": 0}

        def _handle(*a, **k):
            calls["n"] += 1
            return True

        with patch.object(self.ChannelCls, "handle_inbound", side_effect=_handle):
            self.Inbox._cron_process()
        self.assertEqual(calls["n"], 0, "item com lease ativo pertence a outro worker")
        self.assertEqual(rec.status, "processing")

    # ----- outbox -----
    def test_outbox_expired_lease_with_provider_id_is_not_resent(self):
        ob = self.Outbox._enqueue(self.channel, "5561999990000", "oi")
        ob.write(
            {
                "status": "sending",
                "attempts": 1,
                "lease_until": self._past(),
                "provider_message_id": "PX-1",
            }
        )
        calls = {"n": 0}

        def _send(*a, **k):
            calls["n"] += 1
            return {"key": {"id": "NOVO"}}

        with patch.object(self.ChannelCls, "send_text", side_effect=_send):
            self.Outbox._cron_process()
        self.assertEqual(ob.status, "sent")
        self.assertEqual(calls["n"], 0)

    def test_outbox_expired_lease_without_id_retries(self):
        ob = self.Outbox._enqueue(self.channel, "5561999990000", "oi")
        ob.write({"status": "sending", "attempts": 1, "lease_until": self._past()})
        self.Outbox._recover_expired_leases()
        self.assertEqual(ob.status, "failed")
        self.assertIn("duplicada", ob.error)
        with patch.object(self.ChannelCls, "send_text", return_value={"key": {"id": "OK-2"}}):
            self.Outbox._cron_process()
        self.assertEqual(ob.status, "sent")
        self.assertEqual(ob.attempts, 2)
        self.assertEqual(ob.current_status, "sent")
        self.assertEqual(ob.provider_message_id, "OK-2")

    # ----- concorrência real (dois cursores) -----
    def test_concurrent_claim_is_disjoint(self):
        ids = set()
        channel_id = None
        with self.registry.cursor() as cr_setup:
            env = api.Environment(cr_setup, SUPERUSER_ID, {})
            channel = env["dz23.channel"].create(
                {
                    "name": "Canal Concorrência",
                    "provider": "evolution",
                    "evo_base": "http://nao-existe.local:8080",
                    "evo_instance": "conc_%s" % uuid.uuid4().hex[:10],
                    "evo_apikey": "K",
                }
            )
            channel_id = channel.id
            for i in range(6):
                ids.add(env["dz23.message.outbox"]._enqueue(channel, "55619000000%02d" % i, "m").id)
            env.flush_all()
            cr_setup.commit()
        cr_a = self.registry.cursor()
        cr_b = self.registry.cursor()
        try:
            got_a = set(
                claim_due(cr_a, "dz23_message_outbox", ("pending", "failed"), "sending", 300, 3)
            )
            got_b = set(
                claim_due(cr_b, "dz23_message_outbox", ("pending", "failed"), "sending", 300, 50)
            )
            self.assertEqual(len(got_a & ids), 3)
            self.assertFalse(got_a & got_b, "dois workers não podem reivindicar o mesmo item")
            self.assertEqual((got_a | got_b) & ids, ids)
        finally:
            cr_a.rollback()
            cr_b.rollback()
            cr_a.close()
            cr_b.close()
            with self.registry.cursor() as cr_clean:
                cr_clean.execute("DELETE FROM dz23_channel WHERE id = %s", (channel_id,))
                cr_clean.commit()
