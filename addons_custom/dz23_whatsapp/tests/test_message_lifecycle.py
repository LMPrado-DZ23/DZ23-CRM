# Ciclo de vida da mensagem (ADR-006): eventos append-only com dedupe, status
# normalizado por provedor e transições MONOTÔNICAS na outbox (evento atrasado não
# regride; falha depois de entregue só registra o erro).
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.addons.dz23_whatsapp.models.message_event import normalize_status
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install", "dz23")
class TestMessageLifecycle(TransactionCase):
    def setUp(self):
        super().setUp()
        self.channel = self.env["dz23.channel"].create(
            {
                "name": "Canal Ciclo",
                "company_id": self.env.company.id,
                "provider": "evolution",
                "evo_base": "http://nao-existe.local:8080",
                "evo_instance": "ciclo_inst",
                "evo_apikey": "K",
            }
        )
        self.Event = self.env["dz23.message.event"]
        self.outbox = self.env["dz23.message.outbox"]._enqueue(self.channel, "5561999990000", "oi")
        self.outbox.write({"status": "sent", "provider_message_id": "WAMID-1"})
        self.t0 = fields.Datetime.now()
        self.outbox._apply_status("sent", occurred_at=self.t0)

    def _event(self, status, minutes=1, mid="WAMID-1", **extra):
        vals = {
            "provider": "evolution",
            "provider_message_id": mid,
            "direction": "outbound",
            "status": status,
            "provider_status": status.upper(),
            "occurred_at": self.t0 + timedelta(minutes=minutes),
            "payload": {"status": status},
        }
        vals.update(extra)
        return self.Event._record(self.channel, vals)

    def test_forward_progression(self):
        self._event("delivered", 1)
        self._event("read", 2)
        self.assertEqual(self.outbox.current_status, "read")
        self.assertTrue(self.outbox.sent_at)
        self.assertTrue(self.outbox.delivered_at)
        self.assertTrue(self.outbox.read_at)
        self.assertEqual(self.outbox.last_status_at, self.t0 + timedelta(minutes=2))

    def test_late_event_does_not_regress(self):
        self._event("read", 5)
        ev, created = self._event("delivered", 3)
        self.assertTrue(created)
        self.assertEqual(self.outbox.current_status, "read", "evento atrasado não regride")
        self.assertEqual(self.outbox.delivered_at, self.t0 + timedelta(minutes=3))
        self.assertEqual(self.outbox.last_status_at, self.t0 + timedelta(minutes=5))
        self.assertEqual(ev.outbox_id, self.outbox)
        self.assertTrue(ev.processed)

    def test_failure_after_delivered_records_error_only(self):
        self._event("delivered", 1)
        self._event("failed", 2, error_code="131026", error_message="Message undeliverable")
        self.assertEqual(self.outbox.current_status, "delivered")
        self.assertEqual(self.outbox.provider_error_code, "131026")
        self.assertFalse(self.outbox.failed_at)

    def test_failure_before_delivery_then_confirmed(self):
        self._event("failed", 1, error_code="470")
        self.assertEqual(self.outbox.current_status, "failed")
        self.assertTrue(self.outbox.failed_at)
        # confirmação posterior do provedor prevalece (rank maior)
        self._event("delivered", 2)
        self.assertEqual(self.outbox.current_status, "delivered")

    def test_unknown_never_changes_status(self):
        self._event("unknown", 1)
        self.assertEqual(self.outbox.current_status, "sent")

    def test_duplicate_callback_is_deduplicated(self):
        ev1, created1 = self._event("delivered", 1)
        ev2, created2 = self._event("delivered", 1)
        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(ev1, ev2)
        self.assertEqual(
            self.Event.search_count(
                [("provider_message_id", "=", "WAMID-1"), ("status", "=", "delivered")]
            ),
            1,
        )

    def test_event_is_append_only(self):
        ev, _created = self._event("delivered", 1)
        with self.assertRaises(UserError):
            ev.write({"status": "read"})

    def test_event_without_outbox_is_kept(self):
        ev, created = self._event("delivered", 1, mid="EXTERNO-9")
        self.assertTrue(created)
        self.assertFalse(ev.outbox_id)
        # Dentro da janela o evento fica pendente (o envio pode ainda não ter gravado o id).
        self.assertFalse(ev.processed)
        self.assertEqual(self.outbox.current_status, "sent")
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE dz23_message_event SET received_at = %s WHERE id = %s",
            (fields.Datetime.now() - timedelta(hours=1), ev.id),
        )
        self.env.invalidate_all()
        self.Event._cron_apply_pending()
        self.assertTrue(ev.processed, "fora da janela é encerrado sem outbox")

    def test_contract_validation(self):
        with self.assertRaises(ValueError):
            self.Event._record(
                self.channel, {"provider": "evolution", "direction": "outbound", "status": "read"}
            )
        with self.assertRaises(ValueError):
            self._event("read", 1, direction="sideways")
        with self.assertRaises(ValueError):
            self._event("lida", 1)

    def test_error_message_is_sanitized(self):
        self._event("failed", 1, mid="EXTERNO-1", error_message="falhou para 5561999990000")
        ev = self.Event.search([("provider_message_id", "=", "EXTERNO-1")])
        self.assertNotIn("5561999990000", ev.error_message)

    def test_normalize_status_per_provider(self):
        self.assertEqual(normalize_status("meta_cloud", "read"), "read")
        self.assertEqual(normalize_status("meta_cloud", "deleted"), "cancelled")
        self.assertEqual(normalize_status("twilio", "undelivered"), "undelivered")
        self.assertEqual(normalize_status("twilio", "sending"), "queued")
        self.assertEqual(normalize_status("evolution", "DELIVERY_ACK"), "delivered")
        self.assertEqual(normalize_status("evolution", 4), "read")
        self.assertEqual(normalize_status("evolution", "xyz"), "unknown")
        self.assertEqual(normalize_status("desconhecido", "read"), "unknown")

    def test_dlq_sets_failed_lifecycle(self):
        ob = self.env["dz23.message.outbox"]._enqueue(self.channel, "5561999990001", "oi")
        ob.max_attempts = 1
        with patch.object(
            type(self.env["dz23.channel"]), "send_text", side_effect=Exception("boom")
        ):
            ob._process_one()
        self.assertEqual(ob.status, "dead")
        self.assertEqual(ob.current_status, "failed")
        self.assertTrue(ob.failed_at)
