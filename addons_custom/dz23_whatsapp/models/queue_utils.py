# Utilitários compartilhados das filas duráveis (inbox, outbox e eventos):
# claim com lease (ADR-003), backoff com jitter, serialização/hash de payload e
# saneamento de mensagens de erro (sem PII em banco/log).
import hashlib
import json
import re
import secrets

from odoo.tools import SQL, config

_JITTER = secrets.SystemRandom()
_PREVIEW_CHARS = 500
_RE_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_RE_LONG_DIGITS = re.compile(r"\d{8,}")
_NOW_UTC = SQL("(clock_timestamp() AT TIME ZONE 'utc')")


def backoff_seconds(attempts):
    """Backoff exponencial (teto 1 h) com jitter de até 50%."""
    base = min(3600, 2 ** max(1, int(attempts or 0)))
    return base + _JITTER.randint(0, max(1, base // 2))


def sanitize_error(message, limit=200):
    """Erro curto, sem e-mail nem sequências longas de dígitos (telefone/documento)."""
    text = _RE_EMAIL.sub("[email]", str(message or ""))
    text = _RE_LONG_DIGITS.sub("[num]", text)
    return text[:limit]


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


def claim_due(cr, table, from_states, to_state, lease_seconds, limit):
    """Reivindica até `limit` itens vencidos de forma atômica e curta.

    O SELECT interno usa FOR UPDATE SKIP LOCKED (dois workers nunca pegam o mesmo
    item); o UPDATE muda o estado, incrementa `attempts` e grava o lease. Retorna
    os ids reivindicados. O chamador faz commit antes de processar (ADR-003).
    """
    cr.execute(
        SQL(
            """
            UPDATE %(table)s AS q
               SET status = %(to_state)s,
                   attempts = q.attempts + 1,
                   lease_until = %(now)s + %(lease)s * interval '1 second'
             WHERE q.id IN (
                   SELECT id FROM %(table)s
                    WHERE status = ANY(%(from_states)s)
                      AND next_attempt_at <= %(now)s
                    ORDER BY next_attempt_at, id
                    LIMIT %(limit)s
                    FOR UPDATE SKIP LOCKED)
         RETURNING q.id
            """,
            table=SQL.identifier(table),
            to_state=to_state,
            now=_NOW_UTC,
            lease=int(lease_seconds),
            from_states=list(from_states),
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
