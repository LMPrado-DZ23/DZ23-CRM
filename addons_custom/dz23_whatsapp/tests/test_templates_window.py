# Templates oficiais e janela de 24 h (ADR-009), sem rede: texto livre fora da janela
# é recusado (nunca contorna a política), dentro da janela sai; template aprovado com
# payload correto (Meta/Twilio/Evolution); não aprovado ou variáveis erradas =>
# DLQ permanente; sincronização de templates da Meta.
import json
from datetime import timedelta
from unittest.mock import MagicMock, patch

from odoo import fields
from odoo.addons.dz23_whatsapp.models.provider_normalizers import normalize_meta
from odoo.tests import TransactionCase, tagged

_POST = "odoo.addons.dz23_whatsapp.models.whatsapp_channel.requests.post"
_SYNC_GET = "odoo.addons.dz23_whatsapp.models.message_template.requests.get"
_NUMBER = "5561999990001"


def _resp(status=200, json_body=None):
    resp = MagicMock()
    resp.status_code = status
    resp.headers = {}
    resp.json.return_value = json_body if json_body is not None else {}
    return resp


@tagged("post_install", "-at_install", "dz23")
class TestTemplatesAndWindow(TransactionCase):
    def setUp(self):
        super().setUp()
        Channel = self.env["dz23.channel"]
        company = self.env.company
        self.meta = Channel.create(
            {
                "name": "Meta Janela",
                "company_id": company.id,
                "provider": "meta_cloud",
                "meta_phone_id": "PHONE-W1",
                "meta_token": "T",
                "meta_waba_id": "WABA-1",
            }
        )
        self.twilio = Channel.create(
            {
                "name": "Twilio Janela",
                "company_id": company.id,
                "provider": "twilio",
                "twilio_sid": "AC_W",
                "twilio_token": "tok",
                "twilio_from": "+14155238886",
            }
        )
        self.evo = Channel.create(
            {
                "name": "Evo Janela",
                "company_id": company.id,
                "provider": "evolution",
                "evo_base": "http://evo.local:8080",
                "evo_instance": "janela_inst",
                "evo_apikey": "K",
            }
        )
        self.Outbox = self.env["dz23.message.outbox"]
        self.Template = self.env["dz23.message.template"]
        self.Contact = self.env["dz23.channel.contact"]

    def _template(self, channel, status="approved", sid=False):
        return self.Template.create(
            {
                "channel_id": channel.id,
                "name": "lembrete_horario",
                "language_code": "pt_BR",
                "body": "Olá {{1}}, seu horário é {{2}}.",
                "status": status,
                "provider_template_id": sid,
            }
        )

    def test_variable_count_and_render(self):
        template = self._template(self.meta)
        self.assertEqual(template.variable_count, 2)
        self.assertEqual(template._render(["Ana", "10:00"]), "Olá Ana, seu horário é 10:00.")

    def test_free_text_outside_window_is_refused(self):
        ob = self.Outbox._enqueue(self.meta, _NUMBER, "oi")
        with patch(_POST) as post:
            ob._process_one()
        self.assertEqual(ob.status, "dead")
        self.assertEqual(ob.dlq_reason, "permanent_error")
        self.assertIn("janela", ob.error)
        self.assertEqual(post.call_count, 0, "nunca tenta contornar a política do provedor")

    def test_free_text_inside_window_is_sent(self):
        self.Contact._touch_inbound(self.meta, _NUMBER)
        ob = self.Outbox._enqueue(self.meta, _NUMBER, "oi")
        with patch(_POST, return_value=_resp(json_body={"messages": [{"id": "wamid.W1"}]})):
            ob._process_one()
        self.assertEqual(ob.status, "sent")

    def test_window_closes_after_24h(self):
        self.Contact._touch_inbound(
            self.twilio, _NUMBER, fields.Datetime.now() - timedelta(hours=25)
        )
        ob = self.Outbox._enqueue(self.twilio, _NUMBER, "oi")
        with patch(_POST) as post:
            ob._process_one()
        self.assertEqual(ob.status, "dead")
        self.assertEqual(post.call_count, 0)

    def test_inbound_webhook_opens_window(self):
        payload = {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "metadata": {"phone_number_id": "PHONE-W1"},
                                "messages": [
                                    {
                                        "from": _NUMBER,
                                        "id": "wamid.IN-W",
                                        "timestamp": str(int(fields.Datetime.now().timestamp())),
                                        "type": "text",
                                        "text": {"body": "oi"},
                                    }
                                ],
                            }
                        }
                    ]
                }
            ]
        }
        self.meta._ingest_events(normalize_meta(payload))
        self.assertTrue(self.meta._service_window_open(_NUMBER))

    def test_evolution_has_no_window(self):
        ob = self.Outbox._enqueue(self.evo, _NUMBER, "oi")
        with patch(_POST, return_value=_resp(json_body={"key": {"id": "EVO-W"}})):
            ob._process_one()
        self.assertEqual(ob.status, "sent")

    def test_meta_template_payload_outside_window(self):
        template = self._template(self.meta)
        ob = self.Outbox._enqueue(
            self.meta, _NUMBER, None, template=template, params=["Ana", "10:00"]
        )
        self.assertEqual(ob.body, "Olá Ana, seu horário é 10:00.")
        with patch(_POST, return_value=_resp(json_body={"messages": [{"id": "wamid.T1"}]})) as post:
            ob._process_one()
        self.assertEqual(ob.status, "sent")
        sent = post.call_args.kwargs["json"]
        self.assertEqual(sent["type"], "template")
        self.assertEqual(sent["template"]["name"], "lembrete_horario")
        self.assertEqual(sent["template"]["language"], {"code": "pt_BR"})
        self.assertEqual(
            [p["text"] for p in sent["template"]["components"][0]["parameters"]], ["Ana", "10:00"]
        )

    def test_unapproved_template_is_permanent(self):
        template = self._template(self.meta, status="pending")
        ob = self.Outbox._enqueue(
            self.meta, _NUMBER, None, template=template, params=["Ana", "10:00"]
        )
        with patch(_POST) as post:
            ob._process_one()
        self.assertEqual((ob.status, ob.dlq_reason), ("dead", "permanent_error"))
        self.assertEqual(post.call_count, 0)

    def test_wrong_param_count_is_permanent(self):
        template = self._template(self.meta)
        ob = self.Outbox._enqueue(self.meta, _NUMBER, None, template=template, params=["Ana"])
        with patch(_POST) as post:
            ob._process_one()
        self.assertEqual(ob.status, "dead")
        self.assertEqual(post.call_count, 0)

    def test_twilio_template_uses_content_sid(self):
        template = self._template(self.twilio, sid="HX123")
        ob = self.Outbox._enqueue(
            self.twilio, _NUMBER, None, template=template, params=["Ana", "10:00"]
        )
        with patch(_POST, return_value=_resp(json_body={"sid": "SM-T1"})) as post:
            ob._process_one()
        self.assertEqual(ob.status, "sent")
        data = post.call_args.kwargs["data"]
        self.assertEqual(data["ContentSid"], "HX123")
        self.assertEqual(json.loads(data["ContentVariables"]), {"1": "Ana", "2": "10:00"})

    def test_evolution_template_sends_rendered_text(self):
        template = self._template(self.evo)
        ob = self.Outbox._enqueue(
            self.evo, _NUMBER, None, template=template, params=["Ana", "10:00"]
        )
        with patch(_POST, return_value=_resp(json_body={"key": {"id": "EVO-T"}})) as post:
            ob._process_one()
        self.assertEqual(ob.status, "sent")
        self.assertEqual(post.call_args.kwargs["json"]["text"], "Olá Ana, seu horário é 10:00.")

    def test_sync_meta_templates_upserts(self):
        data = {
            "data": [
                {
                    "name": "confirmacao",
                    "language": "pt_BR",
                    "status": "APPROVED",
                    "category": "UTILITY",
                    "id": "987",
                    "components": [{"type": "BODY", "text": "Oi {{1}}, confirmado para {{2}}."}],
                }
            ]
        }
        with patch(_SYNC_GET, return_value=_resp(json_body=data)):
            self.assertEqual(self.Template._sync_meta(self.meta), 1)
        template = self.Template.search(
            [("channel_id", "=", self.meta.id), ("name", "=", "confirmacao")]
        )
        self.assertEqual((template.status, template.variable_count), ("approved", 2))
        data["data"][0]["status"] = "PAUSED"
        with patch(_SYNC_GET, return_value=_resp(json_body=data)):
            self.Template._sync_meta(self.meta)
        self.assertEqual(
            self.Template.search_count(
                [("channel_id", "=", self.meta.id), ("name", "=", "confirmacao")]
            ),
            1,
        )
        self.assertEqual(template.status, "paused")
