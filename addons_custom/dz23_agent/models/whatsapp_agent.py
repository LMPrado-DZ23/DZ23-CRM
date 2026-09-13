# Cérebro do atendente/vendedor, agora POR CANAL e no ESCOPO da empresa do canal
# (multi-tenant). Estende dz23.channel e sobrescreve handle_inbound:
#   - agenda (cria evento -> Google Calendar),
#   - vende (abre orçamento na empresa do canal),
#   - ou responde com IA usando o catálogo/persona daquela empresa.
# A busca de lead é ESCOPADA por empresa (nunca global por cauda de telefone).
import logging
import re
from datetime import timedelta

from odoo import fields, models
from odoo.addons.dz23_whatsapp.models.whatsapp_channel import _digits, _e164_br
from odoo.tools.translate import _

_logger = logging.getLogger(__name__)

_DEFAULT_PROMPT = (
    "Você é a Sofia, assistente virtual da equipe de atendimento, conversando pelo "
    "WhatsApp. Seja calorosa, simpática, natural e direta, em português do Brasil, "
    "com no máximo 2 ou 3 frases curtas por mensagem, até 1 emoji quando combinar. "
    "Se perguntarem, assuma com naturalidade que é uma assistente virtual do time — "
    "não finja ser humana, mas também não seja robótica. Seu papel é entender a "
    "necessidade, apresentar produtos/serviços e preços com base APENAS no catálogo "
    "abaixo, tirar dúvidas e conduzir para fechar a venda ou marcar horário. Nunca "
    "invente itens, preços ou disponibilidade que não estejam no catálogo; se não "
    "souber, diga que vai confirmar e faça uma pergunta. Atenda qualquer ramo (salão, "
    "loja, clínica, oficina, serviços) usando sempre o contexto da empresa abaixo."
)
# Intenção de AGENDAR (marcar horário).
_SCHED_RE = re.compile(r"agend|marc|hor[aá]rio|reuni|consulta|atend", re.IGNORECASE)
# PERGUNTA de preço (NÃO cria pedido).
_PRICE_RE = re.compile(r"pre[çc]o|valor|quanto\s+custa|quanto\s+[ée]|tabela", re.IGNORECASE)
# CONFIRMAÇÃO de compra (cria orçamento) — exige intenção explícita.
_BUY_RE = re.compile(
    r"quero\s+comprar|vou\s+(?:comprar|levar|querer)|pode\s+fechar|fecha[r]?\b|"
    r"confirm|fazer\s+o\s+pedido|quero\s+fechar|bora\s+fechar|adquirir|contratar",
    re.IGNORECASE,
)
# Palavras de hora explícita (para não assumir 09:00 silenciosamente).
_TIME_RE = re.compile(r"\b(\d{1,2})(?:[:hHed]\s?(\d{2}))?\s*(?:h|hs|horas?|:00)?\b")


def _strip_html(v):
    return re.sub(r"<[^>]+>", " ", v or "").strip()


def _norm(s):
    """Normaliza acentos/caixa para casamento robusto de produtos."""
    import unicodedata

    s = unicodedata.normalize("NFKD", (s or "").lower())
    return "".join(c for c in s if not unicodedata.combining(c))


class DZ23ChannelAgent(models.Model):
    _inherit = "dz23.channel"

    # ---- lead via IDENTIDADE do canal (nunca busca global por telefone) ----
    def _agent_find_lead(self, number):
        self.ensure_one()
        e164 = _e164_br(number) or _digits(number)
        company = self.company_id
        # 1) resolve a identidade (channel_id, provider_user_id) — escopo do canal
        contact = self.env["dz23.channel.contact"]._get_or_create(self, e164, e164)
        if contact.lead_id:
            return contact.lead_id
        lead = self.env["crm.lead"].create(
            {
                "name": _("WhatsApp %s") % number,
                "phone": e164 or number,
                "type": "lead",
                "company_id": company.id,
                "partner_id": contact.partner_id.id if contact.partner_id else False,
            }
        )
        contact.lead_id = lead.id
        return lead

    def _agent_partner_for(self, lead):
        if lead.partner_id:
            return lead.partner_id
        partner = self.env["res.partner"].create(
            {
                "name": lead.contact_name or lead.name or _("Cliente WhatsApp"),
                "phone": lead.phone or "",
                "company_id": self.company_id.id,
            }
        )
        lead.partner_id = partner.id
        return partner

    # ---- contexto do negócio (escopado por empresa) -----------------------
    def _agent_catalog(self, limit=40):
        return self.env["product.template"].search(
            [("sale_ok", "=", True), ("company_id", "in", (False, self.company_id.id))],
            order="list_price desc",
            limit=limit,
        )

    def _agent_business_context(self):
        company = self.company_id
        parts = [_("Empresa: %s.") % (company.name or "DZ23 CRM")]
        currency = company.currency_id.symbol or "R$"
        prods = self._agent_catalog()
        if prods:
            cat = ["- %s: %s %.2f" % (p.name, currency, p.list_price or 0.0) for p in prods]
            parts.append(_("Catálogo de produtos/serviços à venda:\n%s") % "\n".join(cat))
        else:
            parts.append(
                _(
                    "Ainda não há produtos cadastrados; faça o atendimento, entenda a "
                    "necessidade do cliente e colete os dados do interesse."
                )
            )
        return "\n".join(parts)

    def _agent_history(self, lead, limit=6):
        msgs = self.env["mail.message"].search(
            [("model", "=", lead._name), ("res_id", "=", lead.id)], order="id desc", limit=limit
        )
        hist = [b for b in (_strip_html(m.body) for m in reversed(msgs)) if b]
        return "\n".join(hist[-limit:])

    def _agent_system_prompt(self, lead):
        base = self.agent_prompt or _DEFAULT_PROMPT
        blocks = [base, self._agent_business_context()]
        # Histórico (chatter = notas internas) só vai para IA LOCAL (on-prem).
        # Provedor externo NÃO recebe notas internas (privacidade/LGPD).
        provider = (
            self.env["ir.config_parameter"].sudo().get_param("dz23.ai_provider", "ollama")
            or "ollama"
        )
        if provider == "ollama":
            history = self._agent_history(lead)
            if history:
                blocks.append(_("Histórico recente da conversa:\n%s") % history)
        return "\n\n".join(blocks)

    # ---- agenda: parsing DETERMINÍSTICO (não assume hora) -----------------
    def _agent_company_tz(self):
        import pytz

        name = self.company_id.partner_id.tz or self.env.user.tz or "America/Sao_Paulo"
        try:
            return pytz.timezone(name)
        except Exception:  # noqa: BLE001
            return pytz.timezone("America/Sao_Paulo")

    @staticmethod
    def _parse_time_tuple(text):
        """Extrai (hora, minuto) SÓ se houver hora explícita; senão None."""
        m = re.search(r"\b(\d{1,2})[:h](\d{2})\b", text or "")
        if m:
            return int(m.group(1)), int(m.group(2))
        m = re.search(r"\b(\d{1,2})\s*h\b", text or "") or re.search(
            r"[àa]s\s*(\d{1,2})\b", text or ""
        )
        if m:
            return int(m.group(1)), 0
        return None

    def _agent_parse_when(self, text):
        """Retorna dict: has_date, valid, date, time(ou None). Nunca assume 09:00."""
        import datetime as dt

        today = fields.Date.context_today(self)
        t = _norm(text)
        date = None
        md = re.search(r"(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?", text or "")
        if md:
            day, month = int(md.group(1)), int(md.group(2))
            year = int(md.group(3)) if md.group(3) else today.year
            if year < 100:
                year += 2000
            try:
                date = dt.date(year, month, day)
            except ValueError:
                return {"has_date": True, "valid": False}
        elif "depois de amanha" in t:
            date = today + dt.timedelta(days=2)
        elif "amanha" in t:
            date = today + dt.timedelta(days=1)
        elif "hoje" in t:
            date = today
        if not date:
            return {"has_date": False}
        return {"has_date": True, "valid": True, "date": date, "time": self._parse_time_tuple(text)}

    def _agent_slot_conflict(self, start_utc, minutes=60):
        """True se já existe evento sobrepondo o intervalo (evita double-booking).
        MULTI-TENANT: calendar.event no Odoo 19 CE não tem company_id, então
        escopamos pelo company_id da oportunidade ligada ao evento
        (opportunity_id.company_id) — todo evento do agente carrega o lead da
        empresa do canal. Assim um horário do tenant B não bloqueia o tenant A."""
        stop = start_utc + timedelta(minutes=minutes)
        return bool(
            self.env["calendar.event"].search(
                [
                    ("start", "<", fields.Datetime.to_string(stop)),
                    ("stop", ">", fields.Datetime.to_string(start_utc)),
                    ("opportunity_id.company_id", "=", self.company_id.id),
                ],
                limit=1,
            )
        )

    def _agent_local_to_utc(self, date, hour, minute):
        import datetime as dt

        import pytz

        aware = self._agent_company_tz().localize(dt.datetime.combine(date, dt.time(hour, minute)))
        return aware.astimezone(pytz.utc).replace(tzinfo=None), aware

    # ---- venda: casamento DETERMINÍSTICO (acentos + limite de palavra) -----
    def _agent_match_products(self, text):
        t = _norm(text)
        out = []
        for p in self._agent_catalog():
            name = _norm(p.name)
            if len(name) < 3:
                continue
            if len(name) <= 4:
                hit = bool(re.search(r"\b%s\b" % re.escape(name), t))
            else:
                hit = name in t
            if hit:
                out.append(p)
        return out

    # ---- ações (na empresa do canal) -------------------------------------
    def _agent_create_event(self, lead, start_utc):
        self.env["calendar.event"].create(
            {
                "name": _("Agendamento WhatsApp — %s") % (lead.contact_name or lead.name),
                "start": fields.Datetime.to_string(start_utc),
                "stop": fields.Datetime.to_string(start_utc + timedelta(hours=1)),
                "partner_ids": [(4, lead.partner_id.id)] if lead.partner_id else [],
                "opportunity_id": lead.id if lead._name == "crm.lead" else False,
            }
        )

    def _agent_create_quote(self, lead, product):
        if "sale.order" not in self.env or not product.sale_ok:
            return False
        partner = self._agent_partner_for(lead)
        so = self.env["sale.order"].create(
            {
                "partner_id": partner.id,
                "company_id": self.company_id.id,
                "origin": "WhatsApp DZ23",
                "order_line": [
                    (0, 0, {"product_id": product.product_variant_id.id, "product_uom_qty": 1.0})
                ],
            }
        )
        lead.message_post(body=_("🛒 Orçamento %s aberto: %s") % (so.name, product.name))
        return so

    def _agent_reply_ai(self, lead, text, fallback):
        try:
            return self.env["dz23.ai"].chat(text, system=self._agent_system_prompt(lead))
        except Exception as e:  # noqa: BLE001 - IA pode não estar configurada
            _logger.info("Agente: IA indisponível (%s), usando padrão.", type(e).__name__)
            return fallback

    def _cur(self):
        return self.company_id.currency_id.symbol or "R$"

    # ---- pipeline principal (determinístico; LLM só conversa) -------------
    def handle_inbound(self, number, text, raw=None, message=None):
        self.ensure_one()
        if not self.agent_autoreply:
            return super().handle_inbound(number, text, raw, message=message)
        mtype = (message or {}).get("message_type") or "text"
        lead = self._agent_find_lead(number)
        if mtype not in ("text", "interactive"):
            return self._handle_non_text(lead, number, message or {})
        lead.message_post(body=_("📩 WhatsApp recebido de %s: %s") % (number, text))
        txt = text or ""

        # 1) AGENDA — só cria com data E hora explícitas, futuro e sem conflito.
        if _SCHED_RE.search(txt):
            reply = self._handle_schedule(lead, txt)
        # 2) COMPRA — só com confirmação explícita e produto não-ambíguo.
        elif _BUY_RE.search(txt):
            reply = self._handle_buy(lead, txt)
        # 3) PREÇO — informa, NUNCA cria pedido.
        elif _PRICE_RE.search(txt):
            reply = self._handle_price(lead, txt)
        # 4) Conversa geral via IA.
        else:
            reply = self._agent_reply_ai(
                lead,
                txt,
                _("Oi! Já vi sua mensagem 😊 Me conta o que você precisa que eu te ajudo."),
            )

        # Entrega DURÁVEL (HIGH-01): o efeito de negócio já foi aplicado uma vez;
        # a resposta é ENFILEIRADA na outbox e enviada por um worker com retry +
        # DLQ. Assim, se o provedor cair no instante do envio, a resposta não é
        # perdida — é reenviada, sem duplicar o efeito.
        if reply:
            self.env["dz23.message.outbox"].sudo()._enqueue(self, number, reply)
            lead.message_post(body=_("🤖 Resposta enfileirada para envio: %s") % reply)
        return True

    def _handle_non_text(self, lead, number, message):
        """Mídia/localização/contato: registra no lead (nunca descarta em silêncio)
        e confirma o recebimento. Não envia nada à IA (imagem pode conter documento)."""
        labels = {
            "image": _("imagem"),
            "audio": _("áudio"),
            "video": _("vídeo"),
            "document": _("documento"),
            "location": _("localização"),
            "contact": _("contato"),
            "sticker": _("figurinha"),
        }
        mtype = message.get("message_type")
        label = labels.get(mtype, _("mensagem"))
        caption = message.get("caption")
        lead.message_post(
            body=_("📎 WhatsApp recebido de %(number)s: %(kind)s%(caption)s")
            % {
                "number": number,
                "kind": label,
                "caption": (" — %s" % caption) if caption else "",
            }
        )
        if mtype == "sticker":
            return True
        reply = _("Recebi aqui (%s) 😊 Já vou verificar e te respondo por aqui.") % label
        self.env["dz23.message.outbox"].sudo()._enqueue(self, number, reply)
        return True

    def _handle_schedule(self, lead, txt):
        w = self._agent_parse_when(txt)
        if not w.get("has_date"):
            return _("Claro! Para qual dia você gostaria de marcar? 😊")
        if not w.get("valid"):
            return _("Não consegui entender essa data. Pode confirmar o dia (ex.: 15/09)?")
        if not w.get("time"):
            return _("Perfeito, dia %s! Qual horário fica melhor pra você?") % (
                w["date"].strftime("%d/%m")
            )
        hour, minute = w["time"]
        try:
            start_utc, aware = self._agent_local_to_utc(w["date"], hour, minute)
        except ValueError:
            return _("Esse horário não parece válido. Pode confirmar dia e hora?")
        if start_utc <= fields.Datetime.now():
            return _("Esse horário já passou 😅 Me passa uma data e hora futuras?")
        if self._agent_slot_conflict(start_utc):
            return _("Esse horário já está reservado 🙈 Quer tentar outro horário?")
        self._agent_create_event(lead, start_utc)
        lead.message_post(
            body=_("📅 Evento criado para %s (sincroniza com Google Calendar)")
            % aware.strftime("%d/%m/%Y %H:%M")
        )
        return _("Prontinho! Agendei para %s. Se precisar remarcar, é só falar 💙") % (
            aware.strftime("%d/%m/%Y às %H:%M")
        )

    def _handle_buy(self, lead, txt):
        matches = self._agent_match_products(txt)
        if len(matches) == 1:
            p = matches[0]
            so = self._agent_create_quote(lead, p)
            if so:
                return _(
                    "Boa escolha! Preparei seu orçamento de %s (%s %.2f). "
                    "Posso seguir com a confirmação do pedido? 💙"
                ) % (p.name, self._cur(), p.list_price or 0.0)
            return _("Consigo te ajudar com %s — me confirma que já registro.") % p.name
        if len(matches) > 1:
            nomes = ", ".join(m.name for m in matches[:5])
            return _("Temos algumas opções: %s. Qual delas você quer? 😊") % nomes
        return self._agent_reply_ai(
            lead, txt, _("Me diz qual produto ou serviço você quer que eu já organizo pra você.")
        )

    def _handle_price(self, lead, txt):
        matches = self._agent_match_products(txt)
        if len(matches) == 1:
            p = matches[0]
            return _("O %s fica %s %.2f. Quer que eu já reserve pra você? 😊") % (
                p.name,
                self._cur(),
                p.list_price or 0.0,
            )
        if len(matches) > 1:
            nomes = ", ".join(
                "%s (%s %.2f)" % (m.name, self._cur(), m.list_price or 0.0) for m in matches[:5]
            )
            return _("Temos: %s. Sobre qual quer saber? ") % nomes
        return self._agent_reply_ai(
            lead, txt, _("Me diz qual item você quer saber o preço que eu te falo certinho.")
        )
