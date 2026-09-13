# Webhook Woovi via HTTP com assinatura RSA real gerada no teste (chave de teste
# efêmera): assinatura válida persiste o evento, inválida/ausente => 401, corpo
# grande => 413, JSON inválido => 400, reentrega idêntica não duplica.
import base64
import json

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from odoo.tests import HttpCase, tagged

_URL = "/payment/woovi/webhook"


@tagged("post_install", "-at_install", "dz23")
class TestWooviWebhook(HttpCase):
    def setUp(self):
        super().setUp()
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = self.key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        self.env["ir.config_parameter"].sudo().set_param("dz23.woovi.webhook_pubkey", pem.decode())
        self.body = json.dumps(
            {
                "event": "OPENPIX:CHARGE_COMPLETED",
                "charge": {"correlationID": "WEBHOOK-QA-1", "status": "COMPLETED", "value": 500},
            }
        ).encode()

    def _sign(self, body):
        signature = self.key.sign(body, padding.PKCS1v15(), hashes.SHA256())
        return base64.b64encode(signature).decode()

    def _post(self, body, signature):
        headers = {"Content-Type": "application/json"}
        if signature is not None:
            headers["x-webhook-signature"] = signature
        return self.url_open(_URL, data=body, headers=headers, timeout=30)

    def _events(self):
        self.env.invalidate_all()
        return self.env["dz23.woovi.event"].search_count([("correlation_id", "=", "WEBHOOK-QA-1")])

    def test_valid_signature_persists_event_once(self):
        self.assertEqual(self._post(self.body, self._sign(self.body)).status_code, 200)
        self.assertEqual(self._post(self.body, self._sign(self.body)).status_code, 200)
        self.assertEqual(self._events(), 1)

    def test_missing_or_invalid_signature_is_401(self):
        self.assertEqual(self._post(self.body, None).status_code, 401)
        tampered = self.body.replace(b"500", b"5")
        self.assertEqual(self._post(tampered, self._sign(self.body)).status_code, 401)
        self.assertEqual(self._events(), 0)

    def test_invalid_json_is_400(self):
        body = b"{ nope"
        self.assertEqual(self._post(body, self._sign(body)).status_code, 400)

    def test_large_body_is_413(self):
        body = b"{" + b" " * (1024 * 1024 + 10) + b"}"
        self.assertEqual(self._post(body, self._sign(body)).status_code, 413)
