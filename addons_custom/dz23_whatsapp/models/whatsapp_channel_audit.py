# Auditoria de alteração de credenciais do canal (Fase 10, ADR-013): registra QUEM
# alterou QUAIS campos — nunca os valores.
from odoo import models

CREDENTIAL_FIELDS = frozenset(
    {
        "evo_apikey",
        "meta_token",
        "meta_app_secret",
        "meta_verify_token",
        "twilio_token",
        "callback_secret",
        "webhook_token",
    }
)


class DZ23ChannelAudit(models.Model):
    _inherit = "dz23.channel"

    def write(self, vals):
        changed = sorted(CREDENTIAL_FIELDS.intersection(vals))
        result = super().write(vals)
        if changed:
            AccessLog = self.env["dz23.access.log"]
            for channel in self:
                AccessLog._log(
                    "credential_change", records=channel, detail="campos=%s" % ",".join(changed)
                )
        return result
