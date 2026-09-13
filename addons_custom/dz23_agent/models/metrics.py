# Métricas de negócio do atendimento (Fase 9, ADR-012): conversas transferidas para
# humano, orçamentos e pedidos de origem WhatsApp, pagamentos confirmados desses
# pedidos e desempenho da IA — por canal e dia.
from odoo import fields, models
from odoo.addons.dz23_whatsapp.models.metrics import ratio
from odoo.tools import SQL


class DZ23MetricsDailyAgent(models.Model):
    _inherit = "dz23.metrics.daily"

    escalated = fields.Integer("Transferidas para humano", readonly=True)
    quotes = fields.Integer("Orçamentos", readonly=True)
    orders = fields.Integer("Pedidos confirmados", readonly=True)
    payments_confirmed = fields.Integer("Pagamentos confirmados", readonly=True)
    quote_conversion_rate = fields.Float(
        "Conversão em orçamento (%)", readonly=True, aggregator="avg"
    )
    order_conversion_rate = fields.Float("Conversão em pedido (%)", readonly=True, aggregator="avg")
    ai_requests = fields.Integer("Respostas de IA", readonly=True)
    ai_fallbacks = fields.Integer("IA sem resposta (transferida)", readonly=True)
    ai_guard_triggered = fields.Integer("Guarda da IA acionada", readonly=True)
    ai_latency_avg_ms = fields.Float(
        "Latência média da IA (ms)", readonly=True, aggregator="avg", digits=(10, 0)
    )

    def _extra_values(self, channel, start, stop):
        values = super()._extra_values(channel, start, stop)

        def between(field):
            return [(field, ">=", start), (field, "<", stop)]

        Order = self.env["sale.order"].sudo()
        Request = self.env["dz23.ai.request"].sudo()
        by_channel = [("dz23_channel_id", "=", channel.id)]
        quotes = Order.search_count(by_channel + between("create_date"))
        orders = Order.search_count(by_channel + [("state", "=", "sale")] + between("date_order"))
        request_domain = [("channel_id", "=", channel.id)] + between("processed_at")
        [(latency,)] = Request._read_group(
            request_domain + [("used_fallback", "=", False)], aggregates=["duration_ms:avg"]
        )
        conversations = self._conversations_with_inbound(channel, start, stop)
        values.update(
            escalated=self.env["dz23.conversation"]
            .sudo()
            .search_count([("channel_id", "=", channel.id)] + between("handoff_at")),
            quotes=quotes,
            orders=orders,
            payments_confirmed=self.env["payment.transaction"]
            .sudo()
            .search_count(
                [("sale_order_ids.dz23_channel_id", "=", channel.id), ("state", "=", "done")]
                + between("last_state_change")
            ),
            quote_conversion_rate=ratio(quotes, conversations),
            order_conversion_rate=ratio(orders, conversations),
            ai_requests=Request.search_count(request_domain),
            ai_fallbacks=Request.search_count(request_domain + [("used_fallback", "=", True)]),
            ai_guard_triggered=Request.search_count(
                request_domain + [("guard_triggered", "=", True)]
            ),
            ai_latency_avg_ms=float(latency or 0.0),
        )
        return values

    def _conversations_with_inbound(self, channel, start, stop):
        """Conversas com mensagem do cliente no dia (base das taxas de conversão)."""
        self.env.cr.execute(
            SQL(
                """
                SELECT count(DISTINCT conversation_id) FROM dz23_message_inbox
                 WHERE channel_id = %s AND conversation_id IS NOT NULL
                   AND received_at >= %s AND received_at < %s
                """,
                channel.id,
                start,
                stop,
            )
        )
        return self.env.cr.fetchone()[0] or 0
