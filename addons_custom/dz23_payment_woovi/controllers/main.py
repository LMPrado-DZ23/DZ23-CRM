# Webhook Woovi (PIX).
# SEGURANÇA (fail-closed): só processa se a assinatura RSA do corpo bruto for válida
# contra a chave pública da Woovi (ir.config_parameter dz23.woovi.webhook_pubkey —
# chave pública da própria Woovi, igual para todas as contas). Sem chave ou
# assinatura inválida => 401. Corpo > 1 MiB => 413. JSON inválido => 400.
# O evento é PERSISTIDO (deduplicado) antes do 200; falha de persistência => 500.
import base64
import binascii
import json
import logging

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)

_PUBKEY_PARAM = "dz23.woovi.webhook_pubkey"
_MAX_BODY = 1024 * 1024  # 1 MiB


def _verify_woovi_signature(raw_body, signature_b64, pubkey_pem):
    """Verifica RSA-SHA256 (PKCS1v15) do corpo bruto com a chave pública Woovi."""
    if not pubkey_pem or not signature_b64:
        return False
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding

        public_key = serialization.load_pem_public_key(pubkey_pem.encode())
        public_key.verify(
            base64.b64decode(signature_b64),
            raw_body,
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        return True
    except (ValueError, binascii.Error, Exception):  # noqa: BLE001 - falha = inválido
        return False


class WooviController(http.Controller):
    @http.route("/payment/woovi/webhook", type="http", auth="public", methods=["POST"], csrf=False)
    def woovi_webhook(self, **_kwargs):
        if (request.httprequest.content_length or 0) > _MAX_BODY:
            return request.make_response("payload too large", status=413)
        raw = request.httprequest.get_data() or b""
        if len(raw) > _MAX_BODY:
            return request.make_response("payload too large", status=413)
        signature = request.httprequest.headers.get("x-webhook-signature", "")
        pubkey = request.env["ir.config_parameter"].sudo().get_param(_PUBKEY_PARAM, "")
        if not _verify_woovi_signature(raw, signature, pubkey):
            _logger.warning("Woovi webhook REJEITADO (assinatura ausente/inválida).")
            return request.make_response("unauthorized", status=401)
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            return request.make_response("bad request", status=400)
        if not isinstance(data, dict):
            return request.make_response("bad request", status=400)
        try:
            event, created = (
                request.env["dz23.woovi.event"]
                .sudo()
                ._ingest_payload(data, source="webhook", raw=raw)
            )
        except Exception as error:  # noqa: BLE001 - 500 controlado para a Woovi reenviar
            _logger.error("Woovi webhook: falha ao persistir evento (%s).", type(error).__name__)
            return request.make_response("retry later", status=500)
        _logger.info(
            "Woovi webhook OK: evento=%s novo=%s estado=%s", event.id, created, event.state
        )
        return request.make_response("ok")
