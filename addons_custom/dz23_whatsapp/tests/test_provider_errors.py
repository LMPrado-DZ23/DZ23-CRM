# Erros de provedor tipados e outbox robusta (ADR-008), com requests.post simulado
# (nenhuma chamada real): classificação, Retry-After, DLQ imediata para erro
# permanente, pausa do canal em 429, limite por canal, reconciliação de ack perdido
# via correlation_id da Meta e reaplicação de eventos pendentes.
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from unittest.mock import MagicMock, patch

import requests
from odoo import fields
from odoo.addons.dz23_whatsapp.models.provider_errors import (
    ProviderPermanentError,
    ProviderTransientError,
    classify_http_error,
    parse_retry_after,
)
from odoo.addons.dz23_whatsapp.models.provider_normalizers import normalize_meta
from odoo.tests import TransactionCase, tagged

_POST = "odoo.addons.dz23_whatsapp.models.whatsapp_channel.requests.post"


def _response(status, body=None, headers=None):
    resp = MagicMock()
    resp.status_code = status
    resp.headers = headers or {}
    if body is None:
        resp.json.side_effect = ValueError("sem json")
    else:
        resp.json.return_value = body
    resp.text = ""
    return resp


@tagged("post_install", "-at_install", "dz23")
class TestProviderErrors(TransactionCase):
    def setUp(self):
        super().setUp()
        Channel = self.env["dz23.channel"]
        self.meta = Channel.create(
            {
                "name": "Meta Erros",
                "company_id": self.env.company.id,
                "provider": "meta_cloud",
                "meta_phone_id": "PHONE-E1",
                "meta_token": "T",
            }
        )
        self.meta_b = Channel.create(
            {
                "name": "Meta Erros B",
                "company_id": self.env.company.id,
                "provider": "meta_cloud",
                "meta_phone_id": "PHONE-E2",
                "meta_token": "T",
            }
        )
        self.Outbox = self.env["dz23.message.outbox"]
        # Aqui se testa a classificação de erros do provedor, não a janela de 24 h
        # (coberta em test_templates_window): considera a janela aberta.
        self.patch(type(self.env["dz23.channel"]), "_service_window_open", lambda *a, **k: True)

    # ----- classificação -----
    def test_classification(self):
        self.assertIsInstance(classify_http_error(400, {}, {}), ProviderPermanentError)
        self.assertIsInstance(classify_http_error(404, {}, {}), ProviderPermanentError)
        err = classify_http_error(429, {"Retry-After": "120"}, {})
        self.assertIsInstance(err, ProviderTransientError)
        self.assertEqual(err.retry_after, 120)
        self.assertIsInstance(classify_http_error(503, {}, None), ProviderTransientError)
        # 400 com código de rate limit da Meta é temporário
        meta_limit = classify_http_error(400, {}, {"error": {"code": 131056}})
        self.assertIsInstance(meta_limit, ProviderTransientError)
        self.assertEqual(meta_limit.code, 131056)
        self.assertIsInstance(classify_http_error(429, {}, {"code": 20429}), ProviderTransientError)

    def test_retry_after_http_date(self):
        now = datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC)
        header = format_datetime(now + timedelta(seconds=90), usegmt=True)
        self.assertEqual(parse_retry_after(header, now=now), 90)
        self.assertIsNone(parse_retry_after("amanhã"))
        self.assertEqual(parse_retry_after("999999"), 86400)

    def test_network_error_is_transient(self):
        with patch(_POST, side_effect=requests.exceptions.ConnectTimeout("t")):
            with self.assertRaises(ProviderTransientError):
                self.meta.send_text("5561999990001", "oi")

    # ----- outbox -----
    def test_permanent_error_goes_straight_to_dlq(self):
        ob = self.Outbox._enqueue(self.meta, "5561999990001", "oi")
        body = {"error": {"code": 131026, "message": "Message undeliverable"}}
        with patch(_POST, return_value=_response(400, body)):
            ob._process_one()
        self.assertEqual(ob.status, "dead")
        self.assertEqual(ob.dlq_reason, "permanent_error")
        self.assertEqual(ob.attempts, 1, "erro permanente não gasta 6 tentativas")
        self.assertEqual(ob.current_status, "failed")
        self.assertEqual(ob.provider_error_code, "131026")
        ob.action_requeue()
        self.assertFalse(ob.dlq_reason)

    def test_rate_limit_respects_retry_after_and_pauses_channel(self):
        first = self.Outbox._enqueue(self.meta, "5561999990001", "um")
        second = self.Outbox._enqueue(self.meta, "5561999990002", "dois")
        before = fields.Datetime.now()
        with patch(_POST, return_value=_response(429, {}, {"Retry-After": "300"})):
            first._process_one()
        self.assertEqual(first.status, "failed")
        self.assertGreaterEqual(first.next_attempt_at, before + timedelta(seconds=299))
        self.assertGreaterEqual(self.meta.rate_limited_until, before + timedelta(seconds=299))
        with patch(_POST, return_value=_response(200, {"messages": [{"id": "wamid.X"}]})) as post:
            self.Outbox._cron_process()
        self.assertEqual(post.call_count, 0, "canal pausado por 429 não envia")
        self.assertEqual(second.status, "pending")

    def test_per_channel_batch_cap(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "dz23.whatsapp.outbox_per_channel_batch", "2"
        )
        burst = self.Outbox.browse()
        for i in range(5):
            burst |= self.Outbox._enqueue(self.meta, "55619999900%02d" % i, "rajada")
        other = self.Outbox._enqueue(self.meta_b, "5561988880000", "outro canal")
        with patch(_POST, return_value=_response(200, {"messages": [{"id": "wamid.OK"}]})):
            self.Outbox._cron_process()
        self.assertEqual(len(burst.filtered(lambda o: o.status == "sent")), 2)
        self.assertEqual(other.status, "sent", "o outro canal não espera a rajada")

    def test_meta_callback_reconciles_lost_ack(self):
        ob = self.Outbox._enqueue(self.meta, "5561999990001", "oi")
        # envio aceito pela Meta, mas o ack se perdeu: item ficou em retry sem id
        ob.write({"status": "failed", "attempts": 1, "next_attempt_at": fields.Datetime.now()})
        payload = {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "metadata": {"phone_number_id": "PHONE-E1"},
                                "statuses": [
                                    {
                                        "id": "wamid.REC",
                                        "status": "sent",
                                        "timestamp": "1757770003",
                                        "recipient_id": "5561999990001",
                                        "biz_opaque_callback_data": ob.correlation_id,
                                    }
                                ],
                            }
                        }
                    ]
                }
            ]
        }
        self.meta._ingest_events(normalize_meta(payload))
        self.assertEqual(ob.provider_message_id, "wamid.REC")
        self.assertEqual(ob.status, "sent")
        self.assertEqual(ob.current_status, "sent")
        with patch(_POST) as post:
            self.Outbox._cron_process()
        self.assertEqual(post.call_count, 0, "item reconciliado não é reenviado")

    def test_correlation_id_is_sent_to_meta(self):
        ob = self.Outbox._enqueue(self.meta, "5561999990001", "oi")
        with patch(_POST, return_value=_response(200, {"messages": [{"id": "wamid.C"}]})) as post:
            ob._process_one()
        sent_json = post.call_args.kwargs["json"]
        self.assertEqual(sent_json["biz_opaque_callback_data"], ob.correlation_id)
        self.assertEqual(ob.client_message_id, ob.correlation_id)

    def test_pending_status_event_is_reapplied_by_cron(self):
        ob = self.Outbox._enqueue(self.meta, "5561999990001", "oi")
        ob.write({"status": "sent", "provider_message_id": "wamid.P"})
        Event = self.env["dz23.message.event"]
        event = {
            "provider": "meta_cloud",
            "provider_message_id": "wamid.P",
            "direction": "outbound",
            "status": "delivered",
        }
        with patch.object(type(self.Outbox), "_apply_status", side_effect=RuntimeError("lock")):
            ev, created = Event._record(self.meta, event)
        self.assertTrue(created, "o evento é persistido mesmo se aplicar falhar")
        self.assertFalse(ev.processed)
        Event._cron_apply_pending()
        self.assertTrue(ev.processed)
        self.assertEqual(ob.current_status, "delivered")
