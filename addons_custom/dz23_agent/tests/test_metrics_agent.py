# Métricas de negócio do atendimento (Fase 9, ADR-012), sem rede: transferência para
# humano, orçamento e pedido de origem WhatsApp e respostas de IA contam no dia.
import uuid
from datetime import datetime

import pytz
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install", "dz23")
class TestMetricsAgent(TransactionCase):
    def setUp(self):
        super().setUp()
        self.env["ir.config_parameter"].sudo().set_param("dz23.ai_provider", "ollama")
        self.env.company.dz23_ai_provider = False
        self.channel = self.env["dz23.channel"].create(
            {
                "name": "Canal Métricas Agente",
                "company_id": self.env.company.id,
                "provider": "evolution",
                "evo_base": "http://metricas.local:8080",
                "evo_instance": "metricas_%s" % uuid.uuid4().hex[:8],
                "evo_apikey": "K",
                "agent_autoreply": True,
            }
        )
        self.env["product.template"].create(
            {
                "name": "Servico Metricas QA9",
                "type": "service",
                "list_price": 90.0,
                "sale_ok": True,
                "taxes_id": [(6, 0, [])],
            }
        )
        self.patch(type(self.env["dz23.ai"]), "chat", lambda *_a, **_k: "Olá! 😊")

    def test_business_metrics_for_the_day(self):
        self.channel.handle_inbound("5561900000901", "Quero fazer uma reclamação")
        self.channel.handle_inbound("5561900000902", "quero comprar Servico Metricas QA9")
        self.channel.handle_inbound("5561900000902", "sim")
        order = self.env["sale.order"].search([("dz23_channel_id", "=", self.channel.id)])
        self.assertEqual(len(order), 1)
        order.action_confirm()
        self.channel.handle_inbound("5561900000903", "oi")
        self.env["dz23.ai.request"]._cron_process()

        day = datetime.now(pytz.timezone("America/Sao_Paulo")).date()
        metrics = self.env["dz23.metrics.daily"]._refresh_channel_day(self.channel, day, today=day)
        self.assertEqual(metrics.escalated, 1)
        self.assertEqual((metrics.quotes, metrics.orders), (1, 1))
        self.assertEqual((metrics.ai_requests, metrics.ai_fallbacks), (1, 0))
        self.assertEqual(metrics.payments_confirmed, 0)
