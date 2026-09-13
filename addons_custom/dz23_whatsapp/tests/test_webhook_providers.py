# Webhooks ponta a ponta por provedor (HTTP real no servidor de teste, sem rede
# externa): Meta (lote + statuses + phone_number_id divergente), Evolution (grupo,
# mídia, fromMe, MESSAGES_UPDATE, CONNECTION_UPDATE) e Twilio (assinatura,
# mensagem, status callback, conta divergente, credencial ausente).
import hashlib
import hmac
import json

from odoo.addons.dz23_whatsapp.models import provider_normalizers as pn
from odoo.addons.dz23_whatsapp.tests.test_provider_normalizers import _meta_payload
from odoo.tests import HttpCase, tagged

_META_SECRET = "app-secret-de-teste"
_TWILIO_TOKEN = "twilio-token-de-teste"


@tagged("post_install", "-at_install", "dz23")
class TestWebhookProviders(HttpCase):
    def setUp(self):
        super().setUp()
        Channel = self.env["dz23.channel"]
        company = self.env.company
        self.meta = Channel.create(
            {
                "name": "Meta Teste",
                "company_id": company.id,
                "provider": "meta_cloud",
                "meta_phone_id": "PHONE-1",
                "meta_token": "T",
                "meta_app_secret": _META_SECRET,
            }
        )
        self.evo = Channel.create(
            {
                "name": "Evo Prov",
                "company_id": company.id,
                "provider": "evolution",
                "evo_base": "http://evo.local:8080",
                "evo_instance": "inst_prov",
                "evo_apikey": "K",
            }
        )
        self.tw = Channel.create(
            {
                "name": "Twilio Teste",
                "company_id": company.id,
                "provider": "twilio",
                "twilio_sid": "AC_TEST",
                "twilio_token": _TWILIO_TOKEN,
                "twilio_from": "+14155238886",
            }
        )
        self.env["ir.config_parameter"].sudo().set_param(
            "dz23.whatsapp.public_base_url", self.base_url()
        )
        self.Inbox = self.env["dz23.message.inbox"]
        self.Outbox = self.env["dz23.message.outbox"]

    # ----- helpers -----
    def _inbox_count(self, channel, **domain):
        self.env.invalidate_all()
        dom = [("channel_id", "=", channel.id)] + [(k, "=", v) for k, v in domain.items()]
        return self.Inbox.search_count(dom)

    def _sent_outbox(self, channel, provider_id):
        ob = self.Outbox._enqueue(channel, "5561999990001", "resposta")
        ob.write({"status": "sent", "provider_message_id": provider_id})
        ob._apply_status("sent")
        return ob

    def _meta_post(self, payload, secret=_META_SECRET):
        raw = json.dumps(payload).encode()
        sig = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        return self.url_open(
            "/dz23/whatsapp/meta/webhook/%s" % self.meta.webhook_token,
            data=raw,
            headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig},
            timeout=30,
        )

    def _evo_post(self, payload):
        return self.url_open(
            "/dz23/whatsapp/evolution/webhook/%s" % self.evo.webhook_token,
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "X-DZ23-Callback": self.evo.callback_secret,
            },
            timeout=30,
        )

    def _evo_upsert(self, mid, message, jid="5561977776666@s.whatsapp.net", from_me=False):
        return {
            "event": "messages.upsert",
            "instance": "inst_prov",
            "data": {
                "key": {"id": mid, "remoteJid": jid, "fromMe": from_me},
                "message": message,
                "messageTimestamp": 1757770000,
            },
        }

    def _tw_post(self, params, channel=None, signature=None):
        channel = channel or self.tw
        path = "/dz23/whatsapp/twilio/webhook/%s" % channel.webhook_token
        if signature is None:
            signature = pn.twilio_signature(_TWILIO_TOKEN, self.base_url() + path, params)
        return self.url_open(
            path, data=params, headers={"X-Twilio-Signature": signature}, timeout=30
        )

    # ----- Meta -----
    def test_meta_batch_messages_and_statuses(self):
        outbox = self._sent_outbox(self.meta, "wamid.OUT1")
        r = self._meta_post(_meta_payload())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._inbox_count(self.meta), 3, "todas as mensagens do lote")
        self.assertEqual(self._inbox_count(self.meta, message_type="image", caption="foto"), 1)
        self.assertEqual(outbox.current_status, "delivered")
        failed = self.env["dz23.message.event"].search([("provider_message_id", "=", "wamid.OUT2")])
        self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.error_code, "131047")
        # reentrega idêntica não duplica nada
        self.assertEqual(self._meta_post(_meta_payload()).status_code, 200)
        self.assertEqual(self._inbox_count(self.meta), 3)

    def test_meta_foreign_phone_number_id_is_409(self):
        payload = _meta_payload()
        payload["entry"][0]["changes"][0]["value"]["metadata"]["phone_number_id"] = "OUTRO"
        r = self._meta_post(payload)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self._inbox_count(self.meta), 0)

    def test_meta_invalid_signature_is_401(self):
        self.assertEqual(self._meta_post(_meta_payload(), secret="errado").status_code, 401)
        self.assertEqual(self._inbox_count(self.meta), 0)

    # ----- Evolution -----
    def test_evolution_group_ignored_media_saved(self):
        group = self._evo_upsert("G1", {"conversation": "oi grupo"}, jid="1203630@g.us")
        self.assertEqual(self._evo_post(group).status_code, 200)
        self.assertEqual(self._inbox_count(self.evo), 0)
        media = self._evo_upsert(
            "IMG1", {"imageMessage": {"caption": "pix", "mimetype": "image/jpeg"}}
        )
        self.assertEqual(self._evo_post(media).status_code, 200)
        self.assertEqual(self._inbox_count(self.evo, message_type="image", caption="pix"), 1)

    def test_evolution_from_me_goes_to_events_not_inbox(self):
        r = self._evo_post(self._evo_upsert("ME1", {"conversation": "enviada"}, from_me=True))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._inbox_count(self.evo), 0)
        ev = self.env["dz23.message.event"].search([("provider_message_id", "=", "ME1")])
        self.assertEqual((ev.message_direction, ev.status), ("outbound", "sent"))

    def test_evolution_messages_update_advances_outbox(self):
        outbox = self._sent_outbox(self.evo, "EVO-OUT-1")
        payload = {
            "event": "MESSAGES_UPDATE",
            "instance": "inst_prov",
            "data": {
                "keyId": "EVO-OUT-1",
                "remoteJid": "5561999990001@s.whatsapp.net",
                "fromMe": True,
                "status": "READ",
            },
        }
        self.assertEqual(self._evo_post(payload).status_code, 200)
        self.env.invalidate_all()
        self.assertEqual(outbox.current_status, "read")

    def test_evolution_connection_update(self):
        payload = {
            "event": "CONNECTION_UPDATE",
            "instance": "inst_prov",
            "data": {"state": "close"},
        }
        self.assertEqual(self._evo_post(payload).status_code, 200)
        self.env.invalidate_all()
        self.assertEqual(self.evo.connection_state, "close")
        self.assertTrue(self.evo.last_webhook_at)

    # ----- Twilio -----
    def _tw_inbound(self, **extra):
        params = {
            "MessageSid": "SM-IN-1",
            "AccountSid": "AC_TEST",
            "From": "whatsapp:+5561999990001",
            "To": "whatsapp:+14155238886",
            "Body": "quero agendar",
            "NumMedia": "0",
            "SmsStatus": "received",
        }
        params.update(extra)
        return params

    def test_twilio_unknown_token_404(self):
        r = self.url_open(
            "/dz23/whatsapp/twilio/webhook/naoexiste", data=self._tw_inbound(), timeout=30
        )
        self.assertEqual(r.status_code, 404)

    def test_twilio_missing_or_wrong_signature_401(self):
        self.assertEqual(self._tw_post(self._tw_inbound(), signature="").status_code, 401)
        self.assertEqual(self._tw_post(self._tw_inbound(), signature="abc=").status_code, 401)
        self.assertEqual(self._inbox_count(self.tw), 0)

    def test_twilio_inbound_is_persisted_once(self):
        r = self._tw_post(self._tw_inbound())
        self.assertEqual(r.status_code, 200)
        self.assertIn("<Response>", r.text)
        self.assertEqual(self._tw_post(self._tw_inbound()).status_code, 200)
        self.assertEqual(self._inbox_count(self.tw, message_id="SM-IN-1"), 1)

    def test_twilio_status_callback_updates_outbox(self):
        outbox = self._sent_outbox(self.tw, "SM-OUT-1")
        params = {
            "MessageSid": "SM-OUT-1",
            "AccountSid": "AC_TEST",
            "MessageStatus": "undelivered",
            "ErrorCode": "63016",
            "From": "whatsapp:+14155238886",
            "To": "whatsapp:+5561999990001",
        }
        self.assertEqual(self._tw_post(params).status_code, 200)
        self.env.invalidate_all()
        self.assertEqual(outbox.current_status, "undelivered")
        self.assertEqual(outbox.provider_error_code, "63016")

    def test_twilio_foreign_account_or_number_is_409(self):
        self.assertEqual(self._tw_post(self._tw_inbound(AccountSid="AC_OUTRA")).status_code, 409)
        self.assertEqual(
            self._tw_post(self._tw_inbound(To="whatsapp:+551130000000")).status_code, 409
        )
        self.assertEqual(self._inbox_count(self.tw), 0)

    def test_twilio_without_credentials_503(self):
        bare = self.env["dz23.channel"].create(
            {"name": "Twilio vazio", "company_id": self.env.company.id, "provider": "twilio"}
        )
        r = self._tw_post(self._tw_inbound(), channel=bare, signature="x")
        self.assertEqual(r.status_code, 503)

    # ----- contrato -----
    def test_invalid_event_is_skipped_not_fatal(self):
        stats = self.evo._ingest_events(
            [
                {"provider": "evolution", "kind": "message"},
                *pn.normalize_evolution(self._evo_upsert("OK1", {"conversation": "oi"})),
            ]
        )
        self.assertEqual(stats["invalid"], 1)
        self.assertEqual(stats["inbox"], 1)
