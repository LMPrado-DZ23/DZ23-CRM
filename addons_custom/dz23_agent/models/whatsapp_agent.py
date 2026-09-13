# Cérebro do atendente/vendedor, POR CANAL e no ESCOPO da empresa do canal
# (multi-tenant). Estende dz23.channel e sobrescreve handle_inbound. Regras de
# negócio são DETERMINÍSTICAS; a IA só conversa (nunca decide preço, estoque,
# desconto, pedido ou horário):
#   - compra em etapas: produto -> variação/quantidade -> resumo com preço da
#     lista de preços do Odoo -> confirmação explícita -> orçamento idempotente;
#   - agenda sem dupla reserva: advisory lock + conflito com toda a agenda do
#     responsável + expediente/feriados + fuso + duração/intervalo; cancelar/remarcar.
# (ADR-004 e ADR-005)
import datetime as dt
import hashlib
import logging
import re
import unicodedata
import uuid
from datetime import timedelta

import pytz
from odoo import fields, models
from odoo.addons.dz23_whatsapp.models.whatsapp_channel import _digits, _e164_br
from odoo.fields import Domain
from odoo.tools.translate import _

_logger = logging.getLogger(__name__)

_ORIGIN = "WhatsApp DZ23"
_PENDING_MINUTES = 30
_PROMPT_VERSION = "sofia-2026-09-13-gov1"
_FREE_TEXT = object()  # sentinela: mensagem sem intenção determinística

_DEFAULT_PROMPT = (
    "Você é a Sofia, assistente virtual da equipe de atendimento, conversando pelo "
    "WhatsApp. Seja calorosa, simpática, natural e direta, em português do Brasil, "
    "com no máximo 2 ou 3 frases curtas por mensagem, até 1 emoji quando combinar. "
    "Se perguntarem, assuma com naturalidade que é uma assistente virtual do time — "
    "não finja ser humana, mas também não seja robótica. Seu papel é entender a "
    "necessidade, apresentar produtos/serviços com base APENAS no catálogo abaixo, "
    "tirar dúvidas e conduzir para fechar a venda ou marcar horário. Nunca invente "
    "itens, preços, descontos ou disponibilidade; valores e confirmações são sempre "
    "enviados pelo sistema. Se não souber, diga que vai confirmar e faça uma pergunta. "
    "Atenda qualquer ramo (salão, loja, clínica, oficina, serviços)."
)
# Intenção de AGENDAR. Sem "atend"/"marc" soltos (casavam "atendimento", "marca").
_SCHED_RE = re.compile(
    r"\bagend|\bmarcar\b|\bmarca\s+(?:um|uma|pra|para)\b|hor[aá]rio|\breservar?\b|\bconsulta\b",
    re.IGNORECASE,
)
_CANCEL_SCHED_RE = re.compile(
    r"\bcancel\w*\s+(?:o\s+|a\s+|meu\s+|minha\s+)?"
    r"(?:agendamento|hor[aá]rio|consulta|atendimento|reserva)|\bdesmarc",
    re.IGNORECASE,
)
_RESCHED_RE = re.compile(
    r"\bremarc|\breagend|\b(?:mudar|trocar|alterar)\s+(?:o\s+|meu\s+)?hor[aá]rio",
    re.IGNORECASE,
)
# PERGUNTA de preço (NÃO cria pedido).
_PRICE_RE = re.compile(r"pre[çc]o|valor|quanto\s+custa|quanto\s+[ée]|tabela", re.IGNORECASE)
# Intenção de COMPRA (abre o resumo; o pedido só nasce na confirmação).
_BUY_RE = re.compile(
    r"quero\s+comprar|vou\s+(?:comprar|levar|querer)|quero\s+fechar|bora\s+fechar|"
    r"fazer\s+o\s+pedido|adquirir|contratar|quero\s+(?:o|a|um|uma|\d+)\s",
    re.IGNORECASE,
)
# Confirmação/negativa do resumo (texto normalizado, sem acento).
_YES_RE = re.compile(
    r"^\s*(?:sim|s|confirmo|confirmado|confirma|pode\s+(?:fechar|confirmar|ser|mandar|gerar)|"
    r"fechado|fechou|isso(?:\s+mesmo)?|ok|okay|beleza|bora|pode)\b"
)
_NO_RE = re.compile(r"^\s*(?:nao|n|cancela|cancelar|desisto|negativo|deixa\s+pra\s+la)\b")
# Opt-out: a mensagem inteira precisa ser o pedido (evita "quero sair do plano").
_OPT_OUT_RE = re.compile(
    r"^\s*(?:sair|parar|stop|descadastrar|cancelar\s+inscricao|"
    r"nao\s+quero\s+(?:mais\s+)?receber(?:\s+\w+)*)\s*[.!]*\s*$"
)
_NUMBER_WORDS = {"um": 1, "uma": 1, "dois": 2, "duas": 2, "tres": 3, "quatro": 4, "cinco": 5}


class _SlotUnavailableError(Exception):
    """Horário ocupado ou fora do expediente (desfaz a transação do efeito)."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _strip_html(v):
    return re.sub(r"<[^>]+>", " ", v or "").strip()


def _norm(s):
    """Normaliza acentos/caixa para casamento robusto."""
    s = unicodedata.normalize("NFKD", (s or "").lower())
    return "".join(c for c in s if not unicodedata.combining(c))


class DZ23ChannelAgent(models.Model):
    _inherit = "dz23.channel"

    # ---- configuração de agenda POR CANAL ----
    agenda_user_id = fields.Many2one(
        "res.users",
        string="Responsável pela agenda",
        help="Profissional cuja agenda (inclusive eventos manuais) bloqueia horários.",
    )
    agenda_calendar_id = fields.Many2one(
        "resource.calendar",
        string="Horário de atendimento",
        help="Expediente e feriados (ausências globais). Vazio: calendário da empresa.",
    )
    agenda_duration_minutes = fields.Integer("Duração do atendimento (min)", default=60)
    agenda_buffer_minutes = fields.Integer("Intervalo entre atendimentos (min)", default=0)

    # ---- identidade / lead (nunca busca global por telefone) ----
    def _agent_contact(self, number):
        self.ensure_one()
        e164 = _e164_br(number) or _digits(number)
        # Contatos são restritos ao grupo Atendente; o worker do robô usa sudo.
        contact = self.env["dz23.channel.contact"].sudo()._get_or_create(self, e164, e164)
        if not contact.lead_id:
            lead = self.env["crm.lead"].create(
                {
                    "name": _("WhatsApp %s") % number,
                    "phone": e164 or number,
                    "type": "lead",
                    "company_id": self.company_id.id,
                    "partner_id": contact.partner_id.id if contact.partner_id else False,
                }
            )
            contact.lead_id = lead.id
        return contact

    def _agent_find_lead(self, number):
        # O contato vem com sudo; o lead volta ao usuário do robô (sujeito às regras).
        return self._agent_contact(number).lead_id.with_env(self.env)

    def _agent_contact_for_lead(self, lead):
        return (
            self.env["dz23.channel.contact"]
            .sudo()
            .search([("channel_id", "=", self.id), ("lead_id", "=", lead.id)], limit=1)
        )

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

    def _agent_lock(self, *parts):
        """Advisory lock transacional (liberado no commit/rollback do item)."""
        key = ":".join(str(p) for p in ("dz23", self.company_id.id, *parts))
        self.env.cr.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (key,))

    def _agent_agenda_lock(self):
        """Lock da agenda com o MESMO alcance da checagem de conflito: sempre o da empresa
        (todos os eventos das oportunidades dela) e, havendo responsável, o da agenda dele
        (que pode ter eventos de outras empresas). Ordem fixa: empresa → responsável."""
        self.ensure_one()
        self._agent_lock("agenda")
        if self.agenda_user_id:
            key = "dz23:agenda-user:%s" % self.agenda_user_id.id
            self.env.cr.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (key,))

    # ---- contexto do negócio (escopado por empresa) ----
    def _agent_catalog(self, limit=40):
        return self.env["product.template"].search(
            [("sale_ok", "=", True), ("company_id", "in", (False, self.company_id.id))],
            order="list_price desc",
            limit=limit,
        )

    def _cur(self, pricelist=None):
        currency = (pricelist and pricelist.currency_id) or self.company_id.currency_id
        return currency.symbol or "R$"

    def _agent_pricelist(self, partner=None):
        if partner and "property_product_pricelist" in partner._fields:
            pricelist = partner.with_company(self.company_id).property_product_pricelist
            if pricelist:
                return pricelist
        return self.env["product.pricelist"].search(
            [("company_id", "in", (False, self.company_id.id))], limit=1
        )

    def _agent_unit_price(self, variant, qty=1.0, partner=None):
        """Preço UNITÁRIO calculado pelo Odoo (lista de preços), nunca pela IA."""
        pricelist = self._agent_pricelist(partner)
        if pricelist:
            return pricelist._get_product_price(variant, qty or 1.0), pricelist
        return variant.lst_price, pricelist

    def _agent_business_context(self):
        company = self.company_id
        parts = [_("Empresa: %s.") % (company.name or "DZ23 CRM")]
        prods = self._agent_catalog()
        if prods:
            lines = []
            for p in prods:
                price, pricelist = self._agent_unit_price(p.product_variant_id)
                lines.append("- %s: %s %.2f" % (p.name, self._cur(pricelist), price))
            parts.append(_("Catálogo de produtos/serviços à venda:\n%s") % "\n".join(lines))
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
        # Regras de governança são fixas: o prompt do canal não as remove.
        blocks = [base, self._agent_governance_rules(), self._agent_business_context()]
        # Histórico (chatter = notas internas) só vai para IA LOCAL (on-prem).
        if not self.env["dz23.ai"]._is_external(self.company_id):
            history = self._agent_history(lead)
            if history:
                blocks.append(_("Histórico recente da conversa:\n%s") % history)
        return "\n\n".join(blocks)

    # ---- agenda: parsing DETERMINÍSTICO (não assume hora) ----
    def _agent_company_tz(self):
        calendar = self._agent_calendar()
        name = (
            self.company_id.partner_id.tz
            or (calendar and calendar.tz)
            or self.env.user.tz
            or "America/Sao_Paulo"
        )
        try:
            return pytz.timezone(name)
        except pytz.UnknownTimeZoneError:
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

    def _agent_local_to_utc(self, date, hour, minute):
        aware = self._agent_company_tz().localize(dt.datetime.combine(date, dt.time(hour, minute)))
        return aware.astimezone(pytz.utc).replace(tzinfo=None), aware

    def _agent_calendar(self):
        return self.agenda_calendar_id or self.company_id.resource_calendar_id

    def _agent_duration(self):
        return max(5, self.agenda_duration_minutes or 60)

    def _agent_within_working_hours(self, start_utc, stop_utc):
        """O intervalo inteiro precisa caber no expediente (ausências/feriados descontados)."""
        calendar = self._agent_calendar()
        if not calendar:
            return True
        start = pytz.utc.localize(start_utc)
        stop = pytz.utc.localize(stop_utc)
        intervals = calendar._work_intervals_batch(start, stop, tz=self._agent_company_tz())
        covered = timedelta()
        for i_start, i_stop, _meta in intervals.get(False, []):
            overlap = min(i_stop, stop) - max(i_start, start)
            if overlap > timedelta():
                covered += overlap
        return covered >= (stop - start) - timedelta(seconds=1)

    def _agent_slot_conflict(self, start_utc, minutes=None, exclude_event=None):
        """True se há evento sobrepondo [início-intervalo, fim+intervalo]:
        eventos das oportunidades da empresa do canal E, se houver responsável,
        TODA a agenda dele (inclusive eventos manuais). Busca com sudo: a checagem
        precisa enxergar eventos privados; nada é devolvido além do booleano."""
        minutes = minutes or self._agent_duration()
        buffer = timedelta(minutes=max(0, self.agenda_buffer_minutes or 0))
        start = start_utc - buffer
        stop = start_utc + timedelta(minutes=minutes) + buffer
        scope = Domain("opportunity_id.company_id", "=", self.company_id.id)
        responsible = self.agenda_user_id
        if responsible:
            scope |= Domain("user_id", "=", responsible.id) | Domain(
                "partner_ids", "in", responsible.partner_id.id
            )
        domain = (
            Domain("start", "<", fields.Datetime.to_string(stop))
            & Domain("stop", ">", fields.Datetime.to_string(start))
            & scope
        )
        if exclude_event:
            domain &= Domain("id", "!=", exclude_event.id)
        return bool(self.env["calendar.event"].sudo().search_count(domain, limit=1))

    def _agent_future_event(self, lead):
        return (
            self.env["calendar.event"]
            .sudo()
            .search(
                [
                    ("opportunity_id", "=", lead.id),
                    ("start", ">", fields.Datetime.to_string(fields.Datetime.now())),
                ],
                order="start asc",
                limit=1,
            )
        )

    # ---- venda: casamento DETERMINÍSTICO (acentos + limite de palavra) ----
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

    def _agent_pick_variant(self, template, text):
        variants = template.product_variant_ids
        if len(variants) == 1:
            return variants
        t = _norm(text)
        hits = variants.filtered(
            lambda v: (
                v.product_template_attribute_value_ids
                and all(
                    re.search(r"\b%s\b" % re.escape(_norm(val.name)), t)
                    for val in v.product_template_attribute_value_ids
                )
            )
        )
        return hits if len(hits) == 1 else variants.browse()

    @staticmethod
    def _agent_parse_qty(text, product_name):
        t = _norm(text).replace(_norm(product_name), " ")
        m = re.search(r"(?<![\d/:.,])(\d{1,3})(?![\d/:.,])\s*(?:x|un|unid\w*|pcs|pecas?)?\b", t)
        if m:
            return float(min(999, max(1, int(m.group(1)))))
        m = re.search(r"\b(%s)\b" % "|".join(_NUMBER_WORDS), t)
        return float(_NUMBER_WORDS[m.group(1)]) if m else 1.0

    def _agent_open_quote(self, partner, variant):
        return self.env["sale.order"].search(
            [
                ("partner_id", "=", partner.id),
                ("company_id", "=", self.company_id.id),
                ("origin", "=", _ORIGIN),
                ("state", "in", ("draft", "sent")),
                ("order_line.product_id", "=", variant.id),
            ],
            order="id desc",
            limit=1,
        )

    # ---- ações (na empresa do canal) ----
    def _agent_create_event(self, lead, start_utc, minutes=None):
        minutes = minutes or self._agent_duration()
        partners = lead.partner_id
        vals = {
            "name": _("Agendamento WhatsApp — %s") % (lead.contact_name or lead.name),
            "start": fields.Datetime.to_string(start_utc),
            "stop": fields.Datetime.to_string(start_utc + timedelta(minutes=minutes)),
            "opportunity_id": lead.id,
        }
        if self.agenda_user_id:
            vals["user_id"] = self.agenda_user_id.id
            partners |= self.agenda_user_id.partner_id
        vals["partner_ids"] = [(6, 0, partners.ids)]
        return self.env["calendar.event"].create(vals)

    def _agent_create_quote(self, lead, variant, qty=1.0, correlation_id=None):
        if "sale.order" not in self.env or not variant.sale_ok:
            return self.env["sale.order"].browse() if "sale.order" in self.env else False
        partner = self._agent_partner_for(lead)
        so = self.env["sale.order"].create(
            {
                "partner_id": partner.id,
                "company_id": self.company_id.id,
                "origin": _ORIGIN,
                "dz23_channel_id": self.id,
                "dz23_source_correlation_id": correlation_id or False,
                "order_line": [(0, 0, {"product_id": variant.id, "product_uom_qty": qty})],
            }
        )
        lead.message_post(body=_("🛒 Orçamento %s aberto: %s x %s") % (so.name, qty, variant.name))
        return so

    def _agent_prompt_version(self):
        """Versão do prompt registrada em cada resposta de IA (auditoria)."""
        if not self.agent_prompt:
            return _PROMPT_VERSION
        digest = hashlib.sha256(self.agent_prompt.encode("utf-8")).hexdigest()[:10]
        return "canal-%s-gov1" % digest

    def _agent_reply_ai(self, lead, text, fallback, correlation_id=None):
        """Conversa livre com IA FORA do item da inbox (fila dz23.ai.request, ADR-008).
        Retorna None (a resposta sai pelo worker) ou o fallback imediato quando a
        política impede usar IA (provedor externo sem consentimento)."""
        ai = self.env["dz23.ai"]
        company = self.company_id
        if ai._is_external(company) and not ai._external_allowed(company):
            return fallback
        contact = self._agent_contact_for_lead(lead)
        recipient = (contact and contact.provider_user_id) or lead.phone
        if not recipient:
            return fallback
        self.env["dz23.ai.request"].sudo()._enqueue(
            self, lead, recipient, text, fallback, correlation_id
        )
        return None

    # ---- pipeline principal (determinístico; LLM só conversa) ----
    def handle_inbound(self, number, text, raw=None, message=None):
        self.ensure_one()
        message = message or {}
        mtype = message.get("message_type") or "text"
        contact = self._agent_contact(number)
        lead = contact.lead_id.with_env(self.env)
        conversation = self.env["dz23.conversation"].sudo()._for_contact(contact)
        if not self.agent_autoreply or not conversation.bot_can_reply():
            # Robô desligado ou humano no controle (ADR-010): só registra no lead —
            # sem resposta automática, sem pedido e sem IA.
            shown = text or message.get("caption") or mtype
            lead.message_post(body=_("📩 WhatsApp recebido de %s: %s") % (number, shown))
            return True
        if mtype not in ("text", "interactive"):
            return self._handle_non_text(lead, number, message)
        lead.message_post(body=_("📩 WhatsApp recebido de %s: %s") % (number, text))
        if _OPT_OUT_RE.match(_norm(text or "")):
            conversation.opt_out = True
            reply = _(
                "Pronto! Você não vai mais receber mensagens promocionais. "
                "Se precisar de algo, é só chamar 😊"
            )
        elif self._agent_is_sensitive(text):
            # Reclamação, cobrança, cancelamento de compra, pedido de atendente (ADR-011).
            conversation._agent_handoff("sensitive")
            reply = self._agent_handoff_text()
        else:
            reply = self._agent_route(contact, lead, text or "", message.get("correlation_id"))
        if reply:
            # Entrega DURÁVEL: a resposta vai para a outbox (retry + DLQ).
            self.env["dz23.message.outbox"].sudo()._enqueue(self, number, reply)
            lead.message_post(body=_("🤖 Resposta enfileirada para envio: %s") % reply)
        return True

    def _agent_route(self, contact, lead, txt, correlation_id=None):
        conversation = self.env["dz23.conversation"].sudo()._for_contact(contact)
        reply = self._agent_route_business(contact, lead, txt, correlation_id)
        if reply is not _FREE_TEXT:
            if conversation.bot_turns:
                conversation.bot_turns = 0  # houve avanço determinístico
            return reply
        limit = self.agent_max_bot_turns
        if limit and conversation.bot_turns >= limit:
            conversation._agent_handoff("bot_limit")
            return self._agent_handoff_text()
        conversation.bot_turns += 1
        return self._agent_reply_ai(
            lead,
            txt,
            _("Oi! Já vi sua mensagem 😊 Me conta o que você precisa que eu te ajudo."),
            correlation_id,
        )

    def _agent_route_business(self, contact, lead, txt, correlation_id=None):
        """Intenções determinísticas; _FREE_TEXT quando nenhuma casa."""
        # 0) resposta a um resumo de compra pendente
        pending = self._handle_pending(contact, lead, txt, correlation_id)
        if pending:
            return pending
        if _CANCEL_SCHED_RE.search(txt):
            return self._handle_cancel_schedule(lead, correlation_id)
        if _RESCHED_RE.search(txt):
            return self._handle_reschedule(lead, txt, correlation_id)
        if _SCHED_RE.search(txt):
            return self._handle_schedule(lead, txt, correlation_id)
        if _BUY_RE.search(txt):
            return self._handle_buy(lead, txt, contact=contact)
        if _PRICE_RE.search(txt):
            return self._handle_price(lead, txt)
        return _FREE_TEXT

    def _on_media_downloaded(self, media):
        """Arquivo do cliente validado: anexa uma cópia ao lead do contato (o original
        segue privado na mídia, sujeito à retenção)."""
        self.ensure_one()
        attachment = media.attachment_id
        if not attachment:
            return False
        e164 = _e164_br(media.inbox_id.sender)
        contact = (
            self.env["dz23.channel.contact"]
            .sudo()
            .search([("channel_id", "=", self.id), ("provider_user_id", "=", e164)], limit=1)
        )
        lead = contact.lead_id
        if not lead:
            return False
        copy = attachment.sudo().copy({"res_model": lead._name, "res_id": lead.id})
        media.sudo().lead_attachment_id = copy.id  # retenção/LGPD apagam a cópia também
        lead.sudo().message_post(
            body=_("📎 Arquivo recebido pelo WhatsApp: %s") % media.filename,
            attachment_ids=[copy.id],
        )
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

    # ---- agenda ----
    def _book_slot(self, lead, start_utc, exclude_event=None):
        """Reserva sob lock: expediente -> conflito -> cria. Levanta se indisponível."""
        minutes = self._agent_duration()
        stop_utc = start_utc + timedelta(minutes=minutes)
        if not self._agent_within_working_hours(start_utc, stop_utc):
            raise _SlotUnavailableError("closed")
        self._agent_agenda_lock()
        if self._agent_slot_conflict(start_utc, minutes, exclude_event=exclude_event):
            raise _SlotUnavailableError("busy")
        return self._agent_create_event(lead, start_utc, minutes)

    def _slot_unavailable_reply(self, reason):
        if reason == "closed":
            return _(
                "Nesse horário não estamos atendendo 🙏 Pode escolher outro dia/horário "
                "dentro do nosso expediente?"
            )
        return _("Esse horário já está reservado 🙈 Quer tentar outro horário?")

    def _agent_when_or_question(self, txt):
        """(start_utc, aware, None) ou (None, None, pergunta)."""
        w = self._agent_parse_when(txt)
        if not w.get("has_date"):
            return None, None, _("Claro! Para qual dia você gostaria de marcar? 😊")
        if not w.get("valid"):
            return (
                None,
                None,
                _("Não consegui entender essa data. Pode confirmar o dia (ex.: 15/09)?"),
            )
        if not w.get("time"):
            return (
                None,
                None,
                _("Perfeito, dia %s! Qual horário fica melhor pra você?")
                % w["date"].strftime("%d/%m"),
            )
        hour, minute = w["time"]
        try:
            start_utc, aware = self._agent_local_to_utc(w["date"], hour, minute)
        except ValueError:
            return None, None, _("Esse horário não parece válido. Pode confirmar dia e hora?")
        if start_utc <= fields.Datetime.now():
            return None, None, _("Esse horário já passou 😅 Me passa uma data e hora futuras?")
        return start_utc, aware, None

    def _handle_schedule(self, lead, txt, correlation_id=None):
        start_utc, aware, question = self._agent_when_or_question(txt)
        if question:
            return question
        Action = self.env["dz23.business.action"]
        try:
            if correlation_id:
                action, _executed = Action._run_once(
                    "schedule:%s:%s" % (self.id, correlation_id),
                    "schedule",
                    lambda: self._book_slot(lead, start_utc),
                    channel=self,
                    correlation_id=correlation_id,
                )
                event = action._target()
            else:
                event = self._book_slot(lead, start_utc)
        except _SlotUnavailableError as e:
            return self._slot_unavailable_reply(e.reason)
        when = (
            pytz.utc.localize(event.start).astimezone(self._agent_company_tz()) if event else aware
        )
        lead.message_post(
            body=_("📅 Evento criado para %s (sincroniza com Google Calendar)")
            % when.strftime("%d/%m/%Y %H:%M")
        )
        return _("Prontinho! Agendei para %s. Se precisar remarcar, é só falar 💙") % (
            when.strftime("%d/%m/%Y às %H:%M")
        )

    def _handle_cancel_schedule(self, lead, correlation_id=None):
        self._agent_agenda_lock()
        event = self._agent_future_event(lead)
        if not event:
            return _("Não encontrei nenhum agendamento futuro no seu nome 🤔 Quer marcar um?")
        when = pytz.utc.localize(event.start).astimezone(self._agent_company_tz())
        event.write({"active": False})  # arquiva (não apaga)
        lead.message_post(
            body=_("❌ Agendamento de %s cancelado pelo cliente.") % when.strftime("%d/%m/%Y %H:%M")
        )
        return _("Cancelei seu horário de %s. Se quiser marcar outro, é só me dizer 😊") % (
            when.strftime("%d/%m/%Y às %H:%M")
        )

    def _handle_reschedule(self, lead, txt, correlation_id=None):
        current = self._agent_future_event(lead)
        if not current:
            return self._handle_schedule(lead, txt, correlation_id)
        start_utc, aware, question = self._agent_when_or_question(txt)
        if question:
            return _("Vamos remarcar! Para qual dia e horário? 😊")
        try:
            event = self._book_slot(lead, start_utc, exclude_event=current)
        except _SlotUnavailableError as e:
            return self._slot_unavailable_reply(e.reason)
        current.write({"active": False})
        when = pytz.utc.localize(event.start).astimezone(self._agent_company_tz())
        lead.message_post(
            body=_("🔁 Agendamento remarcado para %s") % when.strftime("%d/%m/%Y %H:%M")
        )
        return _("Remarcado para %s ✅") % when.strftime("%d/%m/%Y às %H:%M")

    # ---- compra ----
    def _handle_buy(self, lead, txt, contact=None):
        matches = self._agent_match_products(txt)
        if len(matches) > 1:
            nomes = ", ".join(m.name for m in matches[:5])
            return _("Temos algumas opções: %s. Qual delas você quer? 😊") % nomes
        if not matches:
            return self._agent_reply_ai(
                lead,
                txt,
                _("Me diz qual produto ou serviço você quer que eu já organizo pra você."),
            )
        template = matches[0]
        variant = self._agent_pick_variant(template, txt)
        if not variant:
            opcoes = ", ".join(v.display_name for v in template.product_variant_ids[:8])
            return _("Temos %(name)s nestas opções: %(opts)s. Qual você prefere?") % {
                "name": template.name,
                "opts": opcoes,
            }
        contact = contact or self._agent_contact_for_lead(lead)
        qty = self._agent_parse_qty(txt, template.name)
        partner = self._agent_partner_for(lead)
        unit, pricelist = self._agent_unit_price(variant, qty, partner)
        contact.write(
            {
                "agent_pending_action": "buy",
                "agent_pending_product_id": variant.id,
                "agent_pending_qty": qty,
                "agent_pending_ref": uuid.uuid4().hex,
                "agent_pending_expires_at": fields.Datetime.now()
                + timedelta(minutes=_PENDING_MINUTES),
            }
        )
        return _(
            "Resumo do pedido:\n• %(qty)s x %(name)s — %(cur)s %(total).2f\n"
            "Confirma? Responda *SIM* para eu gerar o orçamento ou *NÃO* para cancelar."
        ) % {
            "qty": ("%g" % qty),
            "name": variant.display_name,
            "cur": self._cur(pricelist),
            "total": unit * qty,
        }

    def _handle_pending(self, contact, lead, txt, correlation_id=None):
        """Confirmação/negativa do resumo. None se não havia pendência aplicável."""
        if not contact or not contact._agent_pending_buy():
            return None
        norm = _norm(txt)
        if _NO_RE.match(norm):
            contact._agent_clear_pending()
            return _("Tudo bem, não gerei o pedido. Se mudar de ideia, é só chamar 😊")
        if not _YES_RE.match(norm):
            return None
        variant = contact.agent_pending_product_id
        qty = contact.agent_pending_qty or 1.0
        key = "quote:%s:%s:%s" % (self.id, contact.id, contact.agent_pending_ref)

        def _create():
            partner = self._agent_partner_for(lead)
            self._agent_lock("quote", partner.id)
            order = self._agent_open_quote(partner, variant)
            if order:
                # Mesma intenção: reutiliza o orçamento aberto (ajusta a quantidade).
                line = order.order_line.filtered(lambda ln: ln.product_id == variant)[:1]
                if line and line.product_uom_qty != qty:
                    line.product_uom_qty = qty
                return order
            return self._agent_create_quote(lead, variant, qty, correlation_id)

        action, _executed = self.env["dz23.business.action"]._run_once(
            key, "quote", _create, channel=self, correlation_id=correlation_id
        )
        order = action._target()
        contact._agent_clear_pending()
        if not order:
            return _("Não consegui gerar o orçamento agora 😕 Um atendente vai te ajudar.")
        return _(
            "Pronto! Orçamento %(name)s gerado: total %(cur)s %(total).2f. "
            "Um atendente segue com você para pagamento e entrega 💙"
        ) % {
            "name": order.name,
            "cur": order.currency_id.symbol or self._cur(),
            "total": order.amount_total,
        }

    def _handle_price(self, lead, txt):
        matches = self._agent_match_products(txt)
        partner = lead.partner_id or None
        if len(matches) == 1:
            p = matches[0]
            unit, pricelist = self._agent_unit_price(p.product_variant_id, 1.0, partner)
            return _("O %s fica %s %.2f. Quer que eu já reserve pra você? 😊") % (
                p.name,
                self._cur(pricelist),
                unit,
            )
        if len(matches) > 1:
            parts = []
            for m in matches[:5]:
                unit, pricelist = self._agent_unit_price(m.product_variant_id, 1.0, partner)
                parts.append("%s (%s %.2f)" % (m.name, self._cur(pricelist), unit))
            return _("Temos: %s. Sobre qual quer saber? ") % ", ".join(parts)
        return self._agent_reply_ai(
            lead, txt, _("Me diz qual item você quer saber o preço que eu te falo certinho.")
        )
