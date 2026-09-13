# Arquivo do cliente validado chega ao lead como anexo (cópia), sem rede.
import base64
import uuid
from unittest.mock import MagicMock, patch

from odoo.tests import TransactionCase, tagged

_POST = "odoo.addons.dz23_whatsapp.models.whatsapp_channel.requests.post"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@tagged("post_install", "-at_install", "dz23")
class TestMediaToLead(TransactionCase):
    def setUp(self):
        super().setUp()
        self.channel = self.env["dz23.channel"].create(
            {
                "name": "Canal Mídia Lead",
                "company_id": self.env.company.id,
                "provider": "evolution",
                "evo_base": "http://evo.local:8080",
                "evo_instance": "midia_lead_%s" % uuid.uuid4().hex[:8],
                "evo_apikey": "K",
            }
        )
        self.number = "5561900000055"
        self.lead = self.channel._agent_contact(self.number).lead_id

    def test_downloaded_file_is_attached_to_lead(self):
        event = {
            "provider_message_id": "EVO-IMG-LEAD",
            "sender": self.number,
            "text": False,
            "message_type": "image",
            "caption": "comprovante",
            "media": {"mime_type": "image/png"},
            "payload": {},
            "occurred_at": None,
        }
        self.env["dz23.message.inbox"]._enqueue_event(self.channel, event)
        response = MagicMock(status_code=200, headers={})
        response.json.return_value = {"base64": base64.b64encode(PNG).decode()}
        with patch(_POST, return_value=response):
            self.env["dz23.message.media"]._cron_process()
        attachments = self.env["ir.attachment"].search(
            [("res_model", "=", "crm.lead"), ("res_id", "=", self.lead.id)]
        )
        self.assertEqual(len(attachments), 1)
        self.assertFalse(attachments.public)
        self.assertTrue(
            self.lead.message_ids.filtered(lambda m: attachments in m.attachment_ids),
            "o anexo aparece no chatter do lead",
        )
        # Auditoria B-2: a retenção (e a anonimização, que usa o mesmo método) apaga
        # também a cópia do arquivo no lead.
        media = self.env["dz23.message.media"].search([("channel_id", "=", self.channel.id)])
        self.assertEqual(media.lead_attachment_id, attachments)
        media.write({"expires_at": "2020-01-01 00:00:00"})
        self.env["dz23.message.media"]._cron_purge_expired()
        self.assertFalse(attachments.exists(), "cópia no lead removida pela retenção")
        self.assertEqual(media.status, "expired")
