# Mídia recebida (ADR-009), sem rede: MIME real pelo conteúdo, arquivos perigosos
# recusados, limite de tamanho, sha256, allowlist anti-SSRF, download por provedor
# (Meta em 2 etapas, Evolution base64, Twilio com basic auth), anexo privado da
# empresa, retry em falha de rede e retenção.
import base64
import hashlib
from unittest.mock import MagicMock, patch

import requests
from odoo import fields
from odoo.addons.dz23_whatsapp.models import media_utils as mu
from odoo.tests import TransactionCase, tagged

_GET = "odoo.addons.dz23_whatsapp.models.whatsapp_channel.requests.get"
_POST = "odoo.addons.dz23_whatsapp.models.whatsapp_channel.requests.post"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
PDF = b"%PDF-1.7\n" + b"0" * 64
HTML = b"<!DOCTYPE html><html><script>alert(1)</script></html>"


def _resp(status=200, json_body=None, content=b"", headers=None):
    resp = MagicMock()
    resp.status_code = status
    resp.headers = headers or {}
    resp.json.return_value = json_body if json_body is not None else {}
    resp.iter_content.return_value = [content] if content else []
    return resp


@tagged("post_install", "-at_install", "dz23")
class TestMediaValidation(TransactionCase):
    def test_mime_comes_from_content(self):
        mime, digest = mu.validate_media("image", PNG, "foto.jpg")
        self.assertEqual(mime, "image/png")
        self.assertEqual(digest, hashlib.sha256(PNG).hexdigest())

    def test_html_disguised_as_image_is_rejected(self):
        with self.assertRaises(mu.MediaRejectedError):
            mu.validate_media("image", HTML, "foto.jpg")

    def test_executable_and_blocked_extension_are_rejected(self):
        with self.assertRaises(mu.MediaRejectedError):
            mu.validate_media("document", b"MZ\x90\x00" + b"\x00" * 60, "nota.pdf")
        with self.assertRaises(mu.MediaRejectedError):
            mu.validate_media("document", PDF, "boleto.pdf.exe")
        with self.assertRaises(mu.MediaRejectedError):
            mu.validate_media("image", b"<svg onload=alert(1)></svg>", "logo.png")

    def test_size_limit(self):
        with self.assertRaises(mu.MediaRejectedError):
            mu.validate_media("image", PNG, max_bytes=10)

    def test_sha256_hex_and_base64(self):
        mu.validate_media("image", PNG, expected_sha256=hashlib.sha256(PNG).hexdigest())
        mu.validate_media(
            "image", PNG, expected_sha256=base64.b64encode(hashlib.sha256(PNG).digest()).decode()
        )
        with self.assertRaises(mu.MediaRejectedError):
            mu.validate_media("image", PNG, expected_sha256="0" * 64)

    def test_download_url_allowlist(self):
        self.assertTrue(mu.is_allowed_download_url("https://lookaside.fbsbx.com/x?mid=1"))
        self.assertTrue(
            mu.is_allowed_download_url("https://api.twilio.com/2010-04-01/Accounts/AC/Media/ME")
        )
        self.assertFalse(mu.is_allowed_download_url("http://api.twilio.com/x"))
        self.assertFalse(mu.is_allowed_download_url("https://evil.example.com/x"))
        self.assertFalse(mu.is_allowed_download_url("https://api.twilio.com.evil.example.com/x"))
        self.assertFalse(mu.is_allowed_download_url("https://user:pw@api.twilio.com/x"))
        self.assertFalse(mu.is_allowed_download_url("https://169.254.169.254/latest/meta-data"))

    def test_safe_filename(self):
        self.assertEqual(mu.safe_filename("../../etc/passwd", "text/plain"), "passwd.txt")
        self.assertEqual(mu.safe_filename("", "image/png", fallback="whatsapp-1"), "whatsapp-1.png")


@tagged("post_install", "-at_install", "dz23")
class TestMediaDownload(TransactionCase):
    def setUp(self):
        super().setUp()
        Channel = self.env["dz23.channel"]
        company = self.env.company
        self.meta = Channel.create(
            {
                "name": "Meta Mídia",
                "company_id": company.id,
                "provider": "meta_cloud",
                "meta_phone_id": "PHONE-M1",
                "meta_token": "T",
            }
        )
        self.evo = Channel.create(
            {
                "name": "Evo Mídia",
                "company_id": company.id,
                "provider": "evolution",
                "evo_base": "http://evo.local:8080",
                "evo_instance": "midia_inst",
                "evo_apikey": "K",
            }
        )
        self.twilio = Channel.create(
            {
                "name": "Twilio Mídia",
                "company_id": company.id,
                "provider": "twilio",
                "twilio_sid": "AC_MIDIA",
                "twilio_token": "tok",
                "twilio_from": "+14155238886",
            }
        )
        self.Inbox = self.env["dz23.message.inbox"]
        self.Media = self.env["dz23.message.media"]

    def _media(self, channel, mid, mtype, media, caption=None):
        event = {
            "provider_message_id": mid,
            "sender": "5561999990001",
            "text": False,
            "message_type": mtype,
            "caption": caption,
            "media": media,
            "payload": {"id": mid},
            "occurred_at": None,
        }
        inbox, _created = self.Inbox._enqueue_event(channel, event)
        return self.Media.search([("inbox_id", "=", inbox.id)])

    def test_meta_download_stores_private_company_attachment(self):
        media = self._media(
            self.meta, "wamid.M1", "image", {"media_id": "MEDIA-1", "mime_type": "image/png"}
        )
        self.assertEqual(media.status, "pending")
        info = {
            "url": "https://lookaside.fbsbx.com/whatsapp_business/attachments/?mid=1",
            "mime_type": "image/png",
            "sha256": hashlib.sha256(PNG).hexdigest(),
            "file_size": len(PNG),
        }
        with patch(_GET, side_effect=[_resp(json_body=info), _resp(content=PNG)]) as get:
            self.Media._cron_process()
        self.assertEqual(media.status, "downloaded")
        self.assertEqual(media.mime_type, "image/png")
        self.assertEqual(media.file_size, len(PNG))
        self.assertEqual(get.call_count, 2)
        self.assertEqual(get.call_args.kwargs["headers"]["Authorization"], "Bearer T")
        attachment = media.attachment_id
        self.assertFalse(attachment.public, "anexo nunca é público")
        self.assertEqual(attachment.company_id, self.meta.company_id)
        self.assertEqual(attachment.res_model, "dz23.message.media")
        contact = self.env["dz23.channel.contact"].search(
            [("channel_id", "=", self.meta.id), ("provider_user_id", "=", "5561999990001")]
        )
        self.assertTrue(contact.last_inbound_at, "mensagem recebida abre a janela de 24 h")

    def test_meta_url_outside_allowlist_is_rejected(self):
        media = self._media(self.meta, "wamid.M2", "image", {"media_id": "MEDIA-2"})
        with patch(
            _GET, side_effect=[_resp(json_body={"url": "https://evil.example.com/x"})]
        ) as get:
            self.Media._cron_process()
        self.assertEqual(media.status, "rejected")
        self.assertEqual(get.call_count, 1, "não baixa de host fora da allowlist")

    def test_meta_media_id_path_injection_is_rejected(self):
        media = self._media(self.meta, "wamid.M3", "image", {"media_id": "../../me/accounts"})
        with patch(_GET) as get:
            self.Media._cron_process()
        self.assertEqual(media.status, "rejected")
        self.assertEqual(get.call_count, 0)

    def test_evolution_base64_download(self):
        media = self._media(
            self.evo,
            "EVO-DOC-1",
            "document",
            {"mime_type": "application/pdf", "filename": "boleto.pdf"},
        )
        body = {"base64": base64.b64encode(PDF).decode(), "mimetype": "application/pdf"}
        with patch(_POST, return_value=_resp(json_body=body)):
            self.Media._cron_process()
        self.assertEqual(media.status, "downloaded")
        self.assertEqual(media.mime_type, "application/pdf")
        self.assertEqual(media.filename, "boleto.pdf")

    def test_evolution_html_content_is_rejected(self):
        media = self._media(self.evo, "EVO-IMG-X", "image", {"mime_type": "image/jpeg"})
        body = {"base64": base64.b64encode(HTML).decode(), "mimetype": "image/jpeg"}
        with patch(_POST, return_value=_resp(json_body=body)):
            self.Media._cron_process()
        self.assertEqual(media.status, "rejected")
        self.assertFalse(media.attachment_id)

    def test_twilio_download_uses_basic_auth(self):
        url = "https://api.twilio.com/2010-04-01/Accounts/AC_MIDIA/Messages/MM1/Media/ME1"
        media = self._media(
            self.twilio, "SM-MEDIA-1", "image", {"media_id": url, "mime_type": "image/png"}
        )
        with patch(_GET, return_value=_resp(content=PNG)) as get:
            self.Media._cron_process()
        self.assertEqual(media.status, "downloaded")
        self.assertEqual(get.call_args.kwargs["auth"], ("AC_MIDIA", "tok"))

    def test_network_error_is_retried(self):
        media = self._media(self.meta, "wamid.M4", "image", {"media_id": "MEDIA-4"})
        with patch(_GET, side_effect=requests.exceptions.ConnectTimeout("t")):
            self.Media._cron_process()
        self.assertEqual(media.status, "failed")
        self.assertEqual(media.attempts, 1)

    def test_retention_removes_expired_file(self):
        media = self._media(self.evo, "EVO-IMG-R", "image", {"mime_type": "image/png"})
        with patch(_POST, return_value=_resp(json_body={"base64": base64.b64encode(PNG).decode()})):
            self.Media._cron_process()
        attachment = media.attachment_id
        self.assertTrue(attachment)
        media.write({"expires_at": fields.Datetime.subtract(fields.Datetime.now(), days=1)})
        self.assertEqual(self.Media._cron_purge_expired(), 1)
        self.assertEqual(media.status, "expired")
        self.assertFalse(media.attachment_id)
        self.assertFalse(attachment.exists())
