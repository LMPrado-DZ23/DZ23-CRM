# Ações de negócio IDEMPOTENTES (ADR-005). Um efeito (orçamento, agendamento,
# cobrança…) roda UMA vez por `idempotency_key`; chamadas repetidas (retry da
# inbox, mensagem repetida, dois workers) devolvem o mesmo alvo. Só ações
# concluídas ficam registradas: se o efeito falhar, a transação do efeito é
# desfeita junto (savepoint) e um novo retry pode executá-lo.
import logging

import psycopg2
from odoo import api, fields, models

from .queue_utils import payload_json

_logger = logging.getLogger(__name__)

ACTION_TYPES = [
    ("quote", "Orçamento"),
    ("schedule", "Agendamento"),
    ("reschedule", "Remarcação"),
    ("cancel_schedule", "Cancelamento de agendamento"),
    ("payment", "Cobrança"),
    ("other", "Outro"),
]


class DZ23BusinessAction(models.Model):
    _name = "dz23.business.action"
    _description = "DZ23 — Ação de negócio idempotente"
    _order = "id desc"
    _rec_name = "idempotency_key"

    idempotency_key = fields.Char(required=True, index=True, readonly=True)
    company_id = fields.Many2one("res.company", required=True, index=True, readonly=True)
    channel_id = fields.Many2one("dz23.channel", ondelete="set null", index=True, readonly=True)
    source_correlation_id = fields.Char(index=True, readonly=True)
    source_inbox_id = fields.Many2one("dz23.message.inbox", ondelete="set null", readonly=True)
    action_type = fields.Selection(ACTION_TYPES, required=True, index=True, readonly=True)
    state = fields.Selection(
        [
            ("pending", "Pendente"),
            ("done", "Concluída"),
            ("failed", "Falhou"),
            ("cancelled", "Cancelada"),
        ],
        default="pending",
        required=True,
        index=True,
        readonly=True,
    )
    target_model = fields.Char(readonly=True)
    target_res_id = fields.Many2oneReference(model_field="target_model", readonly=True)
    result_payload = fields.Text(readonly=True, help="Resumo sanitizado do resultado (JSON).")
    completed_at = fields.Datetime(readonly=True)
    error = fields.Char(readonly=True)

    _key_uniq = models.Constraint(
        "unique(idempotency_key)", "Ação de negócio já registrada (idempotência)."
    )

    def _target(self):
        self.ensure_one()
        if not (self.target_model and self.target_res_id) or self.target_model not in self.env:
            return None
        return self.env[self.target_model].browse(self.target_res_id).exists() or None

    @api.model
    def _run_once(
        self,
        key,
        action_type,
        callback,
        channel=None,
        company=None,
        correlation_id=None,
        result=None,
    ):
        """Executa `callback()` uma única vez por `key`. Retorna (ação, executou?).

        - chave já concluída => não executa, devolve a ação (alvo via `_target()`);
        - concorrente com a mesma chave => espera o outro (índice único) e reutiliza;
        - `callback` levanta => nada é gravado (savepoint) e a exceção propaga.
        """
        if not key:
            raise ValueError("idempotency_key obrigatória")
        Action = self.sudo()
        existing = Action.search([("idempotency_key", "=", key)], limit=1)
        if existing and existing.state == "done":
            return existing, False
        company = company or (channel.company_id if channel else self.env.company)
        inbox = self.env["dz23.message.inbox"].sudo().browse()
        if correlation_id:
            inbox = inbox.search([("correlation_id", "=", correlation_id)], limit=1)
        try:
            with self.env.cr.savepoint():
                action = existing or Action.create(
                    {
                        "idempotency_key": key,
                        "company_id": company.id,
                        "channel_id": channel.id if channel else False,
                        "source_correlation_id": correlation_id or False,
                        "source_inbox_id": inbox.id or False,
                        "action_type": action_type,
                        "state": "pending",
                    }
                )
                action.flush_recordset()
                self.env.cr.execute(
                    "SELECT id FROM dz23_business_action WHERE id = %s FOR UPDATE", (action.id,)
                )
                target = callback()
                vals = {"state": "done", "completed_at": fields.Datetime.now(), "error": False}
                if target:
                    vals.update({"target_model": target._name, "target_res_id": target.id})
                if result:
                    vals["result_payload"] = payload_json(result)
                action.write(vals)
            return action, True
        except psycopg2.IntegrityError:
            # Outro worker concluiu a mesma chave enquanto esperávamos o índice.
            done = Action.search([("idempotency_key", "=", key), ("state", "=", "done")], limit=1)
            if done:
                return done, False
            raise
