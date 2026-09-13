# Serviço de IA plugável COM GOVERNANÇA (Fase 8). Provedores:
#   - ollama   : LOCAL e GRÁTIS (sem custo de token)
#   - groq     : free-tier (OpenAI-compatible)
#   - google   : Gemini free-tier
#   - openai   : pago
#   - anthropic: pago
# Governança: configuração POR EMPRESA (fallback nos parâmetros globais),
# consentimento para IA externa, redação de PII, imagem nunca vai para externo,
# timeout por provedor, 429 tipado, circuit breaker por empresa/provedor, limites
# diários/mensais por empresa e registro de uso (tokens, custo estimado, duração).
# Chaves/base ficam em ir.config_parameter (Ajustes, só admin), nunca no código.
import logging
import math
import re
import time
from contextlib import contextmanager

import requests
from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.tools.translate import _

_logger = logging.getLogger(__name__)

PROVIDERS = [
    ("ollama", "Local grátis (Ollama) — sem custo de token"),
    ("groq", "Groq (free-tier)"),
    ("google", "Google Gemini (free-tier)"),
    ("openai", "OpenAI (pago)"),
    ("anthropic", "Anthropic (pago)"),
]
_OPENAI_COMPAT_BASE = {
    "groq": "https://api.groq.com/openai/v1",
    "openai": "https://api.openai.com/v1",
}
_LOCAL_PROVIDERS = ("ollama",)
_DEFAULT_MODELS = {
    "ollama": "llama3.2:3b",
    "groq": "llama-3.3-70b-versatile",
    "google": "gemini-1.5-flash",
    "openai": "gpt-4o-mini",
    "anthropic": "claude-3-5-sonnet-latest",
}
# Timeout padrão (s) por provedor; sobrescrevível em dz23.ai.timeout.<provedor>.
_DEFAULT_TIMEOUTS = {"ollama": 60, "groq": 20, "google": 20, "openai": 30, "anthropic": 30}
# Preço estimado (USD por 1 milhão de tokens: entrada, saída). Sobrescrevível em
# dz23.ai.price.<provedor>.<modelo> = "entrada,saída". Local/free-tier = 0.
_DEFAULT_PRICES = {
    ("openai", "gpt-4o-mini"): (0.15, 0.60),
    ("openai", "gpt-4o"): (2.50, 10.00),
    ("anthropic", "claude-3-5-sonnet-latest"): (3.00, 15.00),
    ("anthropic", "claude-3-5-haiku-latest"): (0.80, 4.00),
}
_DEFAULT_RETRY_AFTER = 60
_CIRCUIT_OPEN = "circuit_open"

# Redação de PII para o que sai a provedores EXTERNOS (camada extra: o contexto
# enviado já é montado por allowlist).
_RE_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_RE_PHONE = re.compile(r"\+?\d[\d\s().-]{7,}\d")
_RE_DOC = re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b|\b\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}\b")


class AIError(UserError):
    """Falha ao usar o provedor de IA (conta no circuit breaker)."""

    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


class AIRateLimitedError(AIError):
    """Provedor respondeu 429."""


class AIConfigError(UserError):
    """Configuração ausente (chave/provedor) — não conta no circuit breaker."""


class AICircuitOpenError(UserError):
    """Circuit breaker aberto: a chamada nem é feita."""


class AILimitExceededError(UserError):
    """Limite de uso da empresa atingido."""


def _safe_url(url):
    """URL sem query string (evita vazar chave/token em log)."""
    return (url or "").split("?", 1)[0]


def _redact_pii(text):
    """Mascara e-mail, telefone e CPF/CNPJ antes de enviar a IA externa."""
    if not text:
        return text
    text = _RE_EMAIL.sub("[email]", text)
    text = _RE_DOC.sub("[documento]", text)
    text = _RE_PHONE.sub("[telefone]", text)
    return text


def estimate_tokens(text):
    return math.ceil(len(text or "") / 4)


class DZ23AI(models.AbstractModel):
    _name = "dz23.ai"
    _description = "DZ23 — Serviço de IA (local grátis + free-tier + pago, com governança)"

    def _cfg(self, key, default=""):
        return self.env["ir.config_parameter"].sudo().get_param(key, default)

    # ---------- configuração (empresa > global) ----------
    def _company(self, company=None):
        return (company or self.env.company).sudo()

    def _provider(self, company=None):
        company = self._company(company)
        return (
            company.dz23_ai_provider or self._cfg("dz23.ai_provider", "ollama") or "ollama"
        ).strip()

    def _model(self, company=None):
        company = self._company(company)
        return (
            company.dz23_ai_model or self._cfg("dz23.ai_model", "") or self._default_model(company)
        )

    def _default_model(self, company=None):
        return _DEFAULT_MODELS.get(self._provider(company), "llama3.2:3b")

    def _is_external(self, company=None):
        return self._provider(company) not in _LOCAL_PROVIDERS

    def _external_allowed(self, company=None):
        """Consentimento para IA externa: política da empresa ou, se 'padrão', global."""
        policy = self._company(company).dz23_ai_external_policy or "inherit"
        if policy == "allow":
            return True
        if policy == "deny":
            return False
        val = self._cfg("dz23.ai.external_allowed", "0")
        return (val or "0") not in ("0", "False", "false", "")

    def _timeout(self, provider):
        default = _DEFAULT_TIMEOUTS.get(provider, 30)
        try:
            value = int(self._cfg("dz23.ai.timeout.%s" % provider, "") or 0)
        except ValueError:
            return default
        return value if value > 0 else default

    def _price(self, provider, model):
        raw = self._cfg("dz23.ai.price.%s.%s" % (provider, model), "")
        if raw:
            try:
                price_in, price_out = (float(x) for x in raw.split(","))
                return price_in, price_out
            except ValueError:
                _logger.warning("Preço de IA inválido para %s/%s.", provider, model)
        return _DEFAULT_PRICES.get((provider, model), (0.0, 0.0))

    # ---------- API pública ----------
    @api.model
    def chat(self, prompt, system=None, image_b64=None, company=None, purpose="chat"):
        """Envia um prompt e retorna o texto (com todas as regras de governança)."""
        return self._chat(prompt, system, image_b64, company=company, purpose=purpose)["text"]

    @api.model
    def _chat(self, prompt, system=None, image_b64=None, company=None, purpose="chat"):
        if not prompt:
            raise UserError(_("Prompt vazio."))
        company = self._company(company)
        provider = self._provider(company)
        model = self._model(company)
        adapter = {
            "ollama": self._ollama,
            "groq": self._openai_compat,
            "openai": self._openai_compat,
            "google": self._gemini,
            "anthropic": self._anthropic,
        }.get(provider)
        if not adapter:
            raise AIConfigError(_("Provedor de IA não suportado: %s") % provider)
        # GATE de privacidade: externo só com consentimento; redige PII antes de sair.
        if provider not in _LOCAL_PROVIDERS:
            if not self._external_allowed(company):
                raise UserError(
                    _(
                        "Provedor de IA externo (%s) está desativado por política. "
                        "Use o modelo local (Ollama) ou registre o consentimento/base "
                        "legal da empresa para IA externa."
                    )
                    % provider
                )
            # Imagem não é redigível (uma foto de documento vazaria PII crua).
            if image_b64:
                raise UserError(
                    _(
                        "Envio de imagem a provedor de IA externo está bloqueado "
                        "por privacidade. Use o modelo local (Ollama) para imagens."
                    )
                )
            prompt = _redact_pii(prompt)
            system = _redact_pii(system)
        with self._governance() as genv:
            gcompany = genv["res.company"].browse(company.id)
            blocked = genv["dz23.ai"]._limit_violation(gcompany)
            if blocked:
                genv["dz23.ai.usage"]._log(
                    gcompany, provider, model, purpose, status="blocked", error=blocked
                )
            elif not genv["dz23.ai.breaker"]._get(gcompany, provider)._acquire():
                blocked = _CIRCUIT_OPEN
        # Levanta só DEPOIS do commit do cursor de governança.
        if blocked == _CIRCUIT_OPEN:
            raise AICircuitOpenError(
                _("IA (%s) temporariamente indisponível; o atendimento segue sem IA.") % provider
            )
        if blocked:
            raise AILimitExceededError(blocked)
        started = time.monotonic()
        try:
            result = adapter(provider, model, prompt, system, image_b64, self._timeout(provider))
        except AIConfigError:
            raise
        except AIError as error:
            retry_after = error.retry_after if isinstance(error, AIRateLimitedError) else None
            with self._governance() as genv:
                gcompany = genv["res.company"].browse(company.id)
                genv["dz23.ai.breaker"]._get(gcompany, provider)._record_failure(
                    retry_after=retry_after, error=type(error).__name__
                )
                genv["dz23.ai.usage"]._log(
                    gcompany,
                    provider,
                    model,
                    purpose,
                    status="rate_limited" if retry_after else "error",
                    duration_ms=int((time.monotonic() - started) * 1000),
                    error=type(error).__name__,
                )
            raise
        input_tokens = result.get("input_tokens") or (
            estimate_tokens(prompt) + estimate_tokens(system)
        )
        output_tokens = result.get("output_tokens") or estimate_tokens(result.get("text"))
        price_in, price_out = self._price(provider, model)
        with self._governance() as genv:
            gcompany = genv["res.company"].browse(company.id)
            genv["dz23.ai.breaker"]._get(gcompany, provider)._record_success()
            usage_id = (
                genv["dz23.ai.usage"]
                ._log(
                    gcompany,
                    provider,
                    model,
                    purpose,
                    status="ok",
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost_usd=(input_tokens * price_in + output_tokens * price_out) / 1_000_000,
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
                .id
            )
        return {
            "text": result.get("text") or "",
            "provider": provider,
            "model": model,
            "usage_id": usage_id,
        }

    @contextmanager
    def _governance(self):
        """Breaker, limites e uso em cursor PRÓPRIO, com commit ao sair: a falha de um
        provedor precisa ser lembrada mesmo quando o chamador desfaz a transação dele
        (savepoint do worker da fila, erro numa requisição HTTP). Nada aqui levanta."""
        self.env.flush_all()
        with self.env.registry.cursor() as cr:
            yield self.env(cr=cr, su=True)

    def _limit_violation(self, company):
        """Mensagem do limite excedido (ou None)."""
        Usage = self.env["dz23.ai.usage"].sudo()
        now = fields.Datetime.now()
        daily = company.dz23_ai_daily_call_limit or 0
        if daily:
            day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            calls = Usage.search_count(
                [
                    ("company_id", "=", company.id),
                    ("create_date", ">=", day_start),
                    ("status", "!=", "blocked"),
                ]
            )
            if calls >= daily:
                return _("Limite diário de chamadas de IA da empresa atingido (%s).") % daily
        monthly = company.dz23_ai_monthly_cost_limit_usd or 0.0
        if monthly:
            month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            [(spent,)] = Usage._read_group(
                [("company_id", "=", company.id), ("create_date", ">=", month_start)],
                aggregates=["cost_usd:sum"],
            )
            if (spent or 0.0) >= monthly:
                return _("Limite mensal de custo de IA da empresa atingido (US$ %.2f).") % monthly
        return None

    # ---------- HTTP ----------
    def _post(self, provider, url, timeout, **kwargs):
        try:
            resp = requests.post(url, timeout=timeout, **kwargs)
        except requests.exceptions.Timeout:
            _logger.warning("DZ23 IA timeout (%s) em %s", provider, _safe_url(url))
            raise AIError(_("A IA não respondeu a tempo (%s).") % provider) from None
        except requests.exceptions.RequestException:
            # NUNCA logar a exceção crua (pode conter URL com query/segredo).
            _logger.warning("DZ23 IA erro de rede (%s) em %s", provider, _safe_url(url))
            raise AIError(_("Não foi possível contatar a IA (%s).") % provider) from None
        if resp.status_code == 429:
            try:
                retry_after = int(resp.headers.get("Retry-After") or _DEFAULT_RETRY_AFTER)
            except ValueError:
                retry_after = _DEFAULT_RETRY_AFTER
            _logger.info("DZ23 IA %s limitou a taxa (429), retry em %ss", provider, retry_after)
            raise AIRateLimitedError(
                _("A IA (%s) limitou a taxa de uso; tente mais tarde.") % provider,
                retry_after=retry_after,
            )
        if resp.status_code >= 400:
            _logger.info("DZ23 IA %s -> %s", _safe_url(url), resp.status_code)
            raise AIError(_("A IA recusou a requisição (código %s).") % resp.status_code)
        try:
            return resp.json()
        except ValueError:
            raise AIError(_("Resposta inválida da IA (%s).") % provider) from None

    # ---------- Adaptadores (retornam texto + tokens) ----------
    def _ollama(self, provider, model, prompt, system, image_b64, timeout):
        base = (self._cfg("dz23.ai.ollama_base", "http://host.docker.internal:11434")).rstrip("/")
        msg = {"role": "user", "content": prompt}
        if image_b64:
            msg["images"] = [image_b64]
        messages = ([{"role": "system", "content": system}] if system else []) + [msg]
        data = self._post(
            provider,
            "%s/api/chat" % base,
            timeout,
            json={
                "model": model,
                "messages": messages,
                "stream": False,
                "options": {
                    "temperature": 0.6,
                    "top_p": 0.9,
                    "repeat_penalty": 1.2,
                    "num_predict": 220,
                },
            },
        )
        return {
            "text": (data.get("message") or {}).get("content", ""),
            "input_tokens": data.get("prompt_eval_count"),
            "output_tokens": data.get("eval_count"),
        }

    def _openai_compat(self, provider, model, prompt, system, image_b64, timeout):
        key = self._cfg("dz23.ai.%s_key" % provider)
        if not key:
            raise AIConfigError(_("Configure a chave de API do %s em Ajustes.") % provider)
        base = self._cfg("dz23.ai.%s_base" % provider) or _OPENAI_COMPAT_BASE.get(provider)
        if image_b64:
            content = [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,%s" % image_b64}},
            ]
        else:
            content = prompt
        messages = ([{"role": "system", "content": system}] if system else []) + [
            {"role": "user", "content": content}
        ]
        data = self._post(
            provider,
            "%s/chat/completions" % base.rstrip("/"),
            timeout,
            headers={"Authorization": "Bearer %s" % key},
            json={"model": model, "messages": messages},
        )
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise AIError(_("Resposta inesperada da IA (%s).") % provider) from None
        usage = data.get("usage") or {}
        return {
            "text": text,
            "input_tokens": usage.get("prompt_tokens"),
            "output_tokens": usage.get("completion_tokens"),
        }

    def _anthropic(self, provider, model, prompt, system, image_b64, timeout):
        key = self._cfg("dz23.ai.anthropic_key")
        if not key:
            raise AIConfigError(_("Configure a chave de API da Anthropic em Ajustes."))
        content = [{"type": "text", "text": prompt}]
        if image_b64:
            content.append(
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": image_b64},
                }
            )
        payload = {
            "model": model,
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": content}],
        }
        if system:
            payload["system"] = system
        data = self._post(
            provider,
            "https://api.anthropic.com/v1/messages",
            timeout,
            headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
            json=payload,
        )
        try:
            text = data["content"][0]["text"]
        except (KeyError, IndexError, TypeError):
            raise AIError(_("Resposta inesperada da IA (%s).") % provider) from None
        usage = data.get("usage") or {}
        return {
            "text": text,
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
        }

    def _gemini(self, provider, model, prompt, system, image_b64, timeout):
        key = self._cfg("dz23.ai.google_key")
        if not key:
            raise AIConfigError(_("Configure a chave de API do Google (Gemini) em Ajustes."))
        parts = [{"text": prompt}]
        if image_b64:
            parts.append({"inline_data": {"mime_type": "image/png", "data": image_b64}})
        payload = {"contents": [{"parts": parts}]}
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        # Chave no HEADER (x-goog-api-key), NUNCA na query string (evita log leak).
        url = "https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent" % model
        data = self._post(provider, url, timeout, headers={"x-goog-api-key": key}, json=payload)
        try:
            text = data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError):
            raise AIError(_("Resposta inesperada da IA (%s).") % provider) from None
        usage = data.get("usageMetadata") or {}
        return {
            "text": text,
            "input_tokens": usage.get("promptTokenCount"),
            "output_tokens": usage.get("candidatesTokenCount"),
        }
