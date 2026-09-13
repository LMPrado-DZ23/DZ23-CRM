# PIX Woovi idempotente e conciliado (Fase 5), com a API simulada (sem rede):
# renderização repetida não duplica cobrança, só BRL, valor obrigatório para
# confirmar, valor/moeda divergente, evento duplicado, fora de ordem, expiração,
# estorno, conciliação periódica e retry de evento pendente.
from odoo.exceptions import ValidationError
from odoo.tests import TransactionCase, tagged

_REF = "WOOVI-QA-%s"


@tagged("post_install", "-at_install", "dz23")
class TestWoovi(TransactionCase):
    def setUp(self):
        super().setUp()
        self.brl = self.env.ref("base.BRL")
        self.brl.active = True
        self.provider = self.env.ref("dz23_payment_woovi.payment_provider_woovi")
        self.provider.write({"state": "test", "woovi_app_id": "app-id-de-teste"})
        self.method = self.env.ref("dz23_payment_woovi.payment_method_woovi")
        self.partner = self.env["res.partner"].create({"name": "Cliente PIX QA"})
        self.Tx = self.env["payment.transaction"]
        self.Event = self.env["dz23.woovi.event"]
        self.api_calls = []
        self.tx = self._tx("1", 10.50)

    def _tx(self, suffix, amount, currency=None):
        return self.Tx.create(
            {
                "provider_id": self.provider.id,
                "payment_method_id": self.method.id,
                "amount": amount,
                "currency_id": (currency or self.brl).id,
                "partner_id": self.partner.id,
                "reference": _REF % suffix,
                "operation": "online_direct",
            }
        )

    def _mock_api(self, responses):
        """responses: dict endpoint_prefix -> dict | Exception."""

        def _send(tx, method, endpoint, **kwargs):
            self.api_calls.append((method, endpoint, kwargs.get("json")))
            for prefix, answer in responses.items():
                if endpoint.startswith(prefix):
                    if isinstance(answer, Exception):
                        raise answer
                    return answer(tx) if callable(answer) else answer
            raise AssertionError("endpoint inesperado %s" % endpoint)

        self.patch(type(self.Tx), "_send_api_request", _send)

    def _charge(self, tx, status="ACTIVE", **extra):
        charge = {
            "correlationID": tx.reference,
            "value": 1050,
            "status": status,
            "identifier": "CH-%s" % tx.reference,
            "brCode": "00020126QA",
            "qrCodeImage": "https://api.woovi.com/openpix/charge/brcode/image/qa.png",
            "expiresDate": "2099-01-01T00:00:00.000Z",
        }
        charge.update(extra)
        return {"charge": charge}

    def _with_charge(self, tx=None):
        tx = tx or self.tx
        self._mock_api({"/charge": lambda t: self._charge(t)})
        tx._get_specific_rendering_values({})
        return tx

    def _completed(self, tx=None, **extra):
        tx = tx or self.tx
        payload = {
            "event": "OPENPIX:CHARGE_COMPLETED",
            "charge": {"correlationID": tx.reference, "status": "COMPLETED", "value": 1050},
            "pix": {"value": 1050, "endToEndId": "E2E-%s" % tx.reference},
        }
        for key, value in extra.items():
            if value is None:
                payload.pop(key, None)
            else:
                payload[key] = value
        return payload

    # ----- cobrança -----
    def test_repeated_render_creates_single_charge(self):
        self._mock_api({"/charge": lambda t: self._charge(t)})
        first = self.tx._get_specific_rendering_values({})
        second = self.tx._get_specific_rendering_values({})
        self.assertEqual(len(self.api_calls), 1, "recarregar a página não cria nova cobrança")
        self.assertEqual(first, second)
        self.assertEqual(
            self.api_calls[0][2],
            {"correlationID": self.tx.reference, "value": 1050, "comment": self.tx.reference},
        )
        self.assertEqual(self.tx.woovi_charge_id, "CH-%s" % self.tx.reference)
        self.assertEqual(self.tx.state, "pending")
        self.assertTrue(self.tx.woovi_expires_at)

    def test_only_brl_is_accepted(self):
        usd = self.env.ref("base.USD")
        usd.active = True
        tx = self._tx("USD", 10.0, currency=usd)
        self._mock_api({"/charge": lambda t: self._charge(t)})
        self.assertEqual(tx._get_specific_rendering_values({}), {})
        self.assertEqual(tx.state, "error")
        self.assertFalse(self.api_calls)

    def test_invalid_amount_is_rejected(self):
        tx = self._tx("ZERO", 0.0)
        self._mock_api({"/charge": lambda t: self._charge(t)})
        tx._get_specific_rendering_values({})
        self.assertEqual(tx.state, "error")
        self.assertFalse(self.api_calls)

    def test_expired_charge_is_not_recreated(self):
        self._with_charge()
        self.tx.write({"woovi_expires_at": "2020-01-01 00:00:00"})
        values = self.tx._get_specific_rendering_values({})
        self.assertEqual(len(self.api_calls), 1)
        self.assertEqual(values["woovi_status"], "expired")
        self.assertEqual(self.tx.state, "cancel")

    def test_api_error_sets_error_state(self):
        self._mock_api({"/charge": ValidationError("recusado")})
        self.assertEqual(self.tx._get_specific_rendering_values({}), {})
        self.assertEqual(self.tx.state, "error")

    # ----- eventos -----
    def test_completed_with_value_confirms(self):
        self._with_charge()
        event, created = self.Event._ingest_payload(self._completed())
        self.assertTrue(created)
        self.assertEqual(event.state, "done")
        self.assertEqual(self.tx.state, "done")
        self.assertEqual(self.tx.woovi_paid_cents, 1050)

    def test_duplicate_event_has_single_effect(self):
        self._with_charge()
        self.Event._ingest_payload(self._completed())
        _event, created = self.Event._ingest_payload(self._completed())
        self.assertFalse(created)
        self.assertEqual(self.Event.search_count([("correlation_id", "=", self.tx.reference)]), 1)

    def test_missing_value_does_not_confirm(self):
        self._with_charge()
        payload = self._completed(pix=None)
        payload["charge"].pop("value")
        event, _created = self.Event._ingest_payload(payload)
        self.assertEqual(event.state, "failed")
        self.assertIn("ausente", event.error)
        self.assertEqual(self.tx.state, "pending")

    def test_value_mismatch_is_rejected(self):
        self._with_charge()
        event, _created = self.Event._ingest_payload(self._completed(pix={"value": 999}))
        self.assertEqual(event.state, "rejected")
        self.assertEqual(self.tx.state, "error")

    def test_currency_mismatch_is_rejected(self):
        self._with_charge()
        payload = self._completed()
        payload["charge"]["currency"] = "USD"
        event, _created = self.Event._ingest_payload(payload)
        self.assertEqual(event.state, "rejected")
        self.assertNotEqual(self.tx.state, "done")

    def test_out_of_order_expired_after_completed_is_ignored(self):
        self._with_charge()
        self.Event._ingest_payload(self._completed())
        late, _created = self.Event._ingest_payload(
            {
                "event": "OPENPIX:CHARGE_EXPIRED",
                "charge": {"correlationID": self.tx.reference, "status": "EXPIRED"},
            }
        )
        self.assertEqual(late.state, "ignored")
        self.assertEqual(self.tx.state, "done")

    def test_expired_event_cancels_pending(self):
        self._with_charge()
        self.Event._ingest_payload(
            {
                "event": "OPENPIX:CHARGE_EXPIRED",
                "charge": {"correlationID": self.tx.reference, "status": "EXPIRED"},
            }
        )
        self.assertEqual(self.tx.state, "cancel")
        self.assertEqual(self.tx.woovi_status, "expired")

    def test_refund_is_recorded(self):
        self._with_charge()
        self.Event._ingest_payload(self._completed())
        refund, _created = self.Event._ingest_payload(
            {
                "event": "OPENPIX:TRANSACTION_REFUND_RECEIVED",
                "charge": {"correlationID": self.tx.reference, "status": "REFUNDED"},
            }
        )
        self.assertEqual(refund.state, "done")
        self.assertEqual(self.tx.woovi_status, "refunded")

    def test_unknown_transaction_is_ignored(self):
        event, _created = self.Event._ingest_payload(
            {
                "event": "OPENPIX:CHARGE_COMPLETED",
                "charge": {"correlationID": "NAO-EXISTE", "status": "COMPLETED", "value": 1},
            }
        )
        self.assertEqual(event.state, "ignored")

    def test_reconcile_cron_applies_remote_status(self):
        self._with_charge()
        self._mock_api({"/charge/": lambda t: self._charge(t, status="COMPLETED")})
        self.Tx._cron_woovi_reconcile()
        self.assertEqual(self.tx.state, "done")
        event = self.Event.search([("correlation_id", "=", self.tx.reference)])
        self.assertEqual(event.source, "reconcile")

    def test_pending_event_is_retried_by_cron(self):
        self._with_charge()
        original = type(self.Tx)._woovi_apply_payment_data

        def _flaky(tx, data):
            raise RuntimeError("lock")

        self.patch(type(self.Tx), "_woovi_apply_payment_data", _flaky)
        event, _created = self.Event._ingest_payload(self._completed())
        self.assertEqual(event.state, "pending")
        self.patch(type(self.Tx), "_woovi_apply_payment_data", original)
        self.Event._cron_process_pending()
        self.assertEqual(event.state, "done")
        self.assertEqual(self.tx.state, "done")
