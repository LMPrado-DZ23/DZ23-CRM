# Mídia recebida (ADR-009): registrada com o item da inbox e BAIXADA por um worker
# próprio (claim + lease), validada (tamanho, MIME real, extensão, sha256) e guardada
# como anexo PRIVADO da empresa do canal. Arquivo recusado não é tentado de novo;
# falha de rede/provedor tenta com backoff. Retenção remove o arquivo vencido.
import logging
import time

from odoo import api, fields, models

from .media_utils import DEFAULT_MAX_BYTES, MediaRejectedError, safe_filename, validate_media
from .provider_errors import ProviderError
from .queue_utils import backoff_seconds, can_commit, claim_due, expired_leases, sanitize_error

_logger = logging.getLogger(__name__)

_MAX_ATTEMPTS = 5
_BATCH = 10
_LEASE_SECONDS = 300
_DEFAULT_RETENTION_DAYS = 180


class DZ23MessageMedia(models.Model):
    _name = "dz23.message.media"
    _description = "DZ23 — Mídia recebida pelo WhatsApp (anexo privado)"
    _order = "id desc"
    _rec_name = "filename"

    inbox_id = fields.Many2one(
        "dz23.message.inbox", required=True, ondelete="cascade", index=True, readonly=True
    )
    channel_id = fields.Many2one(
        related="inbox_id.channel_id", store=True, index=True, readonly=True
    )
    company_id = fields.Many2one(
        related="channel_id.company_id", store=True, index=True, readonly=True
    )
    provider = fields.Char(readonly=True)
    provider_message_id = fields.Char(index=True, readonly=True)
    message_type = fields.Selection(
        [
            ("image", "Imagem"),
            ("audio", "Áudio"),
            ("video", "Vídeo"),
            ("document", "Documento"),
            ("sticker", "Figurinha"),
        ],
        required=True,
        readonly=True,
    )
    media_id = fields.Char("Referência no provedor", readonly=True)
    declared_mime = fields.Char("MIME declarado", readonly=True)
    mime_type = fields.Char("MIME detectado", readonly=True)
    filename = fields.Char(readonly=True)
    caption = fields.Text(readonly=True)
    file_size = fields.Integer(readonly=True)
    sha256 = fields.Char(readonly=True)
    expected_sha256 = fields.Char(readonly=True)
    attachment_id = fields.Many2one("ir.attachment", ondelete="set null", readonly=True)
    transcription = fields.Text(help="Transcrição/descrição preenchida pelo atendente.")
    status = fields.Selection(
        [
            ("pending", "Pendente"),
            ("processing", "Baixando"),
            ("failed", "Falha (retry)"),
            ("downloaded", "Baixada"),
            ("rejected", "Recusada"),
            ("dead", "Falhou"),
            ("expired", "Expirada (retenção)"),
        ],
        default="pending",
        required=True,
        index=True,
        readonly=True,
    )
    attempts = fields.Integer(default=0, readonly=True)
    max_attempts = fields.Integer(default=_MAX_ATTEMPTS, readonly=True)
    next_attempt_at = fields.Datetime(default=fields.Datetime.now, index=True, readonly=True)
    lease_until = fields.Datetime(index=True, readonly=True)
    downloaded_at = fields.Datetime(readonly=True)
    expires_at = fields.Datetime(index=True, readonly=True)
    error = fields.Char(readonly=True)

    @api.model
    def _enqueue_from_event(self, inbox, event):
        media = event.get("media") or {}
        mtype = event.get("message_type")
        if mtype not in ("image", "audio", "video", "document", "sticker"):
            return self.browse()
        if not (media.get("media_id") or inbox.provider == "evolution"):
            return self.browse()
        return self.sudo().create(
            {
                "inbox_id": inbox.id,
                "provider": inbox.provider,
                "provider_message_id": inbox.message_id,
                "message_type": mtype,
                "media_id": media.get("media_id") or False,
                "declared_mime": media.get("mime_type") or False,
                "filename": media.get("filename") or False,
                "caption": event.get("caption") or False,
                "expected_sha256": media.get("sha256") or False,
            }
        )

    # ---------- worker ----------
    @api.model
    def _cron_process(self, limit=_BATCH):
        self.env.flush_all()
        for rec in self.browse(expired_leases(self.env.cr, self._table, "processing")):
            rec._register_failure(RuntimeError("lease expirado"))
        self.env.flush_all()
        ids = claim_due(
            self.env.cr, self._table, ("pending", "failed"), "processing", _LEASE_SECONDS, limit
        )
        if not ids:
            return
        self.invalidate_model(["status", "attempts", "lease_until"])
        if can_commit():
            self.env.cr.commit()
        for rec in self.browse(ids):
            rec._process_one()
            if can_commit():
                self.env.cr.commit()

    def _max_bytes(self):
        value = self.env["ir.config_parameter"].sudo().get_param("dz23.whatsapp.media_max_bytes")
        try:
            return int(value) if value else DEFAULT_MAX_BYTES
        except ValueError:
            return DEFAULT_MAX_BYTES

    def _retention_days(self):
        value = (
            self.env["ir.config_parameter"]
            .sudo()
            .get_param("dz23.whatsapp.media_retention_days", _DEFAULT_RETENTION_DAYS)
        )
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return _DEFAULT_RETENTION_DAYS

    def _process_one(self):
        self.ensure_one()
        if self.status in ("downloaded", "rejected", "dead", "expired"):
            return
        if self.status != "processing":
            self.write({"status": "processing", "attempts": self.attempts + 1})
        started = time.monotonic()
        try:
            with self.env.cr.savepoint():
                channel = self.channel_id.sudo()
                data, declared, expected = channel._media_fetch(self, self._max_bytes())
                mime, digest = validate_media(
                    self.message_type,
                    data,
                    filename=self.filename,
                    max_bytes=self._max_bytes(),
                    expected_sha256=expected or self.expected_sha256,
                )
                name = safe_filename(self.filename, mime, fallback="whatsapp-%s" % self.id)
                attachment = (
                    self.env["ir.attachment"]
                    .sudo()
                    .create(
                        {
                            "name": name,
                            "raw": data,
                            "mimetype": mime,
                            "res_model": self._name,
                            "res_id": self.id,
                            "company_id": self.company_id.id,
                            "public": False,
                        }
                    )
                )
                now = fields.Datetime.now()
                days = self._retention_days()
                self.write(
                    {
                        "status": "downloaded",
                        "attachment_id": attachment.id,
                        "mime_type": mime,
                        "declared_mime": self.declared_mime or declared or False,
                        "filename": name,
                        "file_size": len(data),
                        "sha256": digest,
                        "downloaded_at": now,
                        "expires_at": fields.Datetime.add(now, days=days) if days else False,
                        "lease_until": False,
                        "error": False,
                    }
                )
            try:
                with self.env.cr.savepoint():
                    self.channel_id.sudo()._on_media_downloaded(self)
            except Exception as hook_error:  # noqa: BLE001 - o arquivo já está salvo
                _logger.warning(
                    "Mídia %s: pós-download falhou (%s).", self.id, type(hook_error).__name__
                )
        except MediaRejectedError as rejection:
            self.write(
                {"status": "rejected", "lease_until": False, "error": sanitize_error(rejection)}
            )
            _logger.info("Mídia %s recusada: %s", self.id, rejection)
        except Exception as error:  # noqa: BLE001 - retry
            self._register_failure(error)
        _logger.debug(
            "Mídia %s processada em %.0f ms", self.id, (time.monotonic() - started) * 1000
        )

    def _register_failure(self, error):
        permanent = isinstance(error, ProviderError) and error.permanent
        if permanent or self.attempts >= self.max_attempts:
            self.write({"status": "dead", "lease_until": False, "error": sanitize_error(error)})
            return
        self.write(
            {
                "status": "failed",
                "lease_until": False,
                "error": sanitize_error("%s: %s" % (type(error).__name__, error)),
                "next_attempt_at": fields.Datetime.add(
                    fields.Datetime.now(), seconds=backoff_seconds(self.attempts)
                ),
            }
        )

    def action_retry(self):
        self.filtered(lambda m: m.status in ("failed", "dead")).write(
            {
                "status": "pending",
                "attempts": 0,
                "next_attempt_at": fields.Datetime.now(),
                "error": False,
            }
        )
        return True

    # ---------- retenção ----------
    @api.model
    def _cron_purge_expired(self, limit=500):
        expired = self.sudo().search(
            [
                ("status", "=", "downloaded"),
                ("expires_at", "!=", False),
                ("expires_at", "<", fields.Datetime.now()),
            ],
            limit=limit,
        )
        for media in expired:
            media.attachment_id.sudo().unlink()
            media.write({"status": "expired", "attachment_id": False})
        if expired:
            _logger.info("Retenção: %s arquivo(s) de mídia removido(s).", len(expired))
        return len(expired)
