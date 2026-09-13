# DZ23 CRM — Provedor de pagamento Woovi (PIX).
# Fluxo: cria cobrança PIX via API Woovi UMA vez por transação (reutilizada ao
# recarregar), exibe QR Code/copia-e-cola e confirma via webhook persistido e
# deduplicado + conciliação periódica. Credenciais (AppID) no provider (servidor).
# NOTA: o fluxo PIX ao vivo requer conta/sandbox Woovi + webhook público.
{
    "name": "DZ23 CRM — Pagamento Woovi (PIX)",
    "version": "19.0.2.0.0",
    "summary": "Provedor de pagamento PIX via Woovi (idempotente, conciliado).",
    "author": "DZ23 (LEANDRO MARCOS PRADO LTDA)",
    "website": "https://www.dz23.com.br",
    "license": "Other OSI approved licence",
    "category": "Accounting/Payment Providers",
    "depends": ["payment"],
    "data": [
        "security/ir.model.access.csv",
        "security/woovi_rules.xml",
        "views/payment_woovi_templates.xml",
        "data/payment_provider_data.xml",
        "data/woovi_cron.xml",
        "views/woovi_event_views.xml",
        "views/payment_provider_views.xml",
    ],
    "installable": True,
    "application": False,
}
