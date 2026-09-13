# Fluxos ponta a ponta do agente (Fase 3, ADR-004/005), sem rede e sem IA:
# compra repetida reutiliza orçamento, confirmação idempotente, preço da lista de
# preços, variações, expediente/feriado/fuso/intervalo, agenda manual do
# responsável, cancelar/remarcar, retry idempotente e reserva concorrente real.
import datetime as dt
import uuid
from datetime import timedelta

import psycopg2
from odoo import SUPERUSER_ID, api, fields
from odoo.addons.dz23_agent.tests.test_agent import slot_text
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged
from odoo.tools import mute_logger


def _ai_offline(*_args, **_kwargs):
    raise UserError("IA desligada no teste")


@tagged("post_install", "-at_install", "dz23")
class TestAgentFlows(TransactionCase):
    def setUp(self):
        super().setUp()
        self.env.company.partner_id.tz = "America/Sao_Paulo"
        self.patch(type(self.env["dz23.ai"]), "chat", _ai_offline)
        self.channel = self.env["dz23.channel"].create(
            {
                "name": "Canal Fluxos",
                "company_id": self.env.company.id,
                "provider": "evolution",
                "evo_base": "http://x.local:8080",
                "evo_instance": "fluxos_%s" % uuid.uuid4().hex[:8],
                "evo_apikey": "K",
            }
        )
        self.product = self.env["product.template"].create(
            {
                "name": "Servico Beta QA2",
                "type": "service",
                "list_price": 50.0,
                "sale_ok": True,
                "taxes_id": [(6, 0, [])],
            }
        )
        self.number = "5561900000077"
        self.Outbox = self.env["dz23.message.outbox"]

    # ----- helpers -----
    def _say(self, text, correlation_id=None):
        self.channel.handle_inbound(
            self.number,
            text,
            message={"message_type": "text", "correlation_id": correlation_id},
        )
        last = self.Outbox.search([("channel_id", "=", self.channel.id)], order="id desc", limit=1)
        return last.body or ""

    def _orders(self):
        return self.env["sale.order"].search([("dz23_channel_id", "=", self.channel.id)])

    def _contact(self):
        return self.channel._agent_contact(self.number)

    def _lead(self):
        return self._contact().lead_id

    # ----- compra -----
    def test_repeated_purchase_reuses_open_quote(self):
        self.assertIn("Resumo", self._say("quero comprar Servico Beta QA2"))
        self.assertFalse(self._orders())
        self.assertIn("Orçamento", self._say("sim"))
        self.assertEqual(len(self._orders()), 1)
        # mesma intenção de novo: não cria segundo orçamento
        self._say("quero comprar Servico Beta QA2")
        self._say("sim")
        self.assertEqual(len(self._orders()), 1)
        # nova quantidade ajusta o orçamento aberto
        self.assertIn("3 x", self._say("quero comprar 3 Servico Beta QA2"))
        self._say("pode fechar")
        orders = self._orders()
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders.order_line.product_uom_qty, 3)
        self.assertEqual(orders.origin, "WhatsApp DZ23")
        self.assertEqual(orders.dz23_channel_id, self.channel)

    def test_confirmation_is_idempotent_for_same_token(self):
        self._say("quero comprar Servico Beta QA2")
        contact = self._contact()
        ref = contact.agent_pending_ref
        pending = {
            "agent_pending_action": "buy",
            "agent_pending_product_id": contact.agent_pending_product_id.id,
            "agent_pending_qty": 1.0,
            "agent_pending_ref": ref,
            "agent_pending_expires_at": contact.agent_pending_expires_at,
        }
        self.channel._handle_pending(contact, self._lead(), "sim", "CORR-Q1")
        contact.write(pending)  # simula a mesma confirmação reentregue
        self.channel._handle_pending(contact, self._lead(), "sim", "CORR-Q1")
        self.assertEqual(len(self._orders()), 1)
        self.assertEqual(
            self.env["dz23.business.action"].search_count(
                [
                    ("idempotency_key", "like", "quote:%s:%%" % self.channel.id),
                    ("state", "=", "done"),
                ]
            ),
            1,
        )

    def test_negative_answer_cancels_pending(self):
        self._say("quero comprar Servico Beta QA2")
        self.assertIn("não gerei", self._say("não"))
        self.assertFalse(self._contact().agent_pending_action)
        self.assertFalse(self._orders())

    def test_expired_pending_is_not_confirmed(self):
        self._say("quero comprar Servico Beta QA2")
        self._contact().write(
            {"agent_pending_expires_at": fields.Datetime.now() - timedelta(minutes=1)}
        )
        self._say("sim")
        self.assertFalse(self._orders(), "confirmação vencida não gera pedido")

    def test_price_and_total_come_from_pricelist(self):
        lead = self._lead()
        partner = self.channel._agent_partner_for(lead)
        pricelist = self.env["product.pricelist"].create(
            {
                "name": "Tabela QA2",
                "currency_id": self.env.company.currency_id.id,
                "company_id": self.env.company.id,
                "item_ids": [
                    (
                        0,
                        0,
                        {
                            "applied_on": "1_product",
                            "product_tmpl_id": self.product.id,
                            "compute_price": "fixed",
                            "fixed_price": 45.0,
                        },
                    )
                ],
            }
        )
        partner.property_product_pricelist = pricelist
        reply = self._say("quero comprar 2 Servico Beta QA2")
        self.assertIn("90.00", reply, "total calculado pela lista de preços (2 x 45)")
        self._say("sim")
        self.assertAlmostEqual(self._orders().amount_untaxed, 90.0)

    def test_price_question_never_creates_order(self):
        self.assertIn("50.00", self._say("quanto custa o Servico Beta QA2?"))
        self.assertFalse(self._orders())
        self.assertFalse(self._contact().agent_pending_action)

    def test_variant_must_be_chosen(self):
        attribute = self.env["product.attribute"].create(
            {
                "name": "Tamanho QA3",
                "create_variant": "always",
                "value_ids": [(0, 0, {"name": "P"}), (0, 0, {"name": "G"})],
            }
        )
        shirt = self.env["product.template"].create(
            {
                "name": "Camiseta QA3",
                "list_price": 70.0,
                "sale_ok": True,
                "taxes_id": [(6, 0, [])],
                "attribute_line_ids": [
                    (
                        0,
                        0,
                        {
                            "attribute_id": attribute.id,
                            "value_ids": [(6, 0, attribute.value_ids.ids)],
                        },
                    )
                ],
            }
        )
        self.assertEqual(len(shirt.product_variant_ids), 2)
        self.assertIn("opções", self._say("quero comprar Camiseta QA3"))
        self.assertFalse(self._contact().agent_pending_action)
        reply = self._say("quero comprar Camiseta QA3 G")
        self.assertIn("Resumo", reply)
        self.assertIn("(G)", reply)

    # ----- agenda -----
    def _events(self, lead=None, active=True):
        domain = [("opportunity_id", "=", (lead or self._lead()).id)]
        if active is None:
            return self.env["calendar.event"].with_context(active_test=False).search(domain)
        return self.env["calendar.event"].search(domain + [("active", "=", active)])

    def test_outside_business_hours_and_weekend_rejected(self):
        night, _d = slot_text(days=400, weekday=1, hour=22, minute=0)
        self.assertIn("não estamos atendendo", self._say(night))
        saturday, _d = slot_text(days=400, weekday=5, hour=10, minute=0)
        self.assertIn("não estamos atendendo", self._say(saturday))
        self.assertFalse(self._events())

    def test_holiday_rejected(self):
        text, day = slot_text(days=460, weekday=2, hour=10, minute=0)
        calendar = self.env.company.resource_calendar_id
        self.env["resource.calendar.leaves"].create(
            {
                "name": "Feriado QA",
                "calendar_id": calendar.id,
                "resource_id": False,
                "date_from": dt.datetime.combine(day, dt.time(0, 0)),
                "date_to": dt.datetime.combine(day + timedelta(days=1), dt.time(6, 0)),
            }
        )
        self.assertIn("não estamos atendendo", self._say(text))
        self.assertFalse(self._events())

    def test_timezone_is_stored_in_utc(self):
        text, _day = slot_text(days=470, weekday=1, hour=14, minute=30)
        self.assertIn("Prontinho", self._say(text))
        event = self._events()
        # America/Sao_Paulo é UTC-3 (sem horário de verão)
        self.assertEqual((event.start.hour, event.start.minute), (17, 30))

    def test_manual_event_of_responsible_blocks_slot(self):
        self.channel.agenda_user_id = self.env.user
        text, day = slot_text(days=480, weekday=3, hour=10, minute=0)
        start_utc, _aware = self.channel._agent_local_to_utc(day, 10, 0)
        self.env["calendar.event"].create(
            {
                "name": "Compromisso manual QA",
                "start": start_utc,
                "stop": start_utc + timedelta(minutes=30),
                "user_id": self.env.user.id,
                "partner_ids": [(6, 0, [self.env.user.partner_id.id])],
            }
        )
        self.assertIn("reservado", self._say(text))
        self.assertFalse(self._events())

    def test_buffer_between_appointments(self):
        self.channel.agenda_buffer_minutes = 30
        first, _d = slot_text(days=490, weekday=1, hour=9, minute=0)
        self.assertIn("Prontinho", self._say(first))
        other = self.channel._agent_find_lead("5561900000088")
        too_close, _d = slot_text(days=490, weekday=1, hour=10, minute=15)
        self.assertIn("reservado", self.channel._handle_schedule(other, too_close))
        ok, _d = slot_text(days=490, weekday=1, hour=10, minute=30)
        self.assertIn("Prontinho", self.channel._handle_schedule(other, ok))

    def test_cancel_and_reschedule(self):
        text, _d = slot_text(days=500, weekday=1, hour=9, minute=0)
        self.assertIn("Prontinho", self._say(text))
        self.assertIn("Cancelei", self._say("quero cancelar meu agendamento"))
        self.assertFalse(self._events())
        self.assertEqual(len(self._events(active=False)), 1, "cancelado é arquivado, não apagado")
        self.assertIn("Prontinho", self._say(text))
        new_text, new_day = slot_text(
            days=500, weekday=2, hour=11, minute=0, prefix="quero remarcar para"
        )
        self.assertIn("Remarcado", self._say(new_text))
        active = self._events()
        self.assertEqual(len(active), 1)
        self.assertEqual(active.start.date(), new_day)

    def test_retry_with_same_correlation_creates_single_event(self):
        text, _d = slot_text(days=510, weekday=1, hour=15, minute=0)
        lead = self._lead()
        r1 = self.channel._handle_schedule(lead, text, correlation_id="CORR-S1")
        r2 = self.channel._handle_schedule(lead, text, correlation_id="CORR-S1")
        self.assertIn("Prontinho", r1)
        self.assertIn("Prontinho", r2, "retry do mesmo item devolve a mesma reserva")
        self.assertEqual(len(self._events()), 1)

    def test_concurrent_booking_is_serialized(self):
        text, _d = slot_text(days=520, weekday=1, hour=11, minute=0)
        with self.registry.cursor() as cr0:
            env0 = api.Environment(cr0, SUPERUSER_ID, {})
            channel = env0["dz23.channel"].create(
                {
                    "name": "Canal Concorrência Agenda",
                    "provider": "evolution",
                    "evo_base": "http://x.local:8080",
                    "evo_instance": "agenda_conc_%s" % uuid.uuid4().hex[:8],
                    "evo_apikey": "K",
                }
            )
            lead_a = channel._agent_find_lead("5561900001001")
            lead_b = channel._agent_find_lead("5561900001002")
            ids = {"channel": channel.id, "a": lead_a.id, "b": lead_b.id}
            env0.flush_all()
            cr0.commit()
        cr_a = self.registry.cursor()
        cr_b = self.registry.cursor()
        try:
            env_a = api.Environment(cr_a, SUPERUSER_ID, {})
            ch_a = env_a["dz23.channel"].browse(ids["channel"])
            self.assertIn(
                "Prontinho", ch_a._handle_schedule(env_a["crm.lead"].browse(ids["a"]), text)
            )
            # B tenta reservar enquanto A segura o lock (sem commit): espera.
            cr_b.execute("SET LOCAL lock_timeout = '300ms'")
            env_b = api.Environment(cr_b, SUPERUSER_ID, {})
            with (
                mute_logger("odoo.sql_db"),
                self.assertRaises(psycopg2.errors.LockNotAvailable),
            ):
                env_b["dz23.channel"].browse(ids["channel"])._agent_lock(
                    "agenda", "canal-%s" % ids["channel"]
                )
            cr_b.rollback()
            cr_a.commit()
            env_b = api.Environment(cr_b, SUPERUSER_ID, {})
            reply_b = (
                env_b["dz23.channel"]
                .browse(ids["channel"])
                ._handle_schedule(env_b["crm.lead"].browse(ids["b"]), text)
            )
            self.assertIn("reservado", reply_b, "depois do commit de A, B vê o conflito")
            cr_b.rollback()
        finally:
            cr_a.rollback()
            cr_b.rollback()
            cr_a.close()
            cr_b.close()
            with self.registry.cursor() as cr_clean:
                env_c = api.Environment(cr_clean, SUPERUSER_ID, {})
                leads = env_c["crm.lead"].browse([ids["a"], ids["b"]]).exists()
                env_c["calendar.event"].with_context(active_test=False).search(
                    [("opportunity_id", "in", leads.ids)]
                ).unlink()
                partners = leads.mapped("partner_id")
                leads.unlink()
                env_c["dz23.channel"].browse(ids["channel"]).exists().unlink()
                partners.unlink()
                cr_clean.commit()
