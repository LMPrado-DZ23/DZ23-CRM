# Canal de mensageria multi-tenant. UM canal pertence a EXATAMENTE UMA empresa
# (company_id) e carrega as PRÓPRIAS credenciais + prompt do agente — nada de
# ir.config_parameter global. O webhook resolve o canal por um token opaco e
# autentica por canal. Todo o processamento roda no escopo da empresa do canal.
import logging
import re
import secrets

import requests
from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools.translate import _

from .provider_errors import (
    ProviderPermanentError,
    ProviderTransientError,
    classify_http_error,
)
from .provider_normalizers import validate_event

_logger = logging.getLogger(__name__)
_TIMEOUT = 15


def _digits(v):
    return re.sub(r"\D", "", v or "")


def _e164_br(number):
    """Normaliza para dígitos E.164; assume Brasil (55) quando faltar DDI."""
    raw = _digits(number)
    if not raw:
        return ""
    if not raw.startswith("55") and len(raw) in (10, 11):
        raw = "55" + raw
    return raw


def ensure_bot_user(env):
    """Cria (idempotente) o usuário técnico dz23_whatsapp_bot via ORM (aplica os
    defaults dos campos, evitando NOT NULL em res_partner) e registra o xmlid.
    Chamado no post_init (instalação) e na migração (upgrade)."""
    imd = env["ir.model.data"]
    existing = imd.search(
        [("module", "=", "dz23_whatsapp"), ("name", "=", "user_dz23_bot")], limit=1
    )
    if existing:
        return env["res.users"].browse(existing.res_id)
    groups = [env.ref("base.group_user").id]
    salesman = env.ref("sales_team.group_sale_salesman", raise_if_not_found=False)
    if salesman:
        groups.append(salesman.id)
    vals = {
        "name": "DZ23 WhatsApp Bot",
        "login": "dz23_whatsapp_bot",
        "share": False,
        "group_ids": [(6, 0, groups)],
    }
    # Alguns campos NOT NULL de res.partner (ex.: autopost_bills do account) não
    # recebem default neste create em tempo de load; preenche defensivamente.
    Partner = env["res.partner"]
    if "autopost_bills" in Partner._fields:
        vals["autopost_bills"] = (
            Partner.default_get(["autopost_bills"]).get("autopost_bills") or "never"
        )
    bot = env["res.users"].with_context(no_reset_password=True).create(vals)
    imd.create(
        {
            "module": "dz23_whatsapp",
            "name": "user_dz23_bot",
            "model": "res.users",
            "res_id": bot.id,
            "noupdate": True,
        }
    )
    return bot


class DZ23Channel(models.Model):
    _name = "dz23.channel"
    _description = "DZ23 — Canal de mensageria (multi-tenant, por empresa)"
    _order = "company_id, name"

    name = fields.Char(required=True)
    active = fields.Boolean(default=True)
    company_id = fields.Many2one(
        "res.company",
        required=True,
        index=True,
        default=lambda self: self.env.company,
        help="Empresa dona deste canal. Todo lead/evento/pedido nasce nela.",
    )
    provider = fields.Selection(
        [
            ("evolution", "Evolution API"),
            ("meta_cloud", "Meta WhatsApp Cloud"),
            ("twilio", "Twilio"),
        ],
        required=True,
        default="evolution",
    )
    # Identificador do canal no provedor (instance/phone_id/from), único por
    # provedor. Base para resolver o canal e validar o payload.
    provider_channel_id = fields.Char(
        compute="_compute_provider_channel_id",
        store=True,
        index=True,
        string="ID do canal no provedor",
    )
    # Token opaco que identifica o canal na URL do webhook (não é segredo de
    # autenticação — a auth é por apikey/app_secret do canal — mas evita expor
    # instância/empresa e permite rotear sem varredura global).
    # groups=group_system: segredos legíveis só por admin (não por group_user
    # comum via ORM/XML-RPC). O envio/webhook lê via sudo (MED-1).
    webhook_token = fields.Char(
        required=True,
        copy=False,
        index=True,
        readonly=True,
        groups="base.group_system",
        default=lambda self: secrets.token_urlsafe(24),
    )
    # Segredo INDEPENDENTE da chave administrativa do provedor, usado só para
    # autenticar o callback (header X-DZ23-Callback). Rotacionável por canal.
    callback_secret = fields.Char(
        required=True,
        copy=False,
        readonly=True,
        groups="base.group_system",
        default=lambda self: secrets.token_urlsafe(32),
    )

    # Evolution
    evo_base = fields.Char("Evolution base URL")
    evo_instance = fields.Char("Evolution instance")
    evo_apikey = fields.Char("Evolution apikey", groups="base.group_system")
    # Meta Cloud
    meta_phone_id = fields.Char("Meta phone_number_id")
    meta_token = fields.Char("Meta token", groups="base.group_system")
    meta_api_version = fields.Char("Meta API version", default="v20.0")
    meta_app_secret = fields.Char("Meta App Secret", groups="base.group_system")
    meta_verify_token = fields.Char("Meta verify token", groups="base.group_system")
    # Twilio
    twilio_sid = fields.Char("Twilio SID")
    twilio_token = fields.Char("Twilio token", groups="base.group_system")
    twilio_from = fields.Char("Twilio from")

    # Saúde do canal (atualizada pelos webhooks)
    connection_state = fields.Char(
        "Estado da conexão", readonly=True, help="Último estado informado pelo provedor."
    )
    connection_state_at = fields.Datetime("Estado atualizado em", readonly=True)
    last_webhook_at = fields.Datetime("Último webhook recebido", readonly=True)
    rate_limited_until = fields.Datetime(
        "Envio pausado até",
        readonly=True,
        help="Preenchido quando o provedor responde 429: a outbox não envia por este "
        "canal até esse horário (respeita Retry-After).",
    )
    public_webhook_url = fields.Char(
        "URL do webhook (provedor)",
        compute="_compute_public_webhook_url",
        groups="base.group_system",
        help="Configure esta URL no provedor. Contém o token opaco do canal.",
    )

    # Agente — o valor é POR CANAL; os Ajustes globais só definem o PADRÃO
    # aplicado a canais NOVOS (evita o controle enganoso apontado no QA).
    agent_autoreply = fields.Boolean(
        "Auto-resposta do agente", default=lambda self: self._default_agent_autoreply()
    )
    agent_prompt = fields.Text(
        "Personalidade/instruções do agente",
        default=lambda self: (
            self.env["ir.config_parameter"].sudo().get_param("dz23.agent.prompt", "")
        ),
    )

    @api.model
    def _default_agent_autoreply(self):
        val = self.env["ir.config_parameter"].sudo().get_param("dz23.agent.autoreply", "1")
        return (val or "1") not in ("0", "False", "false", "")

    _webhook_token_uniq = models.Constraint("unique(webhook_token)", "Token de webhook duplicado.")
    _provider_channel_uniq = models.Constraint(
        "unique(provider, provider_channel_id)",
        "Já existe um canal com esse provedor e identificador.",
    )

    @api.depends("provider", "webhook_token")
    def _compute_public_webhook_url(self):
        kinds = {"evolution": "evolution", "meta_cloud": "meta", "twilio": "twilio"}
        for ch in self:
            ch.public_webhook_url = (
                ch._public_webhook_url(kinds[ch.provider]) if ch.webhook_token else False
            )

    @api.depends("provider", "evo_instance", "meta_phone_id", "twilio_from")
    def _compute_provider_channel_id(self):
        for ch in self:
            ch.provider_channel_id = {
                "evolution": ch.evo_instance,
                "meta_cloud": ch.meta_phone_id,
                "twilio": ch.twilio_from,
            }.get(ch.provider) or False

    # ---- usuário técnico (para sair do sudo no processamento) -------------
    def _bot_user(self):
        """Usuário técnico; criado sob demanda em runtime (quando o account já
        está carregado), nunca em tempo de load do módulo."""
        user = self.env.ref("dz23_whatsapp.user_dz23_bot", raise_if_not_found=False)
        if not user:
            user = ensure_bot_user(self.env)
        return user

    def _ensure_bot_in_company(self):
        """Garante que o usuário técnico existe e pertence à empresa do canal."""
        bot = self.sudo()._bot_user()
        if not bot:
            return
        for ch in self:
            if ch.company_id and ch.company_id.id not in bot.company_ids.ids:
                bot.write({"company_ids": [(4, ch.company_id.id)]})

    def _processing_self(self):
        """Retorna o canal para processar inbound: usuário TÉCNICO (não sudo),
        no escopo estrito da empresa do canal, sujeito às record rules."""
        self.ensure_one()
        self.sudo()._ensure_bot_in_company()
        bot = self.sudo()._bot_user()
        company = self.sudo().company_id
        if bot:
            return (
                self.with_user(bot)
                .with_company(company)
                .with_context(allowed_company_ids=[company.id])
            )
        # fallback (sem bot): escopo por empresa via sudo
        return self.sudo()._scoped()

    @api.constrains("provider", "evo_instance", "company_id")
    def _check_evo_instance_unique(self):
        for ch in self:
            if ch.provider == "evolution" and ch.evo_instance:
                dup = self.search(
                    [
                        ("id", "!=", ch.id),
                        ("provider", "=", "evolution"),
                        ("evo_instance", "=", ch.evo_instance),
                    ],
                    limit=1,
                )
                if dup:
                    raise ValidationError(
                        _("A instância Evolution '%s' já está em uso por outro canal.")
                        % ch.evo_instance
                    )

    # ---------- resolução (usada pelo webhook) ----------
    @api.model
    def _resolve_by_token(self, token):
        """Acha o canal pelo token opaco da URL. Sudo interno; escopo aplicado depois."""
        if not token:
            return self.browse()
        return self.sudo().search([("webhook_token", "=", token)], limit=1)

    def _scoped(self):
        """Retorna self no contexto da empresa do canal (isola dados por tenant)."""
        self.ensure_one()
        return self.with_company(self.company_id).with_context(
            allowed_company_ids=[self.company_id.id]
        )

    # ---------- ingestão de webhooks normalizados (ADR-007) ----------
    def _ingest_events(self, events):
        """Roteia eventos do contrato interno: mensagem recebida -> inbox;
        status e mensagens enviadas (fromMe) -> dz23.message.event; conexão ->
        estado do canal. Evento fora do contrato é descartado com log sem PII.
        Erros de persistência PROPAGAM (o webhook responde 500)."""
        self.ensure_one()
        channel = self.sudo()
        Inbox = self.env["dz23.message.inbox"].sudo()
        Event = self.env["dz23.message.event"].sudo()
        stats = {"inbox": 0, "events": 0, "ignored": 0, "invalid": 0}
        for event in events or []:
            try:
                validate_event(event)
            except ValueError as e:
                stats["invalid"] += 1
                _logger.warning("Evento descartado (contrato) canal=%s motivo=%s", channel.id, e)
                continue
            if event["kind"] == "connection":
                channel._apply_connection_state(event)
                stats["events"] += 1
            elif event["kind"] == "message" and event["direction"] == "inbound":
                if event["message_type"] == "reaction":
                    stats["ignored"] += 1
                    continue
                Inbox._enqueue_event(channel, event)
                stats["inbox"] += 1
            else:
                status = event["status"]
                if event["kind"] == "message" and status == "unknown":
                    status = "sent"  # mensagem enviada por nós/pelo aparelho
                Event._record(channel, dict(event, status=status))
                stats["events"] += 1
        channel._touch_webhook()
        return stats

    def _touch_webhook(self):
        """Marca o último webhook (no máximo 1 escrita/min por canal: evita
        contenção de linha sob rajada de callbacks)."""
        self.env.cr.execute(
            """
            UPDATE dz23_channel
               SET last_webhook_at = (now() AT TIME ZONE 'utc')
             WHERE id = %s
               AND (last_webhook_at IS NULL
                    OR last_webhook_at < (now() AT TIME ZONE 'utc') - interval '60 seconds')
            """,
            (self.id,),
        )
        self.invalidate_recordset(["last_webhook_at"])

    def _apply_connection_state(self, event):
        occurred = event.get("occurred_at") or fields.Datetime.now()
        if self.connection_state_at and occurred < self.connection_state_at:
            return  # estado atrasado não sobrescreve o mais recente
        self.write({"connection_state": event["state"], "connection_state_at": occurred})
        _logger.info("Canal %s: conexão %s", self.id, event["state"])

    # ---------- Twilio: validação do callback ----------
    def _twilio_signature_urls(self, request_url):
        """URLs candidatas para a assinatura: a pública configurada (atrás de
        proxy) e a vista pelo servidor. A assinatura continua exigindo o token."""
        self.ensure_one()
        urls = [self._public_webhook_url("twilio")]
        if request_url and request_url not in urls:
            urls.append(request_url)
        return urls

    def _twilio_payload_matches(self, params, events):
        """O callback precisa ser da conta e do número deste canal."""
        self.ensure_one()

        def first(name):
            value = params.get(name)
            return (value[0] if value else None) if isinstance(value, list) else value

        account = first("AccountSid")
        if account and account != self.twilio_sid:
            return False
        ours = _digits(self.twilio_from)
        for event in events:
            number = event.get("recipient") if event["kind"] == "message" else None
            if event["kind"] == "status":
                number = _digits(str(first("From") or "").replace("whatsapp:", ""))
            if ours and number and number != ours:
                return False
        return True

    # ---------- envio ----------
    def send_text(self, number, body, correlation_id=None):
        self.ensure_one()
        to = _e164_br(number)
        if not to:
            raise ProviderPermanentError(_("Número de WhatsApp inválido."))
        if not body:
            raise ProviderPermanentError(_("Mensagem vazia."))
        # Credenciais do canal são restritas a admin (groups=group_system); o
        # envio roda como usuário técnico (bot) — por isso lê via sudo() aqui.
        channel = self.sudo()
        return {
            "evolution": channel._send_evolution,
            "meta_cloud": channel._send_meta_cloud,
            "twilio": channel._send_twilio,
        }[channel.provider](to, body, correlation_id=correlation_id)

    def _post(self, url, **kwargs):
        """POST ao provedor. Levanta ProviderTransientError/ProviderPermanentError
        (ADR-008); loga só canal, status e código (nunca corpo/URL com dados)."""
        try:
            resp = requests.post(url, timeout=_TIMEOUT, **kwargs)
        except requests.exceptions.RequestException as e:
            _logger.warning(
                "DZ23 WhatsApp erro de rede canal=%s tipo=%s", self.id, type(e).__name__
            )
            raise ProviderTransientError(
                _("Falha de rede ao enviar WhatsApp (%s).") % type(e).__name__
            ) from None
        if resp.status_code >= 400:
            try:
                data = resp.json()
            except ValueError:
                data = {}
            error = classify_http_error(resp.status_code, resp.headers, data)
            _logger.info(
                "DZ23 WhatsApp canal=%s HTTP %s código=%s permanente=%s",
                self.id,
                resp.status_code,
                error.code,
                error.permanent,
            )
            raise error
        try:
            return resp.json()
        except ValueError:
            return {"raw": resp.text}

    def _send_evolution(self, to, body, correlation_id=None):
        if not (self.evo_base and self.evo_instance and self.evo_apikey):
            raise ProviderPermanentError(_("Canal Evolution incompleto (base/instância/apikey)."))
        url = "%s/message/sendText/%s" % (self.evo_base.rstrip("/"), self.evo_instance)
        data = self._post(
            url, headers={"apikey": self.evo_apikey}, json={"number": to, "text": body}
        )
        # Sucesso REAL exige id de mensagem no corpo — 2xx sem id é falha do
        # provedor e deve reprocessar (não marcar "sent" e perder a resposta).
        if not (data.get("key") or {}).get("id"):
            raise ProviderTransientError(_("Evolution não confirmou o envio (sem id de mensagem)."))
        return data

    def _send_meta_cloud(self, to, body, correlation_id=None):
        if not (self.meta_token and self.meta_phone_id):
            raise ProviderPermanentError(_("Canal Meta incompleto (token/phone_id)."))
        url = "https://graph.facebook.com/%s/%s/messages" % (
            self.meta_api_version or "v20.0",
            self.meta_phone_id,
        )
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": body},
        }
        if correlation_id:
            # Devolvido pela Meta nos callbacks de status (correlação).
            payload["biz_opaque_callback_data"] = correlation_id
        data = self._post(
            url,
            headers={"Authorization": "Bearer %s" % self.meta_token},
            json=payload,
        )
        if not (data.get("messages") or [{}])[0].get("id"):
            raise ProviderTransientError(_("Meta não confirmou o envio (sem id de mensagem)."))
        return data

    def _send_twilio(self, to, body, correlation_id=None):
        if not (self.twilio_sid and self.twilio_token and self.twilio_from):
            raise ProviderPermanentError(_("Canal Twilio incompleto (sid/token/from)."))
        url = "https://api.twilio.com/2010-04-01/Accounts/%s/Messages.json" % self.twilio_sid
        frm = (
            self.twilio_from
            if self.twilio_from.startswith("whatsapp:")
            else "whatsapp:%s" % self.twilio_from
        )
        data = self._post(
            url,
            data={
                "From": frm,
                "To": "whatsapp:+%s" % to,
                "Body": body,
                # Status (sent/delivered/read/failed/undelivered) volta assinado
                # para o webhook tokenizado deste canal.
                "StatusCallback": self._public_webhook_url("twilio"),
            },
            auth=(self.twilio_sid, self.twilio_token),
        )
        if not data.get("sid"):
            raise ProviderTransientError(_("Twilio não confirmou o envio (sem SID)."))
        return data

    # ---------- inbound (base: só registra; dz23_agent sobrescreve) ----------
    def handle_inbound(self, number, text, raw=None, message=None):
        """Processa uma mensagem recebida NO ESCOPO da empresa do canal.
        `message`: metadados normalizados (message_type, caption, reply_to, media).
        Base apenas registra (sem PII no log); o dz23_agent sobrescreve."""
        self.ensure_one()
        _logger.info(
            "[canal %s] inbound tipo=%s de ...%s",
            self.id,
            (message or {}).get("message_type") or "text",
            _digits(number)[-4:],
        )
        return False

    # ---------- Evolution: provisionamento por canal ----------
    def _evo_req(self, method, path, **kwargs):
        self.ensure_one()
        if not (self.evo_base and self.evo_apikey):
            raise UserError(_("Configure a base URL e a apikey da Evolution no canal."))
        url = "%s%s" % (self.evo_base.rstrip("/"), path)
        try:
            resp = requests.request(
                method,
                url,
                timeout=_TIMEOUT,
                headers={"apikey": self.evo_apikey, "Content-Type": "application/json"},
                **kwargs,
            )
        except requests.exceptions.RequestException as e:
            raise UserError(_("Não foi possível falar com o servidor Evolution: %s") % e) from e
        try:
            return resp.json()
        except ValueError:
            return {"raw": resp.text}

    def _webhook_url(self, kind):
        # Base ALCANÇÁVEL pelo provedor (rede interna de containers), distinta da
        # web.base.url pública. Configurável em dz23.whatsapp.webhook_base.
        ICP = self.env["ir.config_parameter"].sudo()
        base = (
            ICP.get_param("dz23.whatsapp.webhook_base")
            or ICP.get_param("web.base.url")
            or "http://localhost:8069"
        ).rstrip("/")
        return "%s/dz23/whatsapp/%s/webhook/%s" % (base, kind, self.sudo().webhook_token)

    def _public_webhook_url(self, kind):
        # URL PÚBLICA (internet) usada por Meta/Twilio e na assinatura Twilio.
        # Configurável em dz23.whatsapp.public_base_url (atrás de proxy/HTTPS).
        ICP = self.env["ir.config_parameter"].sudo()
        base = (
            ICP.get_param("dz23.whatsapp.public_base_url")
            or ICP.get_param("web.base.url")
            or "http://localhost:8069"
        ).rstrip("/")
        return "%s/dz23/whatsapp/%s/webhook/%s" % (base, kind, self.sudo().webhook_token)

    def _evolution_webhook_config(self):
        return {
            "webhook": {
                "enabled": True,
                "url": self._webhook_url("evolution"),
                "webhookByEvents": False,
                "events": ["MESSAGES_UPSERT", "MESSAGES_UPDATE", "CONNECTION_UPDATE"],
                "headers": {
                    "X-DZ23-Callback": self.callback_secret,
                    "Content-Type": "application/json",
                },
            }
        }

    def action_evolution_connect(self):
        """Botão do canal: abre o assistente de QR JÁ VINCULADO A ESTE canal
        (corrige HIGH-1: antes retornava um dict cru e nunca exibia o QR; e o
        caminho de Ajustes conectava o canal padrão, não o que está aberto)."""
        self.ensure_one()
        wiz = self.env["dz23.whatsapp.evolution"].create({"channel_id": self.id})
        wiz._load_qr()
        return {
            "type": "ir.actions.act_window",
            "name": _("Conectar WhatsApp (Evolution)"),
            "res_model": "dz23.whatsapp.evolution",
            "res_id": wiz.id,
            "view_mode": "form",
            "target": "new",
        }

    def _evolution_provision(self):
        """Cria a instância (se preciso), configura o webhook tokenizado e devolve o QR."""
        self.ensure_one()
        if not self.evo_instance:
            self.evo_instance = "dz23_%s_%s" % (self.company_id.id, self.id)
        self._evo_req(
            "POST",
            "/instance/create",
            json={
                "instanceName": self.evo_instance,
                "integration": "WHATSAPP-BAILEYS",
                "qrcode": True,
            },
        )
        # webhook tokenizado + SEGREDO DE CALLBACK próprio no header (independente
        # da chave administrativa). O endpoint é fail-closed e valida esse header.
        self._evo_req(
            "POST", "/webhook/set/%s" % self.evo_instance, json=self._evolution_webhook_config()
        )
        data = self._evo_req("GET", "/instance/connect/%s" % self.evo_instance)
        qr = data.get("base64") or (data.get("qrcode") or {}).get("base64") or ""
        return {"instance": self.evo_instance, "qr": qr, "webhook": self._webhook_url("evolution")}

    def action_rotate_callback_secret(self):
        """Gera um novo segredo de callback e reconfigura o webhook do provedor."""
        self.ensure_one()
        self.callback_secret = secrets.token_urlsafe(32)
        if self.provider == "evolution" and self.evo_instance:
            self._evo_req(
                "POST",
                "/webhook/set/%s" % self.evo_instance,
                json=self._evolution_webhook_config(),
            )
        return True
