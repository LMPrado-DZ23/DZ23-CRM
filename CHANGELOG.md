# Changelog

Formato baseado em [Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/).
Este projeto usa versionamento por módulo (Odoo `19.0.x.y.z`).

## [Não lançado]

### Fase 2 — Normalização de provedores e webhooks completos (`dz23_whatsapp` 19.0.9.0.0)
- **Normalizadores puros** (`provider_normalizers.py`, ADR-007) com contrato
  interno validado: Meta (todas as entries/changes/messages + `statuses` com erro),
  Evolution (`MESSAGES_UPSERT`, `MESSAGES_UPDATE`, `CONNECTION_UPDATE`, wrappers
  efêmeros/view-once) e Twilio (mensagem + status callback).
- **Twilio completo**: webhook tokenizado com `X-Twilio-Signature` (HMAC-SHA1 da URL
  pública + parâmetros), `StatusCallback` no envio, validação de `AccountSid` e número.
- **Validação de canal** (409): `phone_number_id` (Meta), instância (Evolution),
  conta/número (Twilio).
- Grupos, `status@broadcast` e newsletters não viram atendimento; mensagens
  `fromMe` vão para eventos (nunca para o inbox); mídia/localização/contato são
  registrados (`message_type`, `caption`, `reply_to`, `media_ref`) e o agente
  confirma o recebimento em vez de ignorar.
- Canal: `connection_state`, `last_webhook_at`, URL pública do webhook para copiar;
  Evolution passa a assinar `MESSAGES_UPDATE` e `CONNECTION_UPDATE`.
- Meta: `biz_opaque_callback_data` = `correlation_id` da outbox.
- Log de inbound sem PII (só últimos 4 dígitos e tipo).
- Testes: contrato dos normalizadores + webhooks HTTP ponta a ponta das três APIs.

### Fase 1 — Ciclo de vida de mensagem e filas com claim/lease (`dz23_whatsapp` 19.0.8.0.0)
- **Novo** `dz23.message.event` (append-only, dedupe por canal+id+direção+status,
  índices por empresa/status, canal/id, outbox/data e pendentes) — ADR-006.
- **Outbox**: `current_status` monotônico (queued→sent→delivered→read; falha só
  antes da entrega), `sent_at/delivered_at/read_at/failed_at/last_status_at`,
  `provider_error_code/message`, `correlation_id`, `client_message_id`,
  `duration_ms`, estado `sending` + `lease_until`.
- **Filas** (ADR-003): claim atômico `FOR UPDATE SKIP LOCKED` + lease, commit por
  item, tentativa contada no claim, recuperação de lease vencido (outbox com id do
  provedor não é reenviada), orçamento de tempo por execução do cron.
- **Inbox**: payload JSON **completo** validado (corrige corte em 20.000 caracteres
  que gerava JSON inválido e mandava a mensagem para a DLQ), `payload_hash`,
  `payload_preview`, `received_at/processed_at`.
- **Webhook**: falha ao persistir responde **HTTP 500** (antes 200 com perda
  silenciosa); log só com metadados.
- Telas: Eventos de status; ciclo de vida e eventos no formulário da outbox; buscas
  e filtros de DLQ/retry.
- Migração 19.0.8.0.0 não destrutiva (backfill de status, correlation_id por linha,
  hash do payload).
- Testes: +24 (`test_message_lifecycle`, `test_queue_claim` com dois cursores reais,
  webhook 500 e reentrega deduplicada). Suíte `dz23`: 65/65.

### CI
- Jobs `sast`, `odoo-tests`, `deps-container` e `secrets` (PR) corrigidos; actions
  atualizadas por SHA; ruff 0.16.4.

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
