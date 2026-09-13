# Governança do robô (Fase 8, ADR-011): assunto sensível e pedido de atendente vão
# para um humano; conversa livre com IA tem limite de respostas seguidas; a resposta
# da IA passa por uma guarda determinística (a IA nunca decide preço, desconto,
# estoque, pagamento, fiscal, cancelamento ou exclusão de dados).
import re
from datetime import timedelta

from odoo import fields, models
from odoo.tools.translate import _

from .whatsapp_agent import _norm

# Texto já normalizado (sem acento, minúsculo). "cancelar agendamento" NÃO entra:
# é tratado pelo fluxo determinístico de agenda.
_SENSITIVE_RE = re.compile(
    r"\breclama|\bprocon\b|\badvogad|\bjudicial|\bprocessar\b|\bjustica\b|"
    r"cobranca\s+indevida|cobrad[oa]s?\s+indevid|\bestorn|\breembols|\bfraude\b|\bgolpe\b|"
    r"\bcancel\w*\s+(?:a\s+|o\s+|minha\s+|meu\s+)?(?:compra|pedido|contrato|assinatura|plano)\b|"
    r"falar\s+com\s+(?:um\s+|uma\s+|o\s+|a\s+)?(?:atendente|humano|pessoa|gerente|responsavel)|"
    r"atendimento\s+humano"
)
_UNSAFE_AI_REPLY_RE = re.compile(
    r"r\$\s*\d|\d\s*%|\bdesconto|\bparcel|\bfrete\s+gratis|\bestorn|\breembols|"
    r"\bem\s+estoque\b|\bnota\s+fiscal\b|\bcancelad[oa]\b|\bpix\b|\bboleto\b"
)


class DZ23ChannelGovernance(models.Model):
    _inherit = "dz23.channel"

    agent_max_bot_turns = fields.Integer(
        "Máx. respostas livres seguidas do robô",
        default=5,
        help="Conversa livre com IA sem avanço (preço, compra, agenda) além deste limite "
        "é transferida para um atendente. 0 = sem limite.",
    )

    def _agent_governance_rules(self):
        return _(
            "Regras obrigatórias: nunca informe, prometa ou negocie preço, desconto, "
            "estoque, prazo de entrega, forma de pagamento, nota fiscal, cancelamento, "
            "reembolso ou exclusão de dados; diga que a equipe confirma pelo sistema. "
            "Não invente informações sobre a empresa."
        )

    def _agent_is_sensitive(self, text):
        return bool(_SENSITIVE_RE.search(_norm(text or "")))

    def _agent_handoff_text(self):
        self.ensure_one()
        now = fields.Datetime.now()
        if self._agent_within_working_hours(now, now + timedelta(minutes=1)):
            return _(
                "Recebi sua mensagem 😊 Vou chamar alguém da nossa equipe para te "
                "atender por aqui, só um instante."
            )
        return _(
            "Recebi sua mensagem 😊 Nossa equipe atende em horário comercial; assim que "
            "abrirmos, alguém te responde por aqui."
        )

    def _agent_guard_ai_reply(self, reply):
        """Retorna (texto, guarda_acionada)."""
        if _UNSAFE_AI_REPLY_RE.search(_norm(reply or "")):
            return (
                _(
                    "Sobre valores, condições e pagamento eu confirmo pela nossa tabela "
                    "oficial 😊 Me diz o nome do produto ou serviço que eu consulto pra você."
                ),
                True,
            )
        return reply, False
