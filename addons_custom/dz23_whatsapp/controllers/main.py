# Webhooks de entrada do WhatsApp (Meta Cloud, Evolution e Twilio), MULTI-TENANT.
# A URL carrega um token opaco que resolve O CANAL (e sua empresa). A auth é POR
# CANAL e fail-closed: token desconhecido => 404; credencial ausente => 503;
# assinatura/segredo inválido => 401; corpo grande => 413; JSON inválido => 400;
# payload de outro canal (phone_number_id / instance / AccountSid) => 409.
# O payload autenticado passa pelos normalizadores (ADR-007) e é PERSISTIDO antes
# do 200; falha de persistência => 500 (o provedor reentrega). Loga só metadados.
import json
import logging
import time

from odoo import http
from odoo.http import request

from ..models import provider_normalizers as pn
from ..models.queue_utils import log_event

_logger = logging.getLogger(__name__)

_MAX_BODY = 1 * 1024 * 1024  # 1 MiB
# Teto de eventos por requisição (anti-DoS). A Meta agrupa no máximo ~1000 updates.
_MAX_EVENTS = 1000


def _too_many(events):
    return len(events) > _MAX_EVENTS


def _too_large():
    return (request.httprequest.content_length or 0) > _MAX_BODY


def _parse_json(raw):
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _ingest(channel, events):
    """Persistência mínima ANTES do 200. Falha => False => HTTP 500."""
    started = time.monotonic()
    try:
        channel.sudo()._ingest_events(events)
        log_event(
            _logger,
            "webhook_ingested",
            channel=channel.id,
            provider=channel.provider,
            events=len(events),
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return True
    except Exception as e:  # noqa: BLE001 - converte em 500 controlado
        _logger.error(
            "Falha ao persistir webhook canal=%s provider=%s erro=%s",
            channel.id,
            channel.provider,
            type(e).__name__,
        )
        return False


class DZ23WhatsAppWebhook(http.Controller):
    # ---------------- Evolution ----------------
    @http.route(
        "/dz23/whatsapp/evolution/webhook/<token>",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
    )
    def evolution_webhook(self, token, **_kw):
        channel = request.env["dz23.channel"]._resolve_by_token(token)
        if not channel or channel.provider != "evolution":
            return request.make_response("not found", status=404)
        if not channel.callback_secret:
            return request.make_response("service unavailable", status=503)
        req_secret = request.httprequest.headers.get("X-DZ23-Callback", "")
        if not pn.hmac.compare_digest(req_secret.encode(), channel.callback_secret.encode()):
            _logger.warning("Evolution webhook REJEITADO (callback) canal=%s", channel.id)
            return request.make_response("unauthorized", status=401)
        if _too_large():
            return request.make_response("payload too large", status=413)
        raw = request.httprequest.get_data() or b""
        if len(raw) > _MAX_BODY:
            return request.make_response("payload too large", status=413)
        data = _parse_json(raw)
        if data is None:
            return request.make_response("bad request", status=400)
        inst = pn.evolution_instance(data)
        # A Evolution sempre envia a instância: ausente ou divergente => recusa
        # (defesa em profundidade além do segredo de callback).
        if channel.evo_instance and inst != channel.evo_instance:
            return request.make_response("conflict", status=409)
        events = pn.normalize_evolution(data)
        if _too_many(events):
            return request.make_response("payload too large", status=413)
        if not _ingest(channel, events):
            return request.make_response("retry later", status=500)
        return request.make_response("ok")

    # ---------------- Meta Cloud ----------------
    @http.route(
        "/dz23/whatsapp/meta/webhook/<token>",
        type="http",
        auth="public",
        methods=["GET"],
        csrf=False,
    )
    def meta_verify(self, token, **kw):
        channel = request.env["dz23.channel"]._resolve_by_token(token)
        if not channel or channel.provider != "meta_cloud":
            return request.make_response("not found", status=404)
        verify = channel.meta_verify_token or ""
        if (
            kw.get("hub.mode") == "subscribe"
            and verify
            and pn.hmac.compare_digest(
                str(kw.get("hub.verify_token") or "").encode(), verify.encode()
            )
        ):
            return request.make_response(kw.get("hub.challenge", ""))
        return request.make_response("forbidden", status=403)

    @http.route(
        "/dz23/whatsapp/meta/webhook/<token>",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
    )
    def meta_webhook(self, token, **_kw):
        channel = request.env["dz23.channel"]._resolve_by_token(token)
        if not channel or channel.provider != "meta_cloud":
            return request.make_response("not found", status=404)
        if not channel.meta_app_secret:
            return request.make_response("service unavailable", status=503)
        if _too_large():
            return request.make_response("payload too large", status=413)
        raw = request.httprequest.get_data() or b""
        if len(raw) > _MAX_BODY:
            return request.make_response("payload too large", status=413)
        sig = request.httprequest.headers.get("X-Hub-Signature-256", "")
        if not pn.meta_signature_valid(channel.meta_app_secret, raw, sig):
            _logger.warning("Meta webhook REJEITADO (assinatura) canal=%s", channel.id)
            return request.make_response("unauthorized", status=401)
        data = _parse_json(raw)
        if data is None:
            return request.make_response("bad request", status=400)
        refs = pn.meta_channel_refs(data)
        if refs and refs != {str(channel.meta_phone_id or "")}:
            _logger.warning("Meta webhook de outro phone_number_id canal=%s", channel.id)
            return request.make_response("conflict", status=409)
        events = pn.normalize_meta(data)
        if _too_many(events):
            return request.make_response("payload too large", status=413)
        if not _ingest(channel, events):
            return request.make_response("retry later", status=500)
        return request.make_response("ok")

    # ---------------- Twilio ----------------
    @http.route(
        "/dz23/whatsapp/twilio/webhook/<token>",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
    )
    def twilio_webhook(self, token, **_kw):
        channel = request.env["dz23.channel"]._resolve_by_token(token)
        if not channel or channel.provider != "twilio":
            return request.make_response("not found", status=404)
        if not (channel.twilio_token and channel.twilio_sid):
            return request.make_response("service unavailable", status=503)
        if _too_large():
            return request.make_response("payload too large", status=413)
        form = request.httprequest.form
        params = {name: form.getlist(name) for name in form}
        signature = request.httprequest.headers.get("X-Twilio-Signature", "")
        candidates = channel._twilio_signature_urls(request.httprequest.url)
        if not any(
            pn.verify_twilio_signature(channel.twilio_token, url, params, signature)
            for url in candidates
        ):
            # Nunca confiar em IP de origem: sem assinatura válida, recusa.
            _logger.warning("Twilio webhook REJEITADO (assinatura) canal=%s", channel.id)
            return request.make_response("unauthorized", status=401)
        events = pn.normalize_twilio(params)
        if not channel._twilio_payload_matches(params, events):
            return request.make_response("conflict", status=409)
        if not _ingest(channel, events):
            return request.make_response("retry later", status=500)
        return request.make_response(
            "<Response></Response>", headers=[("Content-Type", "text/xml")]
        )
