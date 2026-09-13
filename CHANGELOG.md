# Changelog

Formato baseado em [Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/).
Este projeto usa versionamento por módulo (Odoo `19.0.x.y.z`).

## [Não lançado]

### Documentação
- **Fase 0 — baseline** (`docs/BASELINE_AUDIT.md`, `docs/IMPLEMENTATION_PLAN.md`,
  `docs/diagrams/current-message-flow.mmd`): 27 riscos catalogados com evidência
  `arquivo:linha`, incompatibilidades documentação × código e resultado real das
  verificações (instalação limpa + 41/41 testes `dz23`, ruff limpo). Correção de
  termo: a entrega da outbox é *at-least-once*, não "exatamente-uma-vez".

### Segurança
- Credenciais de canal (`evo_apikey`, `meta_token`, `callback_secret`, etc.)
  restritas a administrador (`groups="base.group_system"`); envio/webhook leem via `sudo`.
- Webhook Woovi com teto de corpo de 1 MiB (anti-DoS).
- Gate de privacidade de IA externa (consentimento LGPD) + redação de PII
  (e-mail/telefone/CPF/CNPJ); imagem bloqueada para provedores externos.
- Imagens Docker pinadas por digest; dependências OCA travadas por commit
  (`dependencies.lock.yml`); actions e imagens do CI pinadas por SHA/digest.
- Migração não loga mais o `webhook_token`.

### Adicionado
- **Inbox durável** (`dz23.message.inbox`): idempotência (dedupe por message_id),
  worker com retry/backoff/jitter e DLQ; webhook responde rápido.
- **Outbox durável** (`dz23.message.outbox`): entrega de respostas com retry/DLQ,
  validação de sucesso pelo corpo do provedor e `provider_message_id`.
- Telas de administração de Inbox/Outbox (DLQ visível + reprocessar/reenviar).
- Outbox: envio **exatamente-uma-vez** no cron (guarda de já-enviado + commit por
  registro em produção) reduzindo duplicidade; teste dedicado.
- Workflow de **release** (`.github/workflows/release.yml`): em tag `v*`, gera
  SBOM (CycloneDX) e publica um GitHub Release com o SBOM anexado.
- Script de **smoke E2E** (`scripts/smoke.sh`): instalação limpa de todos os
  módulos num banco descartável + suíte `dz23` (41/41).
- Agente determinístico (agenda sem inventar horário, rejeita passado/conflito;
  preço ≠ compra) com conflito de agenda **isolado por empresa**.
- Toggle de consentimento de IA externa (LGPD) nas Configurações.

### Alterado
- Licença dos módulos DZ23 → **MIT** (manifest usa o enum válido do Odoo
  `Other OSI approved licence`; texto MIT em `LICENSE`/`NOTICE.md`).
- Debrand: promo "Powered by Odoo / site grátis" ocultado em todas as páginas.

### Corrigido
- Entrega de resposta ao cliente deixou de ser "fire-and-forget" (não perde mais
  a resposta se o provedor cair no momento do envio).
- `"license": "MIT"` inválido no manifest do Odoo (quebrava o load) → corrigido.

## Histórico anterior
Ver `audit/` para as auditorias P0/reauditorias e a matriz de paridade OSS.
