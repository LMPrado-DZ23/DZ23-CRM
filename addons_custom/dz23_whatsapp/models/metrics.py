# Métricas diárias por empresa/canal/provedor (Fase 9, ADR-012). Um registro por
# canal e dia (no fuso da empresa), recalculado por cron a partir das filas — sem
# texto de mensagem. Taxas de entrega/leitura são por COORTE: das mensagens enviadas
# no dia, quantas foram entregues/lidas até o momento do cálculo.
import logging
from datetime import datetime, time, timedelta

import pytz
from odoo import api, fields, models
from odoo.exceptions import AccessError
from odoo.tools import SQL
from odoo.tools.translate import _

from .queue_utils import log_event

_logger = logging.getLogger(__name__)
_DEFAULT_TZ = "America/Sao_Paulo"
_OUTBOX_PENDING = ("pending", "sending", "failed")
_INBOX_PENDING = ("pending", "processing", "failed")


def day_bounds(day, tz_name):
    """(início, fim) do dia local em UTC ingênuo (formato do banco)."""
    try:
        tz = pytz.timezone(tz_name or _DEFAULT_TZ)
    except pytz.UnknownTimeZoneError:
        tz = pytz.timezone(_DEFAULT_TZ)
    start = tz.localize(datetime.combine(day, time.min)).astimezone(pytz.utc)
    stop = tz.localize(datetime.combine(day + timedelta(days=1), time.min)).astimezone(pytz.utc)
    return start.replace(tzinfo=None), stop.replace(tzinfo=None)


def ratio(part, total):
    return round(100.0 * part / total, 2) if total else 0.0


class DZ23MetricsDaily(models.Model):
    _name = "dz23.metrics.daily"
    _description = "DZ23 — Métricas diárias por canal"
    _order = "day desc, company_id, channel_id"

    day = fields.Date("Dia", required=True, index=True, readonly=True)
    channel_id = fields.Many2one(
        "dz23.channel", "Canal", required=True, ondelete="cascade", index=True, readonly=True
    )
    company_id = fields.Many2one(related="channel_id.company_id", store=True, index=True)
    provider = fields.Selection(related="channel_id.provider", store=True, string="Provedor")
    received = fields.Integer("Recebidas", readonly=True)
    sent = fields.Integer("Enviadas", readonly=True)
    delivered = fields.Integer("Entregues", readonly=True)
    read_count = fields.Integer("Lidas", readonly=True)
    failed = fields.Integer("Falhas do provedor", readonly=True)
    dlq = fields.Integer("DLQ", readonly=True)
    delivery_rate = fields.Float("Taxa de entrega (%)", readonly=True, aggregator="avg")
    read_rate = fields.Float("Taxa de leitura (%)", readonly=True, aggregator="avg")
    webhook_latency_avg_s = fields.Float(
        "Latência do webhook (s)", readonly=True, aggregator="avg", digits=(10, 2)
    )
    first_response_avg_min = fields.Float(
        "1ª resposta média (min)", readonly=True, aggregator="avg", digits=(10, 2)
    )
    queue_age_max_min = fields.Float(
        "Idade máx. da fila (min)", readonly=True, aggregator="max", digits=(10, 1)
    )
    computed_at = fields.Datetime("Calculado em", readonly=True)

    _channel_day_uniq = models.Constraint(
        "unique(channel_id, day)", "Já existe métrica para este canal neste dia."
    )

    # ---------- cálculo ----------
    def _scalar(self, query):
        self.env.cr.execute(query)
        row = self.env.cr.fetchone()
        return row[0] if row else None

    def _messaging_values(self, channel, start, stop, is_today):
        cr = self.env.cr
        received = self._scalar(
            SQL(
                "SELECT count(*) FROM dz23_message_inbox"
                " WHERE channel_id = %s AND received_at >= %s AND received_at < %s",
                channel.id,
                start,
                stop,
            )
        )
        cr.execute(
            SQL(
                """
                SELECT count(*) FILTER (WHERE sent_at >= %(start)s AND sent_at < %(stop)s),
                       count(*) FILTER (WHERE sent_at >= %(start)s AND sent_at < %(stop)s
                                          AND (delivered_at IS NOT NULL OR read_at IS NOT NULL)),
                       count(*) FILTER (WHERE sent_at >= %(start)s AND sent_at < %(stop)s
                                          AND read_at IS NOT NULL),
                       count(*) FILTER (WHERE failed_at >= %(start)s AND failed_at < %(stop)s)
                  FROM dz23_message_outbox
                 WHERE channel_id = %(channel)s
                   AND (sent_at >= %(start)s OR failed_at >= %(start)s)
                """,
                channel=channel.id,
                start=start,
                stop=stop,
            )
        )
        sent, delivered, read_count, failed = cr.fetchone()
        dlq = self._scalar(
            SQL(
                """
                SELECT (SELECT count(*) FROM dz23_message_inbox
                         WHERE channel_id = %(channel)s AND status = 'dead'
                           AND create_date >= %(start)s AND create_date < %(stop)s)
                     + (SELECT count(*) FROM dz23_message_outbox
                         WHERE channel_id = %(channel)s AND status = 'dead'
                           AND create_date >= %(start)s AND create_date < %(stop)s)
                     + (SELECT count(*) FROM dz23_message_media
                         WHERE channel_id = %(channel)s AND status = 'dead'
                           AND create_date >= %(start)s AND create_date < %(stop)s)
                """,
                channel=channel.id,
                start=start,
                stop=stop,
            )
        )
        webhook_latency = self._scalar(
            SQL(
                """
                SELECT avg(extract(epoch FROM (received_at - occurred_at)))
                  FROM dz23_message_event
                 WHERE channel_id = %s AND received_at >= %s AND received_at < %s
                   AND occurred_at IS NOT NULL AND received_at >= occurred_at
                """,
                channel.id,
                start,
                stop,
            )
        )
        # 1ª resposta: da primeira mensagem do cliente ainda sem resposta até o
        # próximo envio aceito na mesma conversa.
        first_response = self._scalar(
            SQL(
                """
                SELECT avg(extract(epoch FROM (resp.sent_at - i.received_at))) / 60
                  FROM dz23_message_inbox i
                  JOIN LATERAL (
                        SELECT min(o.sent_at) AS sent_at FROM dz23_message_outbox o
                         WHERE o.conversation_id = i.conversation_id
                           AND o.sent_at >= i.received_at) resp ON resp.sent_at IS NOT NULL
                 WHERE i.channel_id = %(channel)s
                   AND i.conversation_id IS NOT NULL
                   AND i.received_at >= %(start)s AND i.received_at < %(stop)s
                   AND NOT EXISTS (
                        SELECT 1 FROM dz23_message_inbox j
                         WHERE j.conversation_id = i.conversation_id
                           AND j.received_at < i.received_at
                           AND j.received_at > COALESCE(
                               (SELECT max(o2.sent_at) FROM dz23_message_outbox o2
                                 WHERE o2.conversation_id = i.conversation_id
                                   AND o2.sent_at < i.received_at),
                               '-infinity'::timestamp))
                """,
                channel=channel.id,
                start=start,
                stop=stop,
            )
        )
        queue_age = 0.0
        if is_today:
            queue_age = self._scalar(
                SQL(
                    """
                    SELECT extract(epoch FROM (now() AT TIME ZONE 'utc') - min(ts)) / 60
                      FROM (SELECT min(received_at) AS ts FROM dz23_message_inbox
                             WHERE channel_id = %(channel)s AND status = ANY(%(inbox)s)
                            UNION ALL
                            SELECT min(create_date) FROM dz23_message_outbox
                             WHERE channel_id = %(channel)s AND status = ANY(%(outbox)s)) q
                    """,
                    channel=channel.id,
                    inbox=list(_INBOX_PENDING),
                    outbox=list(_OUTBOX_PENDING),
                )
            )
        return {
            "received": received or 0,
            "sent": sent or 0,
            "delivered": delivered or 0,
            "read_count": read_count or 0,
            "failed": failed or 0,
            "dlq": dlq or 0,
            "delivery_rate": ratio(delivered or 0, sent or 0),
            "read_rate": ratio(read_count or 0, sent or 0),
            "webhook_latency_avg_s": float(webhook_latency or 0.0),
            "first_response_avg_min": float(first_response or 0.0),
            "queue_age_max_min": max(0.0, float(queue_age or 0.0)),
        }

    def _extra_values(self, channel, start, stop):
        """Gancho para módulos que medem efeitos de negócio (dz23_agent)."""
        return {}

    @api.model
    def _refresh_channel_day(self, channel, day, today=None):
        tz_name = channel.company_id.partner_id.tz or _DEFAULT_TZ
        start, stop = day_bounds(day, tz_name)
        for model in ("dz23.message.inbox", "dz23.message.outbox", "dz23.message.event"):
            self.env[model].flush_model()
        values = self._messaging_values(channel, start, stop, is_today=(day == today))
        values.update(self._extra_values(channel, start, stop))
        values["computed_at"] = fields.Datetime.now()
        Metrics = self.sudo()
        record = Metrics.search([("channel_id", "=", channel.id), ("day", "=", day)], limit=1)
        if record:
            record.write(values)
        else:
            record = Metrics.create({"channel_id": channel.id, "day": day, **values})
        return record

    @api.model
    def _cron_refresh(self, days=2, companies=None):
        """Recalcula hoje e ontem (no fuso de cada empresa): idempotente."""
        domain = [("company_id", "in", companies.ids)] if companies else []
        channels = self.env["dz23.channel"].sudo().with_context(active_test=False).search(domain)
        count = 0
        for channel in channels:
            tz_name = channel.company_id.partner_id.tz or _DEFAULT_TZ
            try:
                today = datetime.now(pytz.timezone(tz_name)).date()
            except pytz.UnknownTimeZoneError:
                today = datetime.now(pytz.timezone(_DEFAULT_TZ)).date()
            for offset in range(days):
                self._refresh_channel_day(channel, today - timedelta(days=offset), today=today)
                count += 1
        log_event(_logger, "metrics_refreshed", channels=len(channels), rows=count)
        return count

    def action_refresh_metrics(self):
        """Botão "Atualizar agora" (cabeçalho da lista, sem @api.model: o cliente envia os
        ids). Só supervisor/administrador e só as empresas ativas do usuário."""
        user = self.env.user
        if not (
            user.has_group("dz23_whatsapp.group_dz23_supervisor")
            or user.has_group("base.group_system")
        ):
            raise AccessError(_("Somente supervisores de atendimento atualizam as métricas."))
        self._cron_refresh(companies=self.env.companies)
        return {"type": "ir.actions.client", "tag": "reload"}
