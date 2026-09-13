# Limites dos webhooks de WhatsApp (Fase 11), HTTP real no servidor de teste:
# JSON inválido mesmo com assinatura válida (400), lote acima de 1000 eventos (413,
# nada gravado) e corpo acima de 1 MiB (413).
import hashlib
import hmac
import json

from odoo.tests import HttpCase, tagged

_META_SECRET = "segredo-meta-limites"


@tagged("post_install", "-at_install", "dz23")
class TestWebhookLimits(HttpCase):
    def setUp(self):
        super().setUp()
        Channel = self.env["dz23.channel"]
        self.meta = Channel.create(
            {
                "name": "Meta Limites",
                "company_id": self.env.company.id,
                "provider": "meta_cloud",
                "meta_phone_id": "PHONE-LIMITES",
                "meta_token": "T",
                "meta_app_secret": _META_SECRET,
            }
        )
        self.evo = Channel.create(
            {
                "name": "Evolution Limites",
                "company_id": self.env.company.id,
                "provider": "evolution",
                "evo_base": "http://limites.local:8080",
                "evo_instance": "inst_limites",
                "evo_apikey": "K",
            }
        )

    def _meta_post(self, raw):
        signature = "sha256=" + hmac.new(_META_SECRET.encode(), raw, hashlib.sha256).hexdigest()
        return self.url_open(
            "/dz23/whatsapp/meta/webhook/%s" % self.meta.webhook_token,
            data=raw,
            headers={"Content-Type": "application/json", "X-Hub-Signature-256": signature},
            timeout=60,
        )

    def test_meta_invalid_json_with_valid_signature_is_400(self):
        self.assertEqual(self._meta_post(b"{ nao-e-json").status_code, 400)

    def test_meta_batch_over_limit_is_413_and_nothing_is_stored(self):
        statuses = [
            {
                "id": "wamid.LIMITE.%d" % index,
                "status": "delivered",
                "timestamp": "1757770000",
                "recipient_id": "5561999990000",
            }
            for index in range(1001)
        ]
        payload = {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "WABA-LIMITES",
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "messaging_product": "whatsapp",
                                "metadata": {"phone_number_id": "PHONE-LIMITES"},
                                "statuses": statuses,
                            },
                        }
                    ],
                }
            ],
        }
        response = self._meta_post(json.dumps(payload).encode())
        self.assertEqual(response.status_code, 413)
        self.env.invalidate_all()
        self.assertEqual(
            self.env["dz23.message.event"].search_count([("channel_id", "=", self.meta.id)]), 0
        )

    def test_evolution_body_over_1mib_is_413(self):
        body = json.dumps(
            {"instance": "inst_limites", "data": {"pad": "x" * (1024 * 1024 + 16)}}
        ).encode()
        response = self.url_open(
            "/dz23/whatsapp/evolution/webhook/%s" % self.evo.webhook_token,
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-DZ23-Callback": self.evo.callback_secret,
            },
            timeout=60,
        )
        self.assertEqual(response.status_code, 413)
