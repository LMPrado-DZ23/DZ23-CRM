# Utilitários compartilhados das filas duráveis (inbox, outbox e eventos):
# claim com lease (ADR-003), backoff com jitter, serialização/hash de payload e
# saneamento de mensagens de erro (sem PII em banco/log).
import hashlib
import json
import logging
import re
import secrets

from odoo.tools import SQL, config

_JITTER = secrets.SystemRandom()
_PREVIEW_CHARS = 500
_LOG_VALUE_CHARS = 120
_RE_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_RE_LONG_DIGITS = re.compile(r"\d{8,}")
_NOW_UTC = SQL("(clock_timestamp() AT TIME ZONE 'utc')")


def backoff_seconds(attempts):
    """Backoff exponencial (teto 1 h) com jitter de até 50%."""
    base = min(3600, 2 ** max(1, int(attempts or 0)))
    return base + _JITTER.randint(0, max(1, base // 2))


def sanitize_error(message, limit=200):
    """Erro curto, sem e-mail nem sequências longas de dígitos (telefone/documento)."""
    text = "" if message is None or message is False else str(message)
    text = _RE_EMAIL.sub("[email]", text)
    text = _RE_LONG_DIGITS.sub("[num]", text)
    return text[:limit]


def log_event(logger, event, level=logging.INFO, **values):
    """Log estruturado `dz23_event=<nome> chave=valor` (chaves em ordem estável, fácil
    de filtrar/agregar). Valores passam por `sanitize_error`: registre ids, contagens,
    durações e códigos — nunca texto de mensagem, telefone ou payload (ADR-012)."""
    if not logger.isEnabledFor(level):
        return
    parts = ["dz23_event=%s" % event]
    for key in sorted(values):
        value = sanitize_error(values[key], limit=_LOG_VALUE_CHARS)
        parts.append("%s=%s" % (key, value.replace(" ", "_") or "-"))
    logger.log(level, "%s", " ".join(parts))


def payload_json(payload):
    """Serializa o envelope inteiro (nunca corta: um JSON cortado é inválido)."""
    return json.dumps(
        payload if payload is not None else {},
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )


def payload_digest(text):
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def payload_preview(text, limit=_PREVIEW_CHARS):
    return (text or "")[:limit]


def can_commit():
    """Commit por item só fora de testes (o Odoo proíbe commit no cursor de teste)."""
    return not config["test_enable"]


def claim_due(
    cr,
    table,
    from_states,
    to_state,
    lease_seconds,
    limit,
    per_channel_limit=None,
    skip_rate_limited=False,
):
    """Reivindica até `limit` itens vencidos de forma atômica e curta.

    O SELECT de travamento usa FOR UPDATE SKIP LOCKED (dois workers nunca pegam o
    mesmo item); o UPDATE muda o estado, incrementa `attempts` e grava o lease.
    `per_channel_limit` limita itens por canal nesta execução (um canal em rajada
    não monopoliza o worker) e `skip_rate_limited` ignora canais pausados por 429
    (ADR-008). Retorna os ids. O chamador faz commit antes de processar (ADR-003).
    """
    rate_limit_filter = SQL(
        """
        AND NOT EXISTS (
            SELECT 1 FROM dz23_channel c
             WHERE c.id = q.channel_id AND c.rate_limited_until > %(now)s)
        """,
        now=_NOW_UTC,
    )
    cr.execute(
        SQL(
            """
            UPDATE %(table)s AS target
               SET status = %(to_state)s,
                   attempts = COALESCE(target.attempts, 0) + 1,
                   lease_until = %(now)s + %(lease)s * interval '1 second'
             WHERE target.id IN (
                   SELECT id FROM %(table)s
                    WHERE id IN (
                          SELECT cand.id FROM (
                                 SELECT q.id, q.next_attempt_at,
                                        row_number() OVER (
                                            PARTITION BY q.channel_id
                                            ORDER BY q.next_attempt_at, q.id) AS rn
                                   FROM %(table)s q
                                  WHERE q.status = ANY(%(from_states)s)
                                    AND q.next_attempt_at <= %(now)s
                                    %(rate_limit)s
                          ) cand
                           WHERE cand.rn <= %(per_channel)s)
                      -- Reavaliado pelo Postgres DEPOIS de obter o lock da linha: se
                      -- outro worker já reivindicou e commitou, a linha não casa mais.
                      AND status = ANY(%(from_states)s)
                      AND next_attempt_at <= %(now)s
                    ORDER BY next_attempt_at, id
                    LIMIT %(limit)s
                    FOR UPDATE SKIP LOCKED)
               AND target.status = ANY(%(from_states)s)
         RETURNING target.id
            """,
            table=SQL.identifier(table),
            to_state=to_state,
            now=_NOW_UTC,
            lease=int(lease_seconds),
            from_states=list(from_states),
            rate_limit=rate_limit_filter if skip_rate_limited else SQL(""),
            per_channel=int(per_channel_limit or limit),
            limit=int(limit),
        )
    )
    return sorted(row[0] for row in cr.fetchall())


def expired_leases(cr, table, state, limit=200):
    """Itens presos em `state` cujo lease venceu (worker morreu/foi morto)."""
    cr.execute(
        SQL(
            """
            SELECT id FROM %(table)s
             WHERE status = %(state)s AND lease_until < %(now)s
             ORDER BY id
             LIMIT %(limit)s
             FOR UPDATE SKIP LOCKED
            """,
            table=SQL.identifier(table),
            state=state,
            now=_NOW_UTC,
            limit=int(limit),
        )
    )
    return [row[0] for row in cr.fetchall()]
