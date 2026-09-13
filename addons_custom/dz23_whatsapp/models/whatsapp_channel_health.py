# Saúde operacional do canal (Fase 9, ADR-012): último webhook/recebida/envio/status,
# item mais antigo de cada fila, DLQ, conexão Evolution e pausa por 429. Calculado
# na leitura com SQL agregado (sem gravar nada) e restrito a administradores.
from datetime import timedelta

from odoo import fields, models
from odoo.tools import SQL

HEALTH_STATES = [("ok", "Saudável"), ("warning", "Atenção"), ("critical", "Crítico")]
_RANK = {"ok": 0, "warning": 1, "critical": 2}
_QUEUE_WARNING_MINUTES = 5
_QUEUE_CRITICAL_MINUTES = 30
_STATUS_CALLBACK_GRACE = timedelta(minutes=30)
_EVOLUTION_CONNECTED = ("open", "connected")
_PENDING_INBOX = ("pending", "processing", "failed")
_PENDING_OUTBOX = ("pending", "sending", "failed")


def evaluate_health(snapshot, now):
    """Regra pura (testável): snapshot do canal -> (estado, motivos)."""
    state = "ok"
    notes = []

    def flag(level, note):
        nonlocal state
        notes.append(note)
        if _RANK[level] > _RANK[state]:
            state = level

    for label, oldest in (
        ("entrada", snapshot.get("oldest_inbox_pending_at")),
        ("saída", snapshot.get("oldest_outbox_pending_at")),
    ):
        if not oldest:
            continue
        minutes = int((now - oldest).total_seconds() // 60)
        if minutes >= _QUEUE_CRITICAL_MINUTES:
            flag("critical", "Fila de %s parada há %d min." % (label, minutes))
        elif minutes >= _QUEUE_WARNING_MINUTES:
            flag("warning", "Fila de %s atrasada (%d min)." % (label, minutes))
    dlq = sum(
        snapshot.get(key) or 0 for key in ("dlq_inbox_count", "dlq_outbox_count", "dlq_media_count")
    )
    if dlq:
        flag("warning", "%d item(ns) na DLQ aguardando análise." % dlq)
    if snapshot.get("provider") == "evolution":
        connection = (snapshot.get("connection_state") or "").lower()
        if not connection:
            flag("warning", "Estado da conexão Evolution ainda desconhecido.")
        elif connection not in _EVOLUTION_CONNECTED:
            flag("critical", "Evolution desconectado (%s): reconecte pelo QR." % connection)
    paused_until = snapshot.get("rate_limited_until")
    if paused_until and paused_until > now:
        flag("warning", "Envio pausado pelo provedor (429) até %s UTC." % paused_until)
    last_sent = snapshot.get("last_sent_at")
    last_status = snapshot.get("last_status_at")
    if (
        last_sent
        and now - last_sent < timedelta(hours=24)
        and now - last_sent > _STATUS_CALLBACK_GRACE
        and (not last_status or last_status < last_sent)
    ):
        flag(
            "warning",
            "Nenhum status de entrega desde o último envio: confira o webhook de status.",
        )
    return state, notes


class DZ23ChannelHealth(models.Model):
    _inherit = "dz23.channel"

    health_state = fields.Selection(
        HEALTH_STATES, "Saúde", compute="_compute_health", groups="base.group_system"
    )
    health_notes = fields.Text("Motivos", compute="_compute_health", groups="base.group_system")
    last_inbound_at = fields.Datetime(
        "Última mensagem recebida", compute="_compute_health", groups="base.group_system"
    )
    last_sent_at = fields.Datetime(
        "Último envio aceito", compute="_compute_health", groups="base.group_system"
    )
    last_status_at = fields.Datetime(
        "Último status confirmado", compute="_compute_health", groups="base.group_system"
    )
    oldest_inbox_pending_at = fields.Datetime(
        "Entrada pendente mais antiga", compute="_compute_health", groups="base.group_system"
    )
    oldest_outbox_pending_at = fields.Datetime(
        "Saída pendente mais antiga", compute="_compute_health", groups="base.group_system"
    )
    pending_inbox_count = fields.Integer(
        "Entrada pendente", compute="_compute_health", groups="base.group_system"
    )
    pending_outbox_count = fields.Integer(
        "Saída pendente", compute="_compute_health", groups="base.group_system"
    )
    dlq_inbox_count = fields.Integer(
        "DLQ entrada", compute="_compute_health", groups="base.group_system"
    )
    dlq_outbox_count = fields.Integer(
        "DLQ saída", compute="_compute_health", groups="base.group_system"
    )
    dlq_media_count = fields.Integer(
        "Mídias com falha", compute="_compute_health", groups="base.group_system"
    )

    def _health_snapshots(self):
        ids = [channel_id for channel_id in self.ids if isinstance(channel_id, int)]
        if not ids:
            return {}
        for model in (
            "dz23.message.inbox",
            "dz23.message.outbox",
            "dz23.message.event",
            "dz23.message.media",
        ):
            self.env[model].flush_model()
        snapshots = {channel_id: {} for channel_id in ids}
        cr = self.env.cr
        cr.execute(
            SQL(
                """
                SELECT channel_id,
                       max(received_at),
                       min(received_at) FILTER (WHERE status = ANY(%s)),
                       count(*) FILTER (WHERE status = ANY(%s)),
                       count(*) FILTER (WHERE status = 'dead')
                  FROM dz23_message_inbox
                 WHERE channel_id = ANY(%s)
                 GROUP BY channel_id
                """,
                list(_PENDING_INBOX),
                list(_PENDING_INBOX),
                ids,
            )
        )
        for channel_id, last_in, oldest, pending, dead in cr.fetchall():
            snapshots[channel_id].update(
                last_inbound_at=last_in,
                oldest_inbox_pending_at=oldest,
                pending_inbox_count=pending,
                dlq_inbox_count=dead,
            )
        cr.execute(
            SQL(
                """
                SELECT channel_id,
                       max(sent_at),
                       min(create_date) FILTER (WHERE status = ANY(%s)),
                       count(*) FILTER (WHERE status = ANY(%s)),
                       count(*) FILTER (WHERE status = 'dead')
                  FROM dz23_message_outbox
                 WHERE channel_id = ANY(%s)
                 GROUP BY channel_id
                """,
                list(_PENDING_OUTBOX),
                list(_PENDING_OUTBOX),
                ids,
            )
        )
        for channel_id, last_sent, oldest, pending, dead in cr.fetchall():
            snapshots[channel_id].update(
                last_sent_at=last_sent,
                oldest_outbox_pending_at=oldest,
                pending_outbox_count=pending,
                dlq_outbox_count=dead,
            )
        cr.execute(
            SQL(
                """
                SELECT channel_id, max(received_at)
                  FROM dz23_message_event
                 WHERE channel_id = ANY(%s) AND message_direction = 'outbound'
                 GROUP BY channel_id
                """,
                ids,
            )
        )
        for channel_id, last_status in cr.fetchall():
            snapshots[channel_id]["last_status_at"] = last_status
        cr.execute(
            SQL(
                """
                SELECT channel_id, count(*)
                  FROM dz23_message_media
                 WHERE channel_id = ANY(%s) AND status = 'dead'
                 GROUP BY channel_id
                """,
                ids,
            )
        )
        for channel_id, dead in cr.fetchall():
            snapshots[channel_id]["dlq_media_count"] = dead
        return snapshots

    def _compute_health(self):
        snapshots = self._health_snapshots()
        now = fields.Datetime.now()
        for channel in self:
            snapshot = dict(snapshots.get(channel.id) or {})
            snapshot.update(
                provider=channel.provider,
                connection_state=channel.connection_state,
                rate_limited_until=channel.rate_limited_until,
            )
            state, notes = evaluate_health(snapshot, now)
            channel.update(
                {
                    "health_state": state,
                    "health_notes": "\n".join(notes) or False,
                    "last_inbound_at": snapshot.get("last_inbound_at") or False,
                    "last_sent_at": snapshot.get("last_sent_at") or False,
                    "last_status_at": snapshot.get("last_status_at") or False,
                    "oldest_inbox_pending_at": snapshot.get("oldest_inbox_pending_at") or False,
                    "oldest_outbox_pending_at": snapshot.get("oldest_outbox_pending_at") or False,
                    "pending_inbox_count": snapshot.get("pending_inbox_count") or 0,
                    "pending_outbox_count": snapshot.get("pending_outbox_count") or 0,
                    "dlq_inbox_count": snapshot.get("dlq_inbox_count") or 0,
                    "dlq_outbox_count": snapshot.get("dlq_outbox_count") or 0,
                    "dlq_media_count": snapshot.get("dlq_media_count") or 0,
                }
            )
