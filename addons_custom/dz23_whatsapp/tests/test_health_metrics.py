# Observabilidade (Fase 9, ADR-012), sem rede: regras de saúde do canal, saúde lida
# das filas, métricas diárias (coorte de entrega/leitura, latência do webhook, 1ª
# resposta, DLQ, recálculo idempotente), fuso da empresa, isolamento por empresa e
# log estruturado sem PII.
import logging
import uuid
from datetime import date, datetime, timedelta

import pytz
from odoo import fields
from odoo.addons.dz23_whatsapp.models.metrics import day_bounds, ratio
from odoo.addons.dz23_whatsapp.models.queue_utils import log_event
from odoo.addons.dz23_whatsapp.models.whatsapp_channel_health import evaluate_health
from odoo.tests import TransactionCase, new_test_user, tagged


def _channel(env, name, company):
    return env["dz23.channel"].create(
        {
            "name": name,
            "company_id": company.id,
            "provider": "evolution",
            "evo_base": "http://obs.local:8080",
            "evo_instance": "obs_%s" % uuid.uuid4().hex[:8],
            "evo_apikey": "K",
        }
    )


@tagged("post_install", "-at_install", "dz23")
class TestHealthMetrics(TransactionCase):
    def setUp(self):
        super().setUp()
        self.channel = _channel(self.env, "Canal Observabilidade", self.env.company)
        self.Inbox = self.env["dz23.message.inbox"]
        self.Outbox = self.env["dz23.message.outbox"]
        self.Event = self.env["dz23.message.event"]
        self.Metrics = self.env["dz23.metrics.daily"]

    # ---------- regras ----------
    def test_health_rules(self):
        now = datetime(2026, 9, 13, 12, 0)
        self.assertEqual(evaluate_health({"provider": "meta_cloud"}, now), ("ok", []))
        state, notes = evaluate_health({"provider": "evolution", "connection_state": "close"}, now)
        self.assertEqual(state, "critical")
        self.assertIn("desconectado", notes[0])
        self.assertEqual(
            evaluate_health({"provider": "evolution", "connection_state": "open"}, now)[0], "ok"
        )
        late = {"provider": "meta_cloud", "oldest_outbox_pending_at": now - timedelta(minutes=10)}
        self.assertEqual(evaluate_health(late, now)[0], "warning")
        late["oldest_outbox_pending_at"] = now - timedelta(minutes=45)
        self.assertEqual(evaluate_health(late, now)[0], "critical")
        state, notes = evaluate_health({"provider": "twilio", "dlq_inbox_count": 2}, now)
        self.assertEqual(state, "warning")
        self.assertIn("2 item", notes[0])
        no_status = {"provider": "twilio", "last_sent_at": now - timedelta(hours=2)}
        self.assertEqual(evaluate_health(no_status, now)[0], "warning")
        no_status["last_status_at"] = now - timedelta(hours=1)
        self.assertEqual(evaluate_health(no_status, now)[0], "ok")
        paused = {"provider": "twilio", "rate_limited_until": now + timedelta(minutes=5)}
        self.assertEqual(evaluate_health(paused, now)[0], "warning")

    def test_channel_health_reads_queues(self):
        self.channel.connection_state = "open"
        self.Outbox._enqueue(self.channel, "5561999990001", "oi")
        dead = self.Outbox._enqueue(self.channel, "5561999990002", "falhou")
        dead.status = "dead"
        self.Inbox._enqueue(self.channel, "MID-OBS-1", "5561999990001", "olá", {"k": 1})
        self.channel.invalidate_recordset()
        channel = self.channel
        self.assertEqual((channel.pending_outbox_count, channel.dlq_outbox_count), (1, 1))
        self.assertEqual(channel.pending_inbox_count, 1)
        self.assertTrue(channel.last_inbound_at)
        self.assertEqual(channel.health_state, "warning")
        self.assertIn("DLQ", channel.health_notes)

    # ---------- métricas ----------
    def test_daily_metrics(self):
        tz = pytz.timezone("America/Sao_Paulo")
        day = datetime.now(tz).date()
        start, _stop = day_bounds(day, "America/Sao_Paulo")
        base = start + timedelta(hours=1)
        number = "5561999990010"
        conversation = self.env["dz23.conversation"]._for_number(self.channel, number)

        def inbox(mid, minutes):
            rec, _created = self.Inbox._enqueue(self.channel, mid, number, "oi", {"m": mid})
            rec.write(
                {
                    "conversation_id": conversation.id,
                    "received_at": base + timedelta(minutes=minutes),
                }
            )

        def outbox(body, **dates):
            rec = self.Outbox._enqueue(self.channel, number, body)
            vals = {"status": "sent", "conversation_id": conversation.id}
            vals.update({k: base + timedelta(minutes=v) for k, v in dates.items()})
            rec.write(vals)
            return rec

        inbox("MID-M1", 0)
        inbox("MID-M2", 5)  # ainda sem resposta: não conta como nova 1ª resposta
        outbox("r1", sent_at=10, delivered_at=11, read_at=12)
        outbox("r2", sent_at=20, delivered_at=21)
        outbox("r3", sent_at=30)
        outbox("r4", failed_at=40)
        outbox("r5").status = "dead"
        event, _created = self.Event._record(
            self.channel,
            {
                "provider": "evolution",
                "provider_message_id": "MID-LATENCIA",
                "direction": "outbound",
                "status": "delivered",
                "provider_status": "DELIVERY_ACK",
                "occurred_at": base,
                "payload": {},
            },
        )
        # Eventos são append-only no ORM: o atraso do provedor é fixado via SQL.
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE dz23_message_event SET received_at = %s WHERE id = %s",
            (base + timedelta(seconds=4), event.id),
        )

        metrics = self.Metrics._refresh_channel_day(self.channel, day, today=day)
        self.assertEqual((metrics.received, metrics.sent), (2, 3))
        self.assertEqual((metrics.delivered, metrics.read_count, metrics.failed), (2, 1, 1))
        self.assertEqual((metrics.delivery_rate, metrics.read_rate), (66.67, 33.33))
        self.assertEqual(metrics.dlq, 1)
        self.assertAlmostEqual(metrics.first_response_avg_min, 10.0, places=2)
        self.assertAlmostEqual(metrics.webhook_latency_avg_s, 4.0, places=2)
        self.assertGreaterEqual(metrics.queue_age_max_min, 0.0)
        again = self.Metrics._refresh_channel_day(self.channel, day, today=day)
        self.assertEqual(again, metrics, "recálculo atualiza o mesmo registro")
        self.assertEqual(
            self.Metrics.search_count([("channel_id", "=", self.channel.id), ("day", "=", day)]), 1
        )

    def test_day_bounds_and_ratio(self):
        start, stop = day_bounds(date(2026, 9, 13), "America/Sao_Paulo")
        self.assertEqual(start, datetime(2026, 9, 13, 3, 0))
        self.assertEqual(stop, datetime(2026, 9, 14, 3, 0))
        self.assertEqual(day_bounds(date(2026, 9, 13), "Fuso/Invalido")[0], start)
        self.assertEqual((ratio(1, 3), ratio(1, 0)), (33.33, 0.0))

    def test_metrics_isolated_by_company(self):
        other_company = self.env["res.company"].create({"name": "Outra Empresa QA9"})
        other_channel = _channel(self.env, "Canal Outra QA9", other_company)
        today = fields.Date.today()
        mine = self.Metrics._refresh_channel_day(self.channel, today)
        theirs = self.Metrics._refresh_channel_day(other_channel, today)
        supervisor = new_test_user(
            self.env,
            login="supervisor_obs_qa9",
            groups="dz23_whatsapp.group_dz23_supervisor",
            company_id=self.env.company.id,
            company_ids=[(6, 0, [self.env.company.id])],
        )
        visible = self.Metrics.with_user(supervisor).search([])
        self.assertIn(mine, visible)
        self.assertNotIn(theirs, visible)

    def test_cron_refresh_covers_today_and_yesterday(self):
        self.assertGreaterEqual(self.Metrics._cron_refresh(), 2)
        self.assertEqual(self.Metrics.search_count([("channel_id", "=", self.channel.id)]), 2)

    # ---------- logs ----------
    def test_structured_log_has_no_pii(self):
        logger = logging.getLogger("dz23.test.observability")
        with self.assertLogs(logger, "INFO") as captured:
            log_event(logger, "teste", phone="5561999998888", email="a@b.com", events=3)
        line = captured.output[0]
        self.assertIn("dz23_event=teste", line)
        self.assertIn("events=3", line)
        self.assertNotIn("5561999998888", line)
        self.assertNotIn("a@b.com", line)
