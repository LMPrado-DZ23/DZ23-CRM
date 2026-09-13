# Normalizadores de payload de provedores (ADR-007). Funções PURAS (sem banco):
# recebem o payload já autenticado e devolvem uma LISTA de eventos no contrato
# interno. O domínio do CRM nunca lê o formato de Meta/Twilio/Evolution direto.
import base64
import hashlib
import hmac
import re
from datetime import UTC, datetime

from .message_event import STATUS_RANK, normalize_status

PROVIDERS = ("meta_cloud", "twilio", "evolution")
KINDS = ("message", "status", "connection")
MESSAGE_TYPES = (
    "text",
    "image",
    "audio",
    "video",
    "document",
    "location",
    "contact",
    "interactive",
    "reaction",
    "sticker",
    "unsupported",
)
# Conversas que não são atendimento 1:1: grupos, status/broadcast e canais.
_IGNORED_JID_SUFFIXES = ("@g.us", "@broadcast", "@newsletter")


def digits(value):
    return re.sub(r"\D", "", str(value or ""))


def parse_timestamp(value):
    """Epoch (s/ms) ou ISO-8601 -> datetime UTC naive; None se ausente/inválido."""
    if value in (None, ""):
        return None
    try:
        if isinstance(value, int | float) or str(value).strip().isdigit():
            number = float(value)
            if number > 1e12:  # milissegundos
                number /= 1000.0
            return datetime.fromtimestamp(number, tz=UTC).replace(tzinfo=None)
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        if parsed.tzinfo:
            parsed = parsed.astimezone(UTC).replace(tzinfo=None)
        return parsed
    except (ValueError, OverflowError, OSError):
        return None


def _base_event(provider, kind, **values):
    event = {
        "provider": provider,
        "kind": kind,
        "event_id": None,
        "direction": None,
        "provider_message_id": None,
        "provider_status": None,
        "status": "unknown",
        "sender": None,
        "recipient": None,
        "message_type": None,
        "text": None,
        "caption": None,
        "media": None,
        "location": None,
        "contacts": None,
        "reply_to": None,
        "occurred_at": None,
        "error_code": None,
        "error_message": None,
        "channel_ref": None,
        "state": None,
        "payload": None,
    }
    event.update(values)
    return event


def validate_event(event):
    """Valida o contrato interno. Levanta ValueError com o motivo."""
    if not isinstance(event, dict):
        raise ValueError("evento deve ser dict")
    if event.get("provider") not in PROVIDERS:
        raise ValueError("provider inválido")
    kind = event.get("kind")
    if kind not in KINDS:
        raise ValueError("kind inválido")
    occurred = event.get("occurred_at")
    if occurred is not None and not isinstance(occurred, datetime):
        raise ValueError("occurred_at deve ser datetime")
    if kind == "connection":
        if not event.get("state"):
            raise ValueError("state obrigatório em connection")
        return event
    if not event.get("provider_message_id"):
        raise ValueError("provider_message_id obrigatório")
    if event.get("direction") not in ("inbound", "outbound"):
        raise ValueError("direction inválida")
    if kind == "status":
        if event.get("status") not in STATUS_RANK:
            raise ValueError("status normalizado inválido")
        return event
    if event.get("message_type") not in MESSAGE_TYPES:
        raise ValueError("message_type inválido")
    if event["direction"] == "inbound" and not event.get("sender"):
        raise ValueError("sender obrigatório em mensagem recebida")
    return event


# ---------------------------------------------------------------- Meta Cloud
_META_TYPES = {
    "text": "text",
    "image": "image",
    "audio": "audio",
    "voice": "audio",
    "video": "video",
    "document": "document",
    "sticker": "sticker",
    "location": "location",
    "contacts": "contact",
    "interactive": "interactive",
    "button": "interactive",
    "reaction": "reaction",
}


def _meta_message(msg, phone_id, display_number):
    raw_type = msg.get("type") or "unsupported"
    mtype = _META_TYPES.get(raw_type, "unsupported")
    body = msg.get(raw_type) if isinstance(msg.get(raw_type), dict) else {}
    text = caption = media = location = contacts = None
    if mtype == "text":
        text = (msg.get("text") or {}).get("body")
    elif mtype in ("image", "audio", "video", "document", "sticker"):
        caption = body.get("caption")
        media = {
            "media_id": body.get("id"),
            "mime_type": body.get("mime_type"),
            "sha256": body.get("sha256"),
            "file_size": body.get("file_size"),
            "filename": body.get("filename"),
        }
    elif mtype == "location":
        location = {
            "latitude": body.get("latitude"),
            "longitude": body.get("longitude"),
            "name": body.get("name"),
            "address": body.get("address"),
        }
    elif mtype == "contact":
        contacts = msg.get("contacts") or []
    elif mtype == "interactive":
        if raw_type == "button":
            text = (msg.get("button") or {}).get("text")
        else:
            reply = body.get("button_reply") or body.get("list_reply") or {}
            text = reply.get("title")
    elif mtype == "reaction":
        text = body.get("emoji")
    return _base_event(
        "meta_cloud",
        "message",
        event_id=msg.get("id"),
        direction="inbound",
        provider_message_id=msg.get("id"),
        provider_status="received",
        sender=digits(msg.get("from")),
        recipient=digits(display_number),
        message_type=mtype,
        text=text,
        caption=caption,
        media=media,
        location=location,
        contacts=contacts,
        reply_to=(msg.get("context") or {}).get("id"),
        occurred_at=parse_timestamp(msg.get("timestamp")),
        channel_ref=phone_id,
        payload=msg,
    )


def _meta_status(status, phone_id):
    error = (status.get("errors") or [{}])[0]
    raw = status.get("status")
    return _base_event(
        "meta_cloud",
        "status",
        event_id="%s:%s" % (status.get("id"), raw),
        direction="outbound",
        provider_message_id=status.get("id"),
        provider_status=raw,
        status=normalize_status("meta_cloud", raw),
        recipient=digits(status.get("recipient_id")),
        occurred_at=parse_timestamp(status.get("timestamp")),
        error_code=error.get("code"),
        error_message=error.get("title") or error.get("message"),
        channel_ref=phone_id,
        payload=status,
    )


def normalize_meta(data):
    """Webhook Meta Cloud: TODAS as entries/changes/messages e statuses."""
    events = []
    for entry in (data or {}).get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            metadata = value.get("metadata") or {}
            phone_id = metadata.get("phone_number_id")
            display = metadata.get("display_phone_number")
            for msg in value.get("messages") or []:
                events.append(_meta_message(msg, phone_id, display))
            for status in value.get("statuses") or []:
                events.append(_meta_status(status, phone_id))
    return events


def meta_channel_refs(data):
    """Todos os phone_number_id presentes no payload (para validar o canal)."""
    refs = set()
    for entry in (data or {}).get("entry") or []:
        for change in entry.get("changes") or []:
            ref = ((change.get("value") or {}).get("metadata") or {}).get("phone_number_id")
            if ref:
                refs.add(str(ref))
    return refs


# ----------------------------------------------------------------- Evolution
_EVO_MEDIA = {
    "imageMessage": "image",
    "videoMessage": "video",
    "audioMessage": "audio",
    "documentMessage": "document",
    "documentWithCaptionMessage": "document",
    "stickerMessage": "sticker",
}
_EVO_WRAPPERS = (
    "ephemeralMessage",
    "viewOnceMessage",
    "viewOnceMessageV2",
    "viewOnceMessageV2Extension",
    "documentWithCaptionMessage",
    "editedMessage",
)


def _evo_unwrap(message):
    msg = message or {}
    for _i in range(4):
        for wrapper in _EVO_WRAPPERS:
            inner = (msg.get(wrapper) or {}).get("message") if isinstance(msg, dict) else None
            if inner:
                msg = inner
                break
        else:
            break
    return msg if isinstance(msg, dict) else {}


def _evo_content(message):
    msg = _evo_unwrap(message)
    text = caption = media = location = contacts = reply_to = None
    mtype = "unsupported"
    if msg.get("conversation"):
        mtype, text = "text", msg["conversation"]
    elif msg.get("extendedTextMessage"):
        ext = msg["extendedTextMessage"]
        mtype, text = "text", ext.get("text")
        reply_to = (ext.get("contextInfo") or {}).get("stanzaId")
    else:
        for key, kind in _EVO_MEDIA.items():
            body = msg.get(key)
            if body:
                mtype = kind
                caption = body.get("caption")
                media = {
                    "media_id": None,
                    "mime_type": body.get("mimetype"),
                    "sha256": body.get("fileSha256"),
                    "file_size": body.get("fileLength"),
                    "filename": body.get("fileName"),
                }
                reply_to = (body.get("contextInfo") or {}).get("stanzaId")
                break
        else:
            if msg.get("locationMessage"):
                loc = msg["locationMessage"]
                mtype = "location"
                location = {
                    "latitude": loc.get("degreesLatitude"),
                    "longitude": loc.get("degreesLongitude"),
                    "name": loc.get("name"),
                    "address": loc.get("address"),
                }
            elif msg.get("contactMessage") or msg.get("contactsArrayMessage"):
                mtype = "contact"
                contacts = msg.get("contactMessage") or msg.get("contactsArrayMessage")
            elif msg.get("buttonsResponseMessage"):
                mtype = "interactive"
                text = msg["buttonsResponseMessage"].get("selectedDisplayText")
            elif msg.get("listResponseMessage"):
                mtype = "interactive"
                text = msg["listResponseMessage"].get("title")
            elif msg.get("templateButtonReplyMessage"):
                mtype = "interactive"
                text = msg["templateButtonReplyMessage"].get("selectedDisplayText")
            elif msg.get("reactionMessage"):
                mtype = "reaction"
                text = msg["reactionMessage"].get("text")
    return mtype, text, caption, media, location, contacts, reply_to


def _evo_items(data):
    body = (data or {}).get("data")
    if isinstance(body, list):
        return [item for item in body if isinstance(item, dict)]
    return [body] if isinstance(body, dict) else []


def _ignored_jid(jid):
    return not jid or str(jid).endswith(_IGNORED_JID_SUFFIXES)


def evolution_event_name(data):
    return str((data or {}).get("event") or "").strip().lower().replace("_", ".")


def evolution_instance(data):
    return (data or {}).get("instance") or ((data or {}).get("data") or {}).get("instance")


def normalize_evolution(data):
    """Webhook Evolution (MESSAGES_UPSERT, MESSAGES_UPDATE, CONNECTION_UPDATE)."""
    name = evolution_event_name(data)
    instance = evolution_instance(data)
    events = []
    if name in ("", "messages.upsert"):
        for item in _evo_items(data):
            key = item.get("key") or {}
            jid = key.get("remoteJid") or ""
            mid = key.get("id")
            if _ignored_jid(jid) or not mid or not item.get("message"):
                continue
            mtype, text, caption, media, location, contacts, reply_to = _evo_content(
                item.get("message")
            )
            from_me = bool(key.get("fromMe"))
            number = digits(str(jid).split("@")[0])
            raw_status = item.get("status")
            events.append(
                _base_event(
                    "evolution",
                    "message",
                    event_id=mid,
                    direction="outbound" if from_me else "inbound",
                    provider_message_id=mid,
                    provider_status=raw_status,
                    status=normalize_status("evolution", raw_status) if from_me else "unknown",
                    sender=None if from_me else number,
                    recipient=number if from_me else None,
                    message_type=mtype,
                    text=text,
                    caption=caption,
                    media=media,
                    location=location,
                    contacts=contacts,
                    reply_to=reply_to,
                    occurred_at=parse_timestamp(item.get("messageTimestamp")),
                    channel_ref=instance,
                    payload=item,
                )
            )
    elif name == "messages.update":
        for item in _evo_items(data):
            key = item.get("key") or {}
            mid = item.get("keyId") or key.get("id")
            jid = item.get("remoteJid") or key.get("remoteJid") or ""
            from_me = item.get("fromMe", key.get("fromMe"))
            raw_status = item.get("status") or (item.get("update") or {}).get("status")
            # Só acompanhamos status do que NÓS enviamos (fromMe).
            if not mid or not from_me or _ignored_jid(jid) or raw_status in (None, ""):
                continue
            events.append(
                _base_event(
                    "evolution",
                    "status",
                    event_id="%s:%s" % (mid, raw_status),
                    direction="outbound",
                    provider_message_id=mid,
                    provider_status=str(raw_status),
                    status=normalize_status("evolution", raw_status),
                    recipient=digits(str(jid).split("@")[0]),
                    occurred_at=parse_timestamp(item.get("date_time") or item.get("datetime")),
                    channel_ref=instance,
                    payload=item,
                )
            )
    elif name == "connection.update":
        body = (data or {}).get("data") or {}
        state = body.get("state") if isinstance(body, dict) else None
        if state:
            events.append(
                _base_event(
                    "evolution",
                    "connection",
                    event_id="connection:%s:%s" % (instance, state),
                    state=str(state),
                    occurred_at=parse_timestamp((data or {}).get("date_time")),
                    channel_ref=instance,
                    payload=body,
                )
            )
    return events


# -------------------------------------------------------------------- Twilio
def _twilio_value(params, name):
    value = params.get(name)
    if isinstance(value, list | tuple):
        return value[0] if value else None
    return value


def _twilio_number(value):
    return digits(str(value or "").replace("whatsapp:", ""))


def normalize_twilio(params):
    """Webhook Twilio (form-encoded): mensagem recebida ou callback de status."""
    params = params or {}
    sid = _twilio_value(params, "MessageSid") or _twilio_value(params, "SmsSid")
    if not sid:
        return []
    message_status = (_twilio_value(params, "MessageStatus") or "").lower()
    sms_status = (_twilio_value(params, "SmsStatus") or "").lower()
    account = _twilio_value(params, "AccountSid")
    inbound = sms_status in ("received", "receiving") or (
        not message_status and _twilio_value(params, "Body") is not None
    )
    if message_status in ("received", "receiving"):
        inbound = True
    payload = {k: _twilio_value(params, k) for k in sorted(params)}
    if not inbound:
        return [
            _base_event(
                "twilio",
                "status",
                event_id="%s:%s" % (sid, message_status),
                direction="outbound",
                provider_message_id=sid,
                provider_status=message_status,
                status=normalize_status("twilio", message_status),
                recipient=_twilio_number(_twilio_value(params, "To")),
                error_code=_twilio_value(params, "ErrorCode"),
                error_message=_twilio_value(params, "ErrorMessage"),
                channel_ref=account,
                payload=payload,
            )
        ]
    body = _twilio_value(params, "Body")
    num_media = int(digits(_twilio_value(params, "NumMedia")) or 0)
    mtype, media, location = "text", None, None
    if num_media:
        content_type = _twilio_value(params, "MediaContentType0") or ""
        mtype = next(
            (
                kind
                for prefix, kind in (("image/", "image"), ("audio/", "audio"), ("video/", "video"))
                if content_type.startswith(prefix)
            ),
            "document",
        )
        media = {
            "media_id": _twilio_value(params, "MediaUrl0"),
            "mime_type": content_type or None,
            "sha256": None,
            "file_size": None,
            "filename": None,
        }
    elif _twilio_value(params, "Latitude"):
        mtype = "location"
        location = {
            "latitude": _twilio_value(params, "Latitude"),
            "longitude": _twilio_value(params, "Longitude"),
            "name": _twilio_value(params, "Label"),
            "address": _twilio_value(params, "Address"),
        }
    elif _twilio_value(params, "ButtonText"):
        mtype = "interactive"
        body = _twilio_value(params, "ButtonText")
    elif not body:
        mtype = "unsupported"
    return [
        _base_event(
            "twilio",
            "message",
            event_id=sid,
            direction="inbound",
            provider_message_id=sid,
            provider_status=sms_status or message_status or "received",
            sender=_twilio_number(_twilio_value(params, "From")),
            recipient=_twilio_number(_twilio_value(params, "To")),
            message_type=mtype,
            text=body if mtype in ("text", "interactive") else None,
            caption=body if media else None,
            media=media,
            location=location,
            reply_to=_twilio_value(params, "OriginalRepliedMessageSid"),
            channel_ref=account,
            payload=payload,
        )
    ]


def twilio_signature(auth_token, url, params):
    """X-Twilio-Signature: Base64(HMAC-SHA1(token, URL + nome+valor ordenados))."""
    data = str(url or "")
    for name in sorted(params or {}):
        values = params[name]
        values = values if isinstance(values, list | tuple) else [values]
        for value in sorted("" if v is None else str(v) for v in values):
            data += name + value
    mac = hmac.new((auth_token or "").encode("utf-8"), data.encode("utf-8"), "sha1")
    return base64.b64encode(mac.digest()).decode("ascii")


def verify_twilio_signature(auth_token, url, params, signature):
    if not auth_token or not signature:
        return False
    expected = twilio_signature(auth_token, url, params)
    return hmac.compare_digest(expected.encode(), str(signature).encode())


def meta_signature_valid(app_secret, raw_body, header_value):
    """X-Hub-Signature-256 = 'sha256=' + HMAC-SHA256(app_secret, corpo bruto)."""
    if not app_secret or not str(header_value or "").startswith("sha256="):
        return False
    expected = (
        "sha256=" + hmac.new(app_secret.encode(), raw_body or b"", hashlib.sha256).hexdigest()
    )
    return hmac.compare_digest(expected, header_value)
