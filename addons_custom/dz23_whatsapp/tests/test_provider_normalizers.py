# Testes de CONTRATO dos normalizadores de provedor (ADR-007) com fixtures
# anonimizadas: nenhum acesso a rede. Cobrem lote Meta, statuses com erro, mídia,
# interativos, Evolution (texto/fromMe/grupo/broadcast/mídia/wrapper/update/conexão),
# Twilio (texto/mídia/status) e a assinatura X-Twilio-Signature.
from odoo.addons.dz23_whatsapp.models import provider_normalizers as pn
from odoo.tests import TransactionCase, tagged


def _meta_payload():
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA-1",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "556130000000",
                                "phone_number_id": "PHONE-1",
                            },
                            "messages": [
                                {
                                    "from": "5561999990001",
                                    "id": "wamid.A",
                                    "timestamp": "1757770000",
                                    "type": "text",
                                    "text": {"body": "oi"},
                                },
                                {
                                    "from": "5561999990002",
                                    "id": "wamid.B",
                                    "timestamp": "1757770001",
                                    "type": "image",
                                    "image": {
                                        "id": "MEDIA-1",
                                        "mime_type": "image/jpeg",
                                        "sha256": "abc",
                                        "caption": "foto",
                                    },
                                    "context": {"id": "wamid.ORIG"},
                                },
                            ],
                        },
                    },
                    {
                        "field": "messages",
                        "value": {
                            "metadata": {"phone_number_id": "PHONE-1"},
                            "messages": [
                                {
                                    "from": "5561999990003",
                                    "id": "wamid.C",
                                    "timestamp": "1757770002",
                                    "type": "interactive",
                                    "interactive": {
                                        "type": "button_reply",
                                        "button_reply": {"id": "b1", "title": "Sim"},
                                    },
                                }
                            ],
                            "statuses": [
                                {
                                    "id": "wamid.OUT1",
                                    "status": "delivered",
                                    "timestamp": "1757770003",
                                    "recipient_id": "5561999990001",
                                },
                                {
                                    "id": "wamid.OUT2",
                                    "status": "failed",
                                    "timestamp": "1757770004",
                                    "recipient_id": "5561999990002",
                                    "errors": [{"code": 131047, "title": "Re-engagement"}],
                                },
                            ],
                        },
                    },
                ],
            }
        ],
    }


@tagged("post_install", "-at_install", "dz23")
class TestProviderNormalizers(TransactionCase):
    # ----- Meta -----
    def test_meta_batch_processes_all_messages_and_statuses(self):
        events = pn.normalize_meta(_meta_payload())
        messages = [e for e in events if e["kind"] == "message"]
        statuses = [e for e in events if e["kind"] == "status"]
        self.assertEqual(
            [m["provider_message_id"] for m in messages], ["wamid.A", "wamid.B", "wamid.C"]
        )
        self.assertEqual(len(statuses), 2)
        for ev in events:
            pn.validate_event(ev)
            self.assertEqual(ev["channel_ref"], "PHONE-1")
        self.assertEqual(pn.meta_channel_refs(_meta_payload()), {"PHONE-1"})

    def test_meta_media_caption_and_reply(self):
        img = pn.normalize_meta(_meta_payload())[1]
        self.assertEqual(img["message_type"], "image")
        self.assertEqual(img["caption"], "foto")
        self.assertEqual(img["media"]["media_id"], "MEDIA-1")
        self.assertEqual(img["reply_to"], "wamid.ORIG")
        self.assertEqual(img["occurred_at"].year, 2025)

    def test_meta_interactive_and_status_error(self):
        events = pn.normalize_meta(_meta_payload())
        self.assertEqual(events[2]["message_type"], "interactive")
        self.assertEqual(events[2]["text"], "Sim")
        failed = events[4]
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["error_code"], 131047)
        self.assertEqual(failed["direction"], "outbound")

    def test_meta_signature(self):
        raw = b'{"a":1}'
        import hashlib
        import hmac

        sig = "sha256=" + hmac.new(b"segredo", raw, hashlib.sha256).hexdigest()
        self.assertTrue(pn.meta_signature_valid("segredo", raw, sig))
        self.assertFalse(pn.meta_signature_valid("outro", raw, sig))
        self.assertFalse(pn.meta_signature_valid("segredo", raw, "sha1=abc"))
        self.assertFalse(pn.meta_signature_valid("", raw, sig))

    # ----- Evolution -----
    def _evo(
        self, message, jid="5561988887777@s.whatsapp.net", from_me=False, event="messages.upsert"
    ):
        return {
            "event": event,
            "instance": "inst1",
            "data": {
                "key": {"id": "EVO-1", "remoteJid": jid, "fromMe": from_me},
                "message": message,
                "messageTimestamp": 1757770000,
            },
        }

    def test_evolution_text_inbound(self):
        (ev,) = pn.normalize_evolution(self._evo({"conversation": "quero agendar"}))
        pn.validate_event(ev)
        self.assertEqual(ev["direction"], "inbound")
        self.assertEqual(ev["sender"], "5561988887777")
        self.assertEqual(ev["text"], "quero agendar")
        self.assertEqual(ev["channel_ref"], "inst1")

    def test_evolution_from_me_is_outbound_not_inbound(self):
        (ev,) = pn.normalize_evolution(self._evo({"conversation": "resposta"}, from_me=True))
        self.assertEqual(ev["direction"], "outbound")
        self.assertIsNone(ev["sender"])
        self.assertEqual(ev["recipient"], "5561988887777")

    def test_evolution_groups_and_broadcast_are_ignored(self):
        for jid in ("120363000000000000@g.us", "status@broadcast", "1203@newsletter"):
            self.assertEqual(pn.normalize_evolution(self._evo({"conversation": "x"}, jid=jid)), [])

    def test_evolution_media_and_wrapped_messages(self):
        (img,) = pn.normalize_evolution(
            self._evo(
                {
                    "imageMessage": {
                        "caption": "comprovante",
                        "mimetype": "image/png",
                        "fileLength": "2048",
                    }
                }
            )
        )
        self.assertEqual(img["message_type"], "image")
        self.assertEqual(img["caption"], "comprovante")
        self.assertEqual(img["media"]["mime_type"], "image/png")
        (wrapped,) = pn.normalize_evolution(
            self._evo({"ephemeralMessage": {"message": {"extendedTextMessage": {"text": "sumiu"}}}})
        )
        self.assertEqual((wrapped["message_type"], wrapped["text"]), ("text", "sumiu"))
        (loc,) = pn.normalize_evolution(
            self._evo({"locationMessage": {"degreesLatitude": -15.7, "degreesLongitude": -48.2}})
        )
        self.assertEqual(loc["message_type"], "location")
        self.assertEqual(loc["location"]["latitude"], -15.7)

    def test_evolution_legacy_payload_without_event_name(self):
        payload = self._evo({"conversation": "oi"})
        payload.pop("event")
        (ev,) = pn.normalize_evolution(payload)
        self.assertEqual(ev["text"], "oi")

    def test_evolution_messages_update_status(self):
        payload = {
            "event": "MESSAGES_UPDATE",
            "instance": "inst1",
            "data": {
                "keyId": "EVO-OUT-1",
                "remoteJid": "5561988887777@s.whatsapp.net",
                "fromMe": True,
                "status": "DELIVERY_ACK",
            },
        }
        (ev,) = pn.normalize_evolution(payload)
        pn.validate_event(ev)
        self.assertEqual((ev["kind"], ev["status"]), ("status", "delivered"))
        # status de mensagem RECEBIDA (fromMe False) não é acompanhado
        payload["data"]["fromMe"] = False
        self.assertEqual(pn.normalize_evolution(payload), [])

    def test_evolution_connection_update(self):
        (ev,) = pn.normalize_evolution(
            {"event": "connection.update", "instance": "inst1", "data": {"state": "close"}}
        )
        pn.validate_event(ev)
        self.assertEqual((ev["kind"], ev["state"]), ("connection", "close"))

    # ----- Twilio -----
    def _twilio_inbound(self, **extra):
        params = {
            "MessageSid": "SM1",
            "AccountSid": "AC1",
            "From": "whatsapp:+5561999990001",
            "To": "whatsapp:+14155238886",
            "Body": "oi",
            "NumMedia": "0",
            "SmsStatus": "received",
        }
        params.update(extra)
        return params

    def test_twilio_inbound_text(self):
        (ev,) = pn.normalize_twilio(self._twilio_inbound())
        pn.validate_event(ev)
        self.assertEqual(
            (ev["direction"], ev["sender"], ev["text"]), ("inbound", "5561999990001", "oi")
        )
        self.assertEqual(ev["channel_ref"], "AC1")

    def test_twilio_inbound_media(self):
        (ev,) = pn.normalize_twilio(
            self._twilio_inbound(
                Body="",
                NumMedia="1",
                MediaContentType0="audio/ogg",
                MediaUrl0="https://api.twilio.com/m/1",
            )
        )
        self.assertEqual(ev["message_type"], "audio")
        self.assertEqual(ev["media"]["mime_type"], "audio/ogg")

    def test_twilio_status_callback(self):
        (ev,) = pn.normalize_twilio(
            {
                "MessageSid": "SM2",
                "AccountSid": "AC1",
                "MessageStatus": "undelivered",
                "ErrorCode": "63016",
                "To": "whatsapp:+5561999990001",
                "From": "whatsapp:+14155238886",
            }
        )
        pn.validate_event(ev)
        self.assertEqual(
            (ev["kind"], ev["status"], ev["error_code"]), ("status", "undelivered", "63016")
        )

    def test_twilio_signature_roundtrip_and_tamper(self):
        url = "https://crm.example.com/dz23/whatsapp/twilio/webhook/TOKEN"
        params = self._twilio_inbound()
        sig = pn.twilio_signature("auth-token", url, params)
        self.assertTrue(pn.verify_twilio_signature("auth-token", url, params, sig))
        reordered = dict(reversed(list(params.items())))
        self.assertTrue(pn.verify_twilio_signature("auth-token", url, reordered, sig))
        self.assertFalse(pn.verify_twilio_signature("outro-token", url, params, sig))
        self.assertFalse(pn.verify_twilio_signature("auth-token", url + "x", params, sig))
        tampered = dict(params, Body="pague aqui")
        self.assertFalse(pn.verify_twilio_signature("auth-token", url, tampered, sig))
        self.assertFalse(pn.verify_twilio_signature("auth-token", url, params, ""))

    # ----- contrato -----
    def test_validate_event_rejects_invalid(self):
        good = pn.normalize_twilio(self._twilio_inbound())[0]
        for broken in (
            dict(good, provider="whatever"),
            dict(good, kind="noise"),
            dict(good, provider_message_id=None),
            dict(good, direction="up"),
            dict(good, message_type="hologram"),
            dict(good, sender=None),
            dict(good, occurred_at="2025-01-01"),
        ):
            with self.assertRaises(ValueError):
                pn.validate_event(broken)

    def test_parse_timestamp_formats(self):
        self.assertEqual(pn.parse_timestamp("1757770000").year, 2025)
        self.assertEqual(pn.parse_timestamp(1757770000000).year, 2025)
        self.assertEqual(pn.parse_timestamp("2025-09-13T12:00:00Z").hour, 12)
        self.assertIsNone(pn.parse_timestamp("ontem"))
        self.assertIsNone(pn.parse_timestamp(None))
