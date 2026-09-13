# DZ23 CRM — Plano de Implementação (evolução em fases)

> Base: commit `fd04837` (`main`), branch de trabalho `feat/crm-evolution`.
> Diagnóstico completo em [`BASELINE_AUDIT.md`](BASELINE_AUDIT.md); fluxo atual em
> [`diagrams/current-message-flow.mmd`](diagrams/current-message-flow.mmd).
> Cada fase termina **instalável**, com testes `dz23` verdes, `ruff` limpo e
> documentação/CHANGELOG atualizados. Nenhuma integração real é chamada em teste.

## Princípios

1. **Nunca editar o core do Odoo** — só `addons_custom/`, herança ORM.
2. **Garantias com nomes corretos**: *at-least-once delivery*, *deduplicated*,
   *exactly-once effect* (efeito de negócio), *reconciled*. Nunca "exactly-once
   delivery" com provedores sem idempotency key.
3. **Sem nova infraestrutura**: filas lógicas dentro do Odoo/Postgres (sem Redis,
   Celery ou broker). Concorrência resolvida no Postgres (locks de linha, advisory
   locks, constraints únicas).
4. **Risco primeiro**: a ordem abaixo segue os IDs de risco do baseline (P0 → P3),
   não a numeração do prompt mestre; todo item do prompt continua coberto.
5. **Migrações não destrutivas**: colunas novas com default/backfill; nada é
   apagado; rollback documentado por fase.

## Decisões arquiteturais (viram ADRs em `docs/adr/`)

| ADR | Decisão | Justificativa |
|---|---|---|
| ADR-003 | **Claim com lease** na outbox/inbox: `UPDATE … SET state='sending', lease_until=now()+ttl WHERE id IN (SELECT … FOR UPDATE SKIP LOCKED) RETURNING id`, commit do claim, chamada externa **fora** do lock, commit do resultado | Remove lock longo durante I/O externo; permite N workers; lease vencido é reconciliado (não reenviado às cegas) |
| ADR-004 | **Anti double-booking via `pg_advisory_xact_lock`** por (empresa, recurso/responsável) + checagem de sobreposição **na mesma transação** | Exclusion constraint exigiria alterar a tabela `calendar_event` do core (afeta eventos manuais de todas as empresas) — rejeitado |
| ADR-005 | **`dz23.business.action`** com `idempotency_key` única (`channel:contact:action:intent-hash`) criada via savepoint antes do efeito | Mensagens repetidas (IDs diferentes, mesma intenção) reutilizam o mesmo orçamento/evento/cobrança |
| ADR-006 | **Ciclo de vida de mensagem** em `dz23.message.event` (append-only) + `current_status` monotônico no outbox por *rank* de estado | Evento atrasado não regride `read → sent`; histórico auditável |
| ADR-007 | **Camada de normalização** (`dz23.provider.normalizer`): payload de provedor → contrato interno validado antes de gravar | Domínio do CRM não conhece formato Meta/Twilio/Evolution |
| ADR-008 | **Filas lógicas** (`inbox`, `agent_decision`, `ai_request`, `business_effect`, `outbox`, `status_event`) como estados/crons separados no Postgres | Stack fixa Odoo+PG; sem broker externo |

## Status (branch `feat/crm-evolution`, PR #6)

| Fase | Status | Commit | Evidência |
|---|---|---|---|
| 0 Baseline | ✅ | `d170085` | smoke 41/41 |
| CI verde (sast/odoo-tests/trivy) | ✅ | `0352ffe` | jobs do CI verdes |
| 1 Ciclo de vida + P0 | ✅ | `621e71b` | smoke 65/65 |
| 2 Normalização e webhooks | ✅ | `8570ceb` | smoke 96/96 |
| 3 Idempotência de efeitos | ✅ | `5bdce7a` | smoke 117/117 |
| 4 Outbox robusta e filas lógicas | ✅ | `8d4a23b` | smoke 131/131 |
| 5 PIX Woovi | ✅ | `0580496` | smoke 151/151 |
| 6 Mídia e templates | ✅ | `c050927` | smoke 179/179 |
| 7 Caixa de atendimento | ✅ | `bcc55b6` | smoke 196/196 |
| 8 Governança da IA | ✅ | `5a88570` | smoke 217/217 |
| 9 Observabilidade | ✅ | `79e6497` | smoke 225/225 |
| 10 LGPD e retenção | ✅ | `716ee26` | smoke 237/237 |
| 11 Testes | ✅ | `d4cf471` | smoke 249/249 + upgrade |
| 12 Documentação | ✅ | commit da Fase 12 | este documento e os guias |

Contagens de teste são as do `scripts/smoke.sh` executado no commit indicado.

## Fases

### Fase 0 — Diagnóstico e baseline ✅
Entregas: `BASELINE_AUDIT.md`, este plano, diagrama Mermaid. Sem mudança funcional.

### Fase 1 — Correções P0 + ciclo de vida da mensagem
Riscos: R01, R02, R03, R04, R05, R08.
- `dz23.message.event` (campos do prompt §5.1) + índices: (company_id, status),
  (channel_id, provider_message_id), (outbox_id, occurred_at), parcial para pendentes.
- Outbox: `current_status`, `sent_at`, `delivered_at`, `read_at`, `failed_at`,
  `last_status_at`, `provider_error_code`, `provider_error_message`,
  `client_message_id`, `correlation_id`; migração com backfill (`sent` → `sent_at`).
- Transição monotônica (`queued < sent < delivered < read`; `failed/undelivered/
  expired/cancelled` terminais, sem regressão).
- Outbox/inbox com claim + lease (ADR-003); commit por item **após** o claim.
- Inbox: payload completo validado como JSON (sem corte arbitrário), `payload_hash`,
  `payload_preview`; falha de persistência → **HTTP 500** (provedor reentrega), nunca 200.
- Inbox: processamento item a item com commit (um item lento não derruba o lote;
  tentativas contam mesmo com timeout do worker).
- Testes: status monotônico, evento fora de ordem, payload grande, enqueue com falha,
  claim concorrente (dois cursores), lease vencido.

### Fase 2 — Normalização e webhooks completos
Riscos: R06, R07, R09, R10, R11.
- Contrato interno validado (§6.4) + normalizadores Meta/Twilio/Evolution.
- Meta: todas as `entry/changes/messages` e `statuses` (erros incluídos);
  `phone_number_id` do payload validado contra o canal (409).
- Twilio: webhook de entrada + status callback, assinatura `X-Twilio-Signature`
  (HMAC-SHA1 da URL + params), correlação por `MessageSid`, `StatusCallback` no envio.
- Evolution: `MESSAGES_UPDATE` (status), `CONNECTION_UPDATE`, `fromMe` como
  evento outbound (nunca inbound), grupos/`status@broadcast` ignorados, eventos fora de ordem.
- Testes de contrato com fixtures anonimizadas por provedor.

### Fase 3 — Idempotência de efeitos de negócio
Riscos: R12, R13, R14, R15.
- `dz23.business.action` (ADR-005) com estados `pending/done/failed`.
- Compra em etapas: produto → quantidade/variação → resumo → **confirmação explícita**
  → orçamento idempotente (reutiliza rascunho aberto do mesmo contato/produto);
  preço/desconto calculados pelo `sale.order` (pricelist), nunca pela IA.
- Pedido carrega canal, empresa, mensagem de origem.
- Agenda (ADR-004): advisory lock, conflito com **todos** os eventos do responsável
  (inclusive manuais), horário de funcionamento (`resource.calendar`), feriados
  (`resource.calendar.leaves`), fuso da empresa, duração/intervalo configuráveis,
  cancelamento/remarcação.
- Testes: compra repetida não duplica, pergunta de preço não cria pedido, produto
  ambíguo, dupla reserva com dois cursores, fuso, feriado, fora do expediente.

### Fase 4 — Outbox robusta e filas lógicas
Riscos: R16, R17, R18.
- Erro transitório (rede, 429, 5xx) × permanente (4xx de validação) → DLQ direta
  com motivo; `Retry-After` respeitado; limite por canal/provedor.
- Duração e tentativa registradas; `correlation_id` ponta a ponta; requeue auditado.
- IA fora do fluxo crítico: `ai_request` como fila própria com timeout curto; o
  inbox responde com fallback determinístico e a resposta de IA segue depois.

### Fase 5 — PIX Woovi robusto
Riscos: R19, R20, R21.
- Cobrança persistida (`woovi_charge_id`, `woovi_expires_at`, `woovi_br_code`),
  reutilizada em re-render; BRL e precisão monetária validados.
- `dz23.woovi.event` persistido antes de processar, dedupe, worker, valor **obrigatório**
  e igual ao esperado, moeda, estados `active/pending/completed/expired/cancelled/refunded/error`,
  reconciliação periódica, eventos fora de ordem.

### Fase 6 — Mídia e templates
- `message_type`, `mime_type`, `media_id`, `attachment_id`, `file_size`, `sha256`,
  `caption`, `transcription`; MIME real + tamanho + extensões bloqueadas; anexos
  privados por empresa; imagem nunca vai para IA externa por padrão.
- Templates oficiais (Meta) e janela de 24 h: fora da janela só template aprovado.

### Fase 7 — Caixa de atendimento humano
Risco: R22, R23.
- `dz23.conversation` (estados `open/bot_active/human_active/waiting_customer/
  waiting_internal/resolved/blocked`), responsável, equipe, prioridade, SLA.
- Assumir/pausar bot/devolver ao bot/transferir; notas internas (nunca enviadas);
  envio humano pela outbox; busca por telefone/lead/pedido/texto; opt-out/bloqueio; auditoria.

### Fase 8 — Governança da IA
Riscos: R24, R25.
- Configuração por empresa; timeout por provedor; 429; circuit breaker; limite de
  mensagens consecutivas; handoff por baixa confiança/assunto sensível; horário;
  registro de modelo/versão do prompt/custo estimado; contexto por allowlist.

### Fase 9 — Observabilidade
- Logs estruturados sem PII; métricas por empresa/canal/provedor; tela de saúde
  (último webhook/envio/status, fila mais antiga, DLQ, conexão Evolution).

### Fase 10 — LGPD e retenção
Riscos: R26, R27.
- Retenção configurável de inbox/outbox/eventos/anexos com anonimização; exportação
  do titular; opt-out/bloqueio de marketing; auditoria de acesso; restrição de leitura
  de payloads a um grupo "Atendimento – supervisor".

### Fase 11 — Testes (transversal)
Cada fase entrega seus testes; aqui fecha a matriz do prompt §15 (webhooks,
mensagens, agente, PIX, multi-tenant, instalação/upgrade).

### Fase 12 — Documentação
README, ARCHITECTURE, CHANGELOG, SECURITY, docs/README, ADRs, matriz de integrações,
guias de webhook, recuperação de filas, backup/restore. Números de testes só com
evidência de execução no commit.

## Verificação por fase

```bash
python -m compileall -q addons_custom
python -m ruff check . && python -m ruff format --check .
bash scripts/smoke.sh          # instalação limpa + suíte dz23 em DB descartável
# upgrade no banco de dev (dados antigos):
docker exec docker-odoo-1 bash -c 'odoo -c /tmp/odoo.conf -d dz23crm -u <módulos> \
  --stop-after-init --workers=0 --http-port=8098'
```

## Fora de escopo sem autorização explícita
Push para `main`, release/tag, deploy em servidor, envio de mensagem real, cobrança
real, alteração de DNS/billing, rotação de credenciais de produção.
