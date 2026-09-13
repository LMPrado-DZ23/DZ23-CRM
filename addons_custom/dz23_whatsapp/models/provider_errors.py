# Erros de provedor TIPADOS (ADR-008): transitório (rede, 408/425/429/5xx e códigos
# de rate limit do provedor) volta para retry respeitando Retry-After; permanente
# (demais 4xx, configuração do canal, número inválido) vai direto para a DLQ.
# Guardam só status HTTP + código do provedor — nunca o corpo (pode conter PII).
import email.utils
from datetime import UTC, datetime

from odoo.exceptions import UserError

_TRANSIENT_HTTP = {408, 425, 429}
_MAX_RETRY_AFTER = 86400
# Códigos de erro que chegam com 4xx mas são temporários (rate limit/instabilidade):
# Meta 4/80007/130429/131048/131056 (limites de envio), 1/2 (erro temporário);
# Twilio 20429 (too many requests).
_TRANSIENT_PROVIDER_CODES = {1, 2, 4, 80007, 130429, 131048, 131056, 20429}


class ProviderError(UserError):
    """Falha ao falar com o provedor de mensagens."""

    permanent = False

    def __init__(self, message, status=None, code=None, retry_after=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.retry_after = retry_after


class ProviderTransientError(ProviderError):
    permanent = False


class ProviderPermanentError(ProviderError):
    permanent = True


def parse_retry_after(value, now=None):
    """Retry-After em segundos ou data HTTP -> segundos (limitado a 24 h)."""
    if value in (None, ""):
        return None
    text = str(value).strip()
    if text.isdigit():
        return min(int(text), _MAX_RETRY_AFTER)
    try:
        when = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    now = now or datetime.now(UTC)
    return max(0, min(_MAX_RETRY_AFTER, int((when - now).total_seconds())))


def provider_error_code(data):
    """Código de erro do provedor a partir do JSON de erro (Meta/Twilio)."""
    if not isinstance(data, dict):
        return None
    error = data.get("error")
    if isinstance(error, dict):
        return error.get("code") or error.get("error_subcode")
    if data.get("code") is not None:  # Twilio
        return data.get("code")
    return None


def classify_http_error(status, headers=None, data=None):
    """Resposta HTTP >= 400 -> exceção tipada."""
    code = provider_error_code(data)
    retry_after = parse_retry_after((headers or {}).get("Retry-After"))
    try:
        numeric_code = int(code) if code is not None else None
    except (TypeError, ValueError):
        numeric_code = None
    message = "HTTP %s%s" % (status, " (código %s)" % code if code is not None else "")
    if status in _TRANSIENT_HTTP or status >= 500 or numeric_code in _TRANSIENT_PROVIDER_CODES:
        return ProviderTransientError(
            "Provedor indisponível/limitado: %s" % message,
            status=status,
            code=code,
            retry_after=retry_after,
        )
    return ProviderPermanentError(
        "Provedor recusou o envio: %s" % message, status=status, code=code
    )
