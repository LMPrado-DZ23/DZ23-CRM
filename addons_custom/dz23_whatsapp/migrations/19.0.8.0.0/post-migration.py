# 19.0.8.0.0 — ciclo de vida de mensagem (ADR-006) e claim/lease (ADR-003).
# Migração NÃO destrutiva: só preenche colunas novas em registros antigos.
#  - outbox 'sent'  -> current_status='sent', sent_at/last_status_at do histórico;
#  - outbox 'dead'  -> current_status='failed', failed_at;
#  - correlation_id único por linha (o default do ORM aplicaria o MESMO valor a
#    todas as linhas existentes ao criar a coluna);
#  - inbox: payload_hash/preview a partir do payload já gravado.
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    cr.execute(
        """
        UPDATE dz23_message_outbox
           SET current_status = 'sent',
               sent_at = COALESCE(sent_at, write_date, create_date),
               last_status_at = COALESCE(last_status_at, write_date, create_date)
         WHERE status = 'sent'
           AND (current_status IS NULL OR current_status = 'queued')
        """
    )
    sent = cr.rowcount
    cr.execute(
        """
        UPDATE dz23_message_outbox
           SET current_status = 'failed',
               failed_at = COALESCE(failed_at, write_date, create_date),
               last_status_at = COALESCE(last_status_at, write_date, create_date)
         WHERE status = 'dead'
           AND (current_status IS NULL OR current_status = 'queued')
        """
    )
    dead = cr.rowcount
    cr.execute(
        """
        UPDATE dz23_message_outbox
           SET correlation_id = md5(random()::text || id::text || clock_timestamp()::text)
        """
    )
    cr.execute(
        """
        UPDATE dz23_message_inbox
           SET correlation_id = md5(random()::text || id::text || clock_timestamp()::text)
        """
    )
    cr.execute(
        """
        UPDATE dz23_message_inbox
           SET payload_hash = encode(sha256(convert_to(COALESCE(payload, ''), 'UTF8')), 'hex'),
               payload_preview = left(COALESCE(payload, ''), 500),
               received_at = COALESCE(received_at, create_date)
         WHERE payload_hash IS NULL
        """
    )
    _logger.info(
        "dz23_whatsapp 19.0.8.0.0: outbox sent=%s dead=%s migrados; inbox com hash.", sent, dead
    )
