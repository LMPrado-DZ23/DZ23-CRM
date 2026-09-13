# Validação de mídia recebida (ADR-009), funções puras: MIME real detectado pelo
# CONTEÚDO (assinatura de arquivo), listas de permitidos/bloqueados, limite de
# tamanho, sha256 e allowlist de hosts de download (anti-SSRF).
import base64
import binascii
import hashlib
import os
import re
from urllib.parse import urlparse

from odoo.tools.mimetypes import guess_mimetype

DEFAULT_MAX_BYTES = 16 * 1024 * 1024
MEDIA_TYPES = ("image", "audio", "video", "document", "sticker")

ALLOWED_MIME = {
    "image": {"image/jpeg", "image/png", "image/webp", "image/gif"},
    "sticker": {"image/webp", "image/png"},
    "audio": {"audio/ogg", "audio/mpeg", "audio/mp4", "audio/amr", "audio/wav", "video/webm"},
    "video": {"video/mp4", "video/3gpp", "video/webm", "video/quicktime"},
    "document": {
        "application/pdf",
        "text/plain",
        "text/csv",
        "application/msword",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "image/jpeg",
        "image/png",
    },
}
BLOCKED_EXTENSIONS = {
    "exe", "bat", "cmd", "com", "scr", "pif", "cpl", "msi", "msp", "dll", "sys",
    "js", "jse", "vbs", "vbe", "wsf", "ps1", "psm1", "sh", "bash", "zsh", "py",
    "pl", "rb", "php", "jar", "apk", "app", "dmg", "iso", "lnk", "reg", "hta",
    "html", "htm", "xhtml", "svg", "svgz",
}  # fmt: skip
BLOCKED_MIME = {
    "application/x-dosexec",
    "application/x-msdownload",
    "application/x-executable",
    "application/x-elf",
    "application/x-sh",
    "application/java-archive",
    "application/vnd.android.package-archive",
    "application/javascript",
    "text/javascript",
    "text/html",
    "application/xhtml+xml",
    "image/svg+xml",
}
ALLOWED_DOWNLOAD_HOSTS = {"graph.facebook.com", "lookaside.fbsbx.com", "api.twilio.com"}
_EXTENSION_BY_MIME = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
    "audio/ogg": "ogg",
    "audio/mpeg": "mp3",
    "audio/mp4": "m4a",
    "audio/amr": "amr",
    "audio/wav": "wav",
    "video/mp4": "mp4",
    "video/3gpp": "3gp",
    "video/webm": "webm",
    "video/quicktime": "mov",
    "application/pdf": "pdf",
    "text/plain": "txt",
    "text/csv": "csv",
}
_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"%PDF-", "application/pdf"),
    (b"OggS", "audio/ogg"),
    (b"ID3", "audio/mpeg"),
    (b"\xff\xfb", "audio/mpeg"),
    (b"\xff\xf3", "audio/mpeg"),
    (b"#!AMR", "audio/amr"),
    (b"\x1aE\xdf\xa3", "video/webm"),
    (b"MZ", "application/x-dosexec"),
    (b"\x7fELF", "application/x-elf"),
    (b"#!", "application/x-sh"),
)


class MediaRejectedError(Exception):
    """Arquivo recusado por política (permanente: não adianta tentar de novo)."""


def sniff_mime(data):
    """MIME pelo conteúdo. Nunca confia no que o provedor/cliente declarou."""
    head = (data or b"")[:64]
    for signature, mime in _SIGNATURES:
        if head.startswith(signature):
            return mime
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "audio/wav"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"M4A ", b"M4B "):
            return "audio/mp4"
        if brand.startswith(b"3gp"):
            return "video/3gpp"
        if brand == b"qt  ":
            return "video/quicktime"
        return "video/mp4"
    lowered = (data or b"")[:1024].lstrip().lower()
    if lowered.startswith((b"<!doctype html", b"<html", b"<script", b"<?xml")) and (
        b"<svg" in lowered or b"<html" in lowered or b"<script" in lowered
    ):
        return "image/svg+xml" if b"<svg" in lowered else "text/html"
    if b"<svg" in lowered:
        return "image/svg+xml"
    guessed = guess_mimetype(data or b"")
    if guessed not in ("application/octet-stream", "text/plain"):
        return guessed
    sample = (data or b"")[:4096]
    if sample and b"\x00" not in sample:
        try:
            sample.decode("utf-8")
            return "text/plain"
        except UnicodeDecodeError:
            pass
    return "application/octet-stream"


def file_extension(filename):
    return os.path.splitext(str(filename or ""))[1].lstrip(".").lower()


def safe_filename(filename, mime, fallback="arquivo"):
    base = os.path.basename(str(filename or "")).strip()
    base = re.sub(r"[^\w.\- ]+", "_", base)[:120].strip(" .")
    if not base:
        base = fallback
    if not file_extension(base) and mime in _EXTENSION_BY_MIME:
        base = "%s.%s" % (base, _EXTENSION_BY_MIME[mime])
    return base


def sha256_matches(expected, data):
    """Aceita sha256 em hex (Meta) ou base64 (Evolution/WhatsApp)."""
    if not expected:
        return True
    digest = hashlib.sha256(data).digest()
    text = str(expected).strip()
    if re.fullmatch(r"[0-9a-fA-F]{64}", text):
        return digest.hex() == text.lower()
    try:
        return base64.b64decode(text, validate=True) == digest
    except (binascii.Error, ValueError):
        return False


def validate_media(
    message_type, data, filename=None, max_bytes=DEFAULT_MAX_BYTES, expected_sha256=None
):
    """Valida o arquivo. Retorna (mime_detectado, sha256_hex) ou levanta MediaRejectedError."""
    if not data:
        raise MediaRejectedError("arquivo vazio")
    if len(data) > max_bytes:
        raise MediaRejectedError("arquivo maior que o limite (%s bytes)" % max_bytes)
    if file_extension(filename) in BLOCKED_EXTENSIONS:
        raise MediaRejectedError("extensão de arquivo bloqueada")
    detected = sniff_mime(data)
    if detected in BLOCKED_MIME:
        raise MediaRejectedError("tipo de arquivo bloqueado (%s)" % detected)
    if detected not in ALLOWED_MIME.get(message_type, set()):
        raise MediaRejectedError("conteúdo %s incompatível com %s" % (detected, message_type))
    if not sha256_matches(expected_sha256, data):
        raise MediaRejectedError("sha256 do arquivo diverge do informado pelo provedor")
    return detected, hashlib.sha256(data).hexdigest()


def is_allowed_download_url(url):
    """Só HTTPS para hosts conhecidos dos provedores (evita SSRF)."""
    parsed = urlparse(str(url or ""))
    if parsed.scheme != "https" or parsed.username or parsed.password:
        return False
    host = (parsed.hostname or "").lower()
    return host in ALLOWED_DOWNLOAD_HOSTS or host.endswith(".fbsbx.com")
