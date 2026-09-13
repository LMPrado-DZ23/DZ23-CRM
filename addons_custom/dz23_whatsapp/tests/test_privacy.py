# Privacidade e retenção (Fase 10, ADR-013), sem rede: pseudônimo por empresa;
# retenção só de mensagens antigas e finalizadas; exportação só do titular pedido;
# anonimização (textos, identificação, notas, supressão) mantendo o que é fiscal;
# auditoria de acesso append-only e sem valores de credencial.
import json
import uuid
from datetime import timedelta

from odoo import fields
from odoo.addons.dz23_whatsapp.models.privacy import ERASED, EXPORT_TAG, REMOVED, subject_hash
from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, new_test_user, tagged


@tagged("post_install", "-at_install", "dz23")
class TestPrivacy(TransactionCase):
    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.company.write(
            {
                "dz23_message_retention_days": 365,
                "dz23_event_retention_days": 180,
                "dz23_access_log_retention_days": 730,
            }
        )
        self.channel = self.env["dz23.channel"].create(
            {
                "name": "Canal Privacidade",
                "company_id": self.company.id,
                "provider": "evolution",
                "evo_base": "http://privacidade.local:8080",
                "evo_instance": "priv_%s" % uuid.uuid4().hex[:8],
                "evo_apikey": "chave-inicial",
            }
        )
        self.number = "5561988887777"
        self.other_number = "5561977776666"
        Contact = self.env["dz23.channel.contact"]
        self.ident = Contact._touch_inbound(self.channel, self.number).provider_user_id
        self.other_ident = Contact._touch_inbound(self.channel, self.other_number).provider_user_id
        self.Inbox = self.env["dz23.message.inbox"]
        self.Outbox = self.env["dz23.message.outbox"]
        self.Log = self.env["dz23.access.log"]
        self.Suppression = self.env["dz23.privacy.suppression"]
        self.supervisor = new_test_user(
            self.env,
            login="supervisor_priv_qa10",
            groups="dz23_whatsapp.group_dz23_supervisor",
            company_id=self.company.id,
            company_ids=[(6, 0, [self.company.id])],
        )

    def _inbox(self, sender, text, days_ago=0, status="done"):
        rec, _created = self.Inbox._enqueue(
            self.channel, "MID-%s" % uuid.uuid4().hex[:10], sender, text, {"text": text}
        )
        rec.write(
            {
                "status": status,
                "received_at": fields.Datetime.now() - timedelta(days=days_ago),
            }
        )
        return rec

    def _outbox(self, recipient, body, days_ago=0, status="sent"):
        rec = self.Outbox._enqueue(self.channel, recipient, body)
        rec.status = status
        if days_ago:
            self.env.flush_all()
            self.env.cr.execute(
                "UPDATE dz23_message_outbox SET create_date = %s WHERE id = %s",
                (fields.Datetime.now() - timedelta(days=days_ago), rec.id),
            )
            rec.invalidate_recordset(["create_date"])
        return rec

    def _request(self, **vals):
        return (
            self.env["dz23.privacy.request"]
            .with_user(self.supervisor)
            .create({"phone": self.number, **vals})
        )

    def test_subject_hash_is_per_company_and_stable(self):
        first = subject_hash(self.env, self.company.id, self.ident)
        self.assertEqual(first, subject_hash(self.env, self.company.id, self.ident))
        self.assertNotEqual(first, subject_hash(self.env, self.company.id + 1, self.ident))
        self.assertTrue(first.startswith("anon-"))
        self.assertNotIn("988887777", first)

    def test_retention_anonymizes_only_old_finished_messages(self):
        old_done = self._inbox(self.ident, "texto antigo do cliente", days_ago=400)
        old_pending = self._inbox(self.ident, "ainda na fila", days_ago=400, status="pending")
        recent = self._inbox(self.ident, "texto recente", days_ago=10)
        old_sent = self._outbox(self.ident, "resposta antiga", days_ago=400)
        old_queued = self._outbox(self.ident, "resposta na fila", days_ago=400, status="pending")
        event, _created = self.env["dz23.message.event"]._record(
            self.channel,
            {
                "provider": "evolution",
                "provider_message_id": "MID-RETENCAO",
                "direction": "outbound",
                "status": "failed",
                "provider_status": "ERROR",
                "error_message": "falha para 5561988887777",
                "occurred_at": fields.Datetime.now(),
                "payload": {"to": self.ident},
            },
        )
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE dz23_message_event SET received_at = %s WHERE id = %s",
            (fields.Datetime.now() - timedelta(days=200), event.id),
        )
        self.env.invalidate_all()

        totals = self.env["dz23.privacy.retention"]._cron_apply()

        self.assertTrue(old_done.anonymized)
        self.assertEqual(old_done.text, REMOVED)
        self.assertFalse(old_done.payload)
        self.assertTrue(old_done.sender.startswith("anon-"))
        self.assertFalse(old_pending.anonymized, "item ainda na fila não é tocado")
        self.assertEqual(recent.text, "texto recente")
        self.assertEqual(old_sent.body, REMOVED)
        self.assertTrue(old_sent.recipient.startswith("anon-"))
        self.assertEqual(old_queued.body, "resposta na fila")
        self.assertFalse(event.error_message)
        self.assertFalse(event.payload_preview)
        self.assertGreaterEqual(totals["inbox"], 1)
        self.assertEqual(event.status, "failed", "o fato (status) permanece")

    def test_retention_zero_keeps_everything(self):
        self.company.dz23_message_retention_days = 0
        old_done = self._inbox(self.ident, "manter", days_ago=900)
        self.env["dz23.privacy.retention"]._cron_apply()
        self.assertEqual(old_done.text, "manter")

    def test_event_retention_context_only_clears(self):
        event, _created = self.env["dz23.message.event"]._record(
            self.channel,
            {
                "provider": "evolution",
                "provider_message_id": "MID-IMUTAVEL",
                "direction": "outbound",
                "status": "delivered",
                "provider_status": "DELIVERY_ACK",
                "occurred_at": fields.Datetime.now(),
                "payload": {},
            },
        )
        with self.assertRaises(UserError):
            event.with_context(dz23_retention=True).write({"payload_preview": "reescrito"})

    def test_export_contains_only_the_subject(self):
        self._inbox(self.ident, "quero o orçamento do titular")
        self._inbox(self.other_ident, "mensagem de outra pessoa")
        self._outbox(self.ident, "segue o orçamento")
        action = self._request().action_export()
        attachment = self.env["ir.attachment"].browse(
            int(action["url"].split("/")[3].split("?")[0])
        )
        self.assertEqual(attachment.description, EXPORT_TAG)
        exported = json.loads(attachment.raw.decode("utf-8"))
        dumped = json.dumps(exported, ensure_ascii=False)
        self.assertIn("quero o orçamento do titular", dumped)
        self.assertIn("segue o orçamento", dumped)
        self.assertNotIn("outra pessoa", dumped)
        self.assertTrue(
            self.Log.search(
                [("action", "=", "export_subject"), ("user_id", "=", self.supervisor.id)]
            )
        )

    def test_old_exports_expire(self):
        attachment = self.env["ir.attachment"].create(
            {"name": "titular.json", "raw": b"{}", "description": EXPORT_TAG}
        )
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE ir_attachment SET create_date = %s WHERE id = %s",
            (fields.Datetime.now() - timedelta(days=8), attachment.id),
        )
        self.env.invalidate_all()
        self.env["dz23.privacy.retention"]._cron_apply()
        self.assertFalse(attachment.exists())

    def test_anonymize_subject(self):
        conversation = self.env["dz23.conversation"]._for_number(self.channel, self.number)
        inbox = self._inbox(self.ident, "meu CPF é 123.456.789-00")
        outbox = self._outbox(self.ident, "anotado")
        queued = self._outbox(self.ident, "lembrete", status="pending")
        other = self._inbox(self.other_ident, "não é o titular")
        conversation.message_post(body="Cliente informou CPF 123.456.789-00")
        with self.assertRaises(UserError):
            self._request().action_anonymize()

        self._request(reason="Protocolo LGPD 42").action_anonymize()

        self.assertEqual(inbox.text, ERASED)
        self.assertEqual(outbox.body, ERASED)
        self.assertEqual(queued.status, "dead", "envio pendente ao titular é cancelado")
        self.assertEqual(other.text, "não é o titular")
        contact = conversation.contact_id
        self.assertTrue(contact.provider_user_id.startswith("anon-"))
        self.assertEqual((conversation.state, conversation.opt_out), ("blocked", True))
        bodies = " ".join(conversation.message_ids.mapped("body"))
        self.assertNotIn("123.456.789-00", bodies)
        self.assertTrue(self.Suppression.search_count([("reason", "=", "erasure")]))
        self.assertTrue(self.Log.search([("action", "=", "anonymize_subject")]))
        # O titular volta a escrever: contato novo, sem mensagens proativas.
        new_contact = self.env["dz23.channel.contact"]._touch_inbound(self.channel, self.number)
        self.assertNotEqual(new_contact, contact)
        new_conversation = self.env["dz23.conversation"]._for_contact(new_contact)
        self.assertTrue(new_conversation.opt_out)
        self.assertNotEqual(new_conversation.state, "blocked")

    def test_opt_out_creates_suppression(self):
        conversation = self.env["dz23.conversation"]._for_number(self.channel, self.number)
        conversation.opt_out = True
        self.assertTrue(self.Suppression._is_suppressed(self.company, self.ident))
        self.assertFalse(self.Suppression._is_suppressed(self.company, self.other_ident))

    def test_access_audit(self):
        conversation = self.env["dz23.conversation"]._for_number(self.channel, self.number)
        conversation.with_user(self.supervisor).web_read({"name": {}})
        viewed = self.Log.search(
            [("action", "=", "view_conversation"), ("user_id", "=", self.supervisor.id)]
        )
        self.assertEqual(viewed.res_ids, str(conversation.id))
        self.channel.write({"evo_apikey": "nova-chave-de-teste"})
        changed = self.Log.search([("action", "=", "credential_change")], limit=1)
        self.assertIn("evo_apikey", changed.detail)
        self.assertNotIn("nova-chave", changed.detail)
        with self.assertRaises(UserError):
            changed.write({"detail": "alterado"})
        with self.assertRaises(UserError):
            changed.unlink()
        plain = new_test_user(self.env, login="usuario_comum_qa10", groups="base.group_user")
        with self.assertRaises(AccessError):
            self.Log.with_user(plain).search([])
