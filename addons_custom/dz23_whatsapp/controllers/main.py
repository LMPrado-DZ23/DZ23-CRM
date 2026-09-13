# Webhooks de entrada do WhatsApp (Meta Cloud + Evolution), MULTI-TENANT.
# A URL carrega um token opaco que resolve O CANAL (e sua empresa). A auth é
# POR CANAL (apikey/App Secret do canal), fail-closed: token desconhecido ou
# credencial ausente/inválida => recusa; corpo grande => 413; JSON inválido => 400.
# O processamento roda no escopo da empresa do canal. Loga só metadados.
import hashlib
import hmac
import json
import logging

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)

_MAX_BODY = 1 * 1024 * 1024  # 1 MiB


def _const_eq(a, b):
    return hmac.compare_digest((a or "").encode(), (b or "").encode())


def _read_body():
    raw = request.httprequest.get_data() or b""
    if len(raw) > _MAX_BODY:
        return None, request.make_response("payload too large", status=413)
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        return None, request.make_response("bad request", status=400)
    if not isinstance(data, dict):
        return None, request.make_response("bad request", status=400)
    return (raw, data), None


def _persist_inbound(channel, message_id, number, text, data):
    """Persistência mínima ANTES do 200. Falha => False => HTTP 500, para o
    provedor reentregar (o dedupe do inbox absorve a reentrega). Loga só metadados."""
    try:
        request.env["dz23.message.inbox"].sudo()._enqueue(channel, message_id, number, text, data)
        return True
    except Exception as e:  # noqa: BLE001 - converte em 500 controlado
        _logger.error(
            "Falha ao persistir inbound canal=%s provider=%s erro=%s",
            channel.id,
            channel.provider,
            type(e).__name__,
        )
        return False


class DZ23WhatsAppWebhook(http.Controller):
    # ---------------- Evolution (tokenizado, por canal) ----------------
    @http.route(
        "/dz23/whatsapp/evolution/webhook/<token>",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
    )
    def evolution_webhook(self, token, **_kw):
        channel = request.env["dz23.channel"]._resolve_by_token(token)
        # FAIL-CLOSED: token desconhecido ou canal sem segredo de callback => recusa.
        if not channel or channel.provider != "evolution":
            return request.make_response("not found", status=404)
        if not channel.callback_secret:
            return request.make_response("service unavailable", status=503)
        # Autentica pelo segredo de callback do canal (independente da chave admin).
        req_secret = request.httprequest.headers.get("X-DZ23-Callback", "")
        if not _const_eq(req_secret, channel.callback_secret):
            _logger.warning("Evolution webhook REJEITADO (callback) canal=%s", channel.id)
            return request.make_response("unauthorized", status=401)
        parsed, err = _read_body()
        if err:
            return err
        raw, data = parsed
        # valida que o evento é da instância deste canal
        inst = data.get("instance") or ((data.get("data") or {}).get("instance"))
        if channel.evo_instance and inst and inst != channel.evo_instance:
            return request.make_response("conflict", status=409)
        svc = request.env["dz23.whatsapp"].sudo()
        number, text = svc._parse_evolution_inbound(data)
        if number and text:
            # Só PERSISTE no inbox durável (dedupe) e confirma rápido; o worker
            # processa depois. Nunca roda IA/negócio de forma síncrona aqui.
            mid = svc._extract_message_id("evolution", data)
            if not _persist_inbound(channel, mid, number, text, data):
                return request.make_response("retry later", status=500)
        return request.make_response("ok")

    # ---------------- Meta Cloud (tokenizado, por canal) ----------------
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
            and _const_eq(kw.get("hub.verify_token"), verify)
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
        raw = request.httprequest.get_data() or b""
        if len(raw) > _MAX_BODY:
            return request.make_response("payload too large", status=413)
        sig = request.httprequest.headers.get("X-Hub-Signature-256", "")
        expected = (
            "sha256=" + hmac.new(channel.meta_app_secret.encode(), raw, hashlib.sha256).hexdigest()
        )
        if not (sig.startswith("sha256=") and hmac.compare_digest(expected, sig)):
            _logger.warning("Meta webhook REJEITADO (assinatura) canal=%s", channel.id)
            return request.make_response("unauthorized", status=401)
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            return request.make_response("bad request", status=400)
        if not isinstance(data, dict):
            return request.make_response("bad request", status=400)
        svc = request.env["dz23.whatsapp"].sudo()
        number, text = svc._parse_meta_inbound(data)
        if number and text:
            mid = svc._extract_message_id("meta_cloud", data)
            if not _persist_inbound(channel, mid, number, text, data):
                return request.make_response("retry later", status=500)
        return request.make_response("ok")
