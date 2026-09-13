# Transação PIX Woovi IDEMPOTENTE (Fase 5):
# - a cobrança é criada UMA vez por transação (lock de linha + id persistido) e
#   reutilizada em novas renderizações da página; correlationID = referência da
#   transação (a Woovi não aceita correlationID repetido);
# - só BRL e valor > 0; expiração registrada; cobrança expirada não é recriada;
# - eventos (webhook/conciliação) aplicados de forma MONOTÔNICA e com valor
#   recebido OBRIGATÓRIO para confirmar; valor/moeda divergente => erro.
import logging

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from .woovi_common import (
    REUSABLE_STATUSES,
    STATUS_RANK,
    WOOVI_STATUSES,
    WooviEventError,
    normalize_status,
    paid_cents,
    parse_datetime,
    payment_currency,
    to_cents,
)

_logger = logging.getLogger(__name__)


class PaymentTransaction(models.Model):
    _inherit = "payment.transaction"

    woovi_charge_id = fields.Char("Cobrança Woovi", readonly=True, copy=False, index=True)
    woovi_br_code = fields.Text("PIX copia e cola", readonly=True, copy=False)
    woovi_qr_code_image = fields.Char("QR Code (URL)", readonly=True, copy=False)
    woovi_expires_at = fields.Datetime("Expira em", readonly=True, copy=False)
    woovi_status = fields.Selection(
        WOOVI_STATUSES, string="Status Woovi", readonly=True, copy=False, index=True
    )
    woovi_paid_cents = fields.Integer("Valor recebido (centavos)", readonly=True, copy=False)
    woovi_event_ids = fields.One2many("dz23.woovi.event", "transaction_id", readonly=True)

    # ------------------------------------------------------------ cobrança
    def _get_specific_rendering_values(self, processing_values):
        if self.provider_code != "woovi":
            return super()._get_specific_rendering_values(processing_values)
        return self._woovi_ensure_charge()

    def _woovi_rendering_values(self):
        return {
            "woovi_br_code": self.woovi_br_code or "",
            "woovi_qr_code_image": self.woovi_qr_code_image or "",
            "woovi_status": self.woovi_status or "",
            "woovi_expires_at": self.woovi_expires_at,
        }

    def _woovi_charge_reusable(self):
        return bool(
            self.woovi_charge_id
            and self.woovi_status in REUSABLE_STATUSES
            and (not self.woovi_expires_at or self.woovi_expires_at > fields.Datetime.now())
        )

    def _woovi_ensure_charge(self):
        """Cria a cobrança uma única vez; renderizações seguintes reutilizam."""
        self.ensure_one()
        if self._woovi_charge_reusable():
            return self._woovi_rendering_values()
        if self.currency_id.name != "BRL":
            self._set_error(
                _("A Woovi aceita apenas BRL (moeda da transação: %s).") % self.currency_id.name
            )
            return {}
        cents = to_cents(self.amount)
        if cents <= 0:
            self._set_error(_("Valor inválido para cobrança PIX."))
            return {}
        # Serializa renderizações concorrentes da MESMA transação (duas abas/cliques).
        self.env.cr.execute(
            "SELECT id FROM payment_transaction WHERE id = %s FOR UPDATE", (self.id,)
        )
        self.invalidate_recordset()
        if self._woovi_charge_reusable():
            return self._woovi_rendering_values()
        if self.woovi_charge_id:
            # Cobrança existente mas vencida/cancelada: o correlationID não pode ser
            # reutilizado — o cliente precisa iniciar um novo pagamento.
            if self.woovi_status in REUSABLE_STATUSES:
                self.woovi_status = "expired"
            if self.state in ("draft", "pending"):
                self._set_canceled(
                    state_message=_("Cobrança PIX expirada; gere um novo pagamento.")
                )
            return self._woovi_rendering_values()
        payload = {"correlationID": self.reference, "value": cents, "comment": self.reference}
        try:
            response = self._send_api_request("POST", "/charge", json=payload)
        except ValidationError as error:
            self._set_error(str(error))
            return {}
        charge = (response or {}).get("charge") or {}
        returned_value = charge.get("value")
        if (returned_value is not None and int(returned_value) != cents) or (
            charge.get("correlationID") and charge["correlationID"] != self.reference
        ):
            self._set_error(_("A Woovi devolveu uma cobrança divergente da transação."))
            return {}
        charge_id = (
            charge.get("identifier") or charge.get("globalID") or charge.get("correlationID")
        ) or self.reference
        self.write(
            {
                "woovi_charge_id": charge_id,
                "provider_reference": charge_id,
                "woovi_br_code": charge.get("brCode") or False,
                "woovi_qr_code_image": charge.get("qrCodeImage") or False,
                "woovi_expires_at": parse_datetime(charge.get("expiresDate")),
                "woovi_status": normalize_status(response) or "active",
            }
        )
        if self.state == "draft":
            self._set_pending()
        return self._woovi_rendering_values()

    # ------------------------------------------------------------ eventos
    def _woovi_apply_payment_data(self, payment_data):
        """Aplica webhook/consulta Woovi. Retorna (resultado, mensagem) com resultado em
        {'applied', 'ignored', 'rejected'}; levanta WooviEventError se faltar dado."""
        self.ensure_one()
        status = normalize_status(payment_data)
        if not status:
            return "ignored", "status Woovi desconhecido"
        current = self.woovi_status
        if current and STATUS_RANK[status] < STATUS_RANK.get(current, 0):
            return "ignored", "evento fora de ordem (%s depois de %s)" % (status, current)
        if current == status and status in ("completed", "expired", "cancelled", "refunded"):
            return "ignored", "status %s já aplicado" % status
        if status == "completed":
            return self._woovi_confirm(payment_data)
        if status in ("expired", "cancelled"):
            self.woovi_status = status
            if self.state in ("draft", "pending", "authorized"):
                self._set_canceled(state_message=_("Cobrança PIX %s.") % status)
            return "applied", "cobrança %s" % status
        if status == "refunded":
            self.woovi_status = "refunded"
            _logger.info("Woovi: estorno registrado na transação %s.", self.reference)
            return "applied", "estorno registrado"
        self.woovi_status = status
        if self.state == "draft":
            self._set_pending()
        return "applied", "cobrança %s" % status

    def _woovi_confirm(self, payment_data):
        received = paid_cents(payment_data)
        if received is None:
            raise WooviEventError("valor recebido ausente: pagamento NÃO confirmado")
        currency = payment_currency(payment_data)
        if currency != "BRL" or self.currency_id.name != "BRL":
            self.write({"woovi_status": "error", "woovi_paid_cents": received})
            self._set_error(_("Moeda divergente na confirmação Woovi (%s).") % currency)
            return "rejected", "moeda divergente"
        expected = to_cents(self.amount)
        if received != expected:
            self.write({"woovi_status": "error", "woovi_paid_cents": received})
            self._set_error(
                _(
                    "Valor divergente na confirmação Woovi (esperado %(expected)s, recebido %(received)s)."
                )
                % {"expected": expected, "received": received}
            )
            return "rejected", "valor divergente"
        self.write({"woovi_status": "completed", "woovi_paid_cents": received})
        self._set_done()
        return "applied", "pagamento confirmado"

    # Compatibilidade com o fluxo padrão do Odoo (_process).
    @api.model
    def _extract_reference(self, provider_code, payment_data):
        if provider_code != "woovi":
            return super()._extract_reference(provider_code, payment_data)
        return ((payment_data or {}).get("charge") or {}).get("correlationID")

    def _extract_amount_data(self, payment_data):
        if self.provider_code != "woovi":
            return super()._extract_amount_data(payment_data)
        return None  # validação de valor/moeda feita em _woovi_confirm (valor obrigatório)

    def _apply_updates(self, payment_data):
        if self.provider_code != "woovi":
            return super()._apply_updates(payment_data)
        try:
            self._woovi_apply_payment_data(payment_data)
        except WooviEventError as error:
            _logger.warning("Woovi: %s (transação %s).", error, self.reference)

    # ------------------------------------------------------------ conciliação
    @api.model
    def _cron_woovi_reconcile(self, limit=50):
        """Consulta cobranças em aberto na Woovi e aplica o status real (eventos
        perdidos ou fora de ordem). Nunca cria cobrança."""
        since = fields.Datetime.subtract(fields.Datetime.now(), days=7)
        txs = self.sudo().search(
            [
                ("provider_code", "=", "woovi"),
                ("state", "in", ("draft", "pending")),
                ("woovi_charge_id", "!=", False),
                ("create_date", ">=", since),
            ],
            order="id",
            limit=limit,
        )
        Event = self.env["dz23.woovi.event"].sudo()
        for tx in txs:
            try:
                response = tx._send_api_request("GET", "/charge/%s" % tx.woovi_charge_id)
            except ValidationError:
                _logger.info("Woovi: conciliação indisponível para %s.", tx.reference)
                continue
            charge = (response or {}).get("charge") or {}
            if charge:
                Event._ingest_payload(
                    {"event": "DZ23:RECONCILE", "charge": charge}, source="reconcile"
                )
