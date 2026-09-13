# Changelog

Formato baseado em [Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/).
Este projeto usa versionamento por módulo (Odoo `19.0.x.y.z`).

## [Não lançado]

### Fase 7 — Caixa de atendimento humano (`dz23_whatsapp` 19.0.13.0.0, `dz23_agent` 19.0.4.0.0)
- **Novo app "Atendimento"** (`dz23.conversation`, ADR-010): uma conversa por contato,
  kanban por estado (`open/bot_active/human_active/waiting_customer/waiting_internal/
  resolved/blocked`), responsável, equipe, prioridade e trilha de auditoria (chatter
  com tracking).
- **Ações**: assumir (pausa o robô), devolver ao robô, transferir (equipe/responsável
  + nota interna), aguardar cliente/interno, resolver, bloquear/desbloquear.
- **Notas internas** ficam no chatter e nunca vão ao cliente; **resposta do atendente
  pela outbox** (retry, DLQ, status, janela de 24 h — fora dela só template).
- **Histórico unificado** de recebidas/enviadas com status de entrega/leitura, pedidos
  de venda do cliente e lead.
- **SLA de 1ª resposta** por canal com cron que marca atraso; **busca** por telefone,
  lead, pedido e texto das mensagens.
- **Robô respeita o atendimento**: com humano no controle (ou bloqueado) só registra;
  com o robô desligado a mensagem continua visível no lead (corrige R16); "SAIR"
  aplica **opt-out** e bloqueia envios proativos; contato bloqueado não recebe nada.
- **Grupos Atendente/Supervisor**; usuário interno comum deixa de ler mensagens de
  WhatsApp (corrige R22).
- Envio manual legado (`dz23.whatsapp.compose`) passa pela outbox (corrige R17).

### Fase 6 — Mídia e templates oficiais (`dz23_whatsapp` 19.0.12.0.0)
- **Mídia recebida** (`dz23.message.media`, ADR-009): worker de download próprio por
  provedor (Meta em 2 etapas, Evolution base64, Twilio com basic auth), **MIME real
  pelo conteúdo**, extensões/MIME perigosos bloqueados (executáveis, scripts, HTML,
  SVG), limite de tamanho em streaming, **sha256** conferido, **allowlist anti-SSRF**
  e validação de `media_id` contra path injection.
- Anexo **privado** da empresa do canal; cópia vai para o chatter do lead; retenção
  configurável remove arquivos vencidos (`dz23.whatsapp.media_retention_days`).
- **Templates oficiais** (`dz23.message.template`): cadastro por canal, variáveis,
  status, sincronização da Meta (`meta_waba_id`); envio Meta (components), Twilio
  (`ContentSid`/`ContentVariables`) e Evolution (texto renderizado).
- **Janela de 24 h**: `last_inbound_at` por contato; texto livre fora da janela em
  Meta/Twilio vai para a DLQ como erro permanente — só template aprovado sai.

### Fase 5 — PIX Woovi robusto (`dz23_payment_woovi` 19.0.2.0.0)
- **Cobrança idempotente**: criada uma única vez por transação (lock de linha + id
  persistido) e **reutilizada ao recarregar a página**; `correlationID` = referência;
  só **BRL** e valor > 0; resposta divergente da Woovi vira erro; expiração registrada;
  cobrança vencida não é recriada (orienta novo pagamento).
- **Eventos persistidos antes de processar** (`dz23.woovi.event`): dedupe pelo hash
  do corpo, processamento imediato em savepoint e retry por cron; estados
  `pending/done/ignored/rejected/failed` auditáveis.
- **Confirmação exige valor recebido** (`pix.value`/`charge.value`); valor ou moeda
  divergente => transação em erro; evento sem valor não confirma.
- **Monotonia**: `active/pending < expired/cancelled < completed < refunded`; evento
  atrasado (EXPIRED depois de COMPLETED) é ignorado; estorno registrado.
- **Conciliação periódica** (10 min): consulta cobranças em aberto e aplica o status
  real (eventos perdidos).
- Webhook responde 500 se não conseguir persistir (a Woovi reenvia).
- Testes: +20 (render repetido, moeda, valor, duplicado, fora de ordem, expiração,
  estorno, conciliação, retry e webhook HTTP com assinatura RSA real de teste).

### Fase 4 — Outbox robusta e filas lógicas (`dz23_whatsapp` 19.0.11.0.0, `dz23_agent` 19.0.3.0.0)
- **Erros de provedor tipados** (ADR-008): transitório (rede, 408/425/429/5xx e códigos
  de rate limit Meta/Twilio) volta para retry com piso de `Retry-After`; permanente
  (demais 4xx, canal sem credencial, número inválido) vai **direto para a DLQ**.
- **429 pausa o canal** (`rate_limited_until`): nenhum item daquele canal é enviado até
  o fim da janela; **limite de itens por canal** por execução do worker
  (`dz23.whatsapp.outbox_per_channel_batch`, padrão 10).
- **DLQ com motivo** (`permanent_error`, `max_attempts`, `lease_expired`) em inbox e
  outbox; reenfileirar limpa o motivo.
- **Correlação ponta a ponta**: a outbox envia `correlation_id` (Meta
  `biz_opaque_callback_data`); callback de status com esse id **reconcilia ack
  perdido** (grava o id do provedor e evita reenvio).
- **Fila `status_event`**: evento persistido que falhar ao aplicar fica pendente e é
  reaplicado por cron (5 min).
- **Fila `ai_request`** (`dz23.ai.request`): a conversa livre com a IA saiu do item da
  inbox; worker com claim/lease, até 3 tentativas e **fallback determinístico** no
  fim; registra provedor, modelo, versão do prompt e duração. IA externa sem
  consentimento responde o fallback na hora. Tela "Fila de IA".

### Fase 3 — Idempotência de efeitos de negócio (`dz23_whatsapp` 19.0.10.0.0, `dz23_agent` 19.0.2.0.0)
- **Novo** `dz23.business.action` (ADR-005): efeito executa uma vez por
  `idempotency_key`; retry/mensagem repetida/worker concorrente devolvem o mesmo
  alvo; falha não registra (retry executa de novo). Tela "Ações de negócio".
- **Compra em etapas**: produto → variação (pergunta quando ambígua) → quantidade →
  resumo com **preço da lista de preços do Odoo** → confirmação explícita (SIM/NÃO,
  expira em 30 min) → orçamento idempotente. Mesma intenção reutiliza o orçamento
  aberto (ajusta quantidade). Pedido carrega canal e mensagem de origem. A IA não
  decide preço nem cria pedido.
- **Agenda sem dupla reserva** (ADR-004): `pg_advisory_xact_lock` por
  empresa/responsável, conflito com toda a agenda do responsável (inclusive eventos
  manuais), expediente e feriados via `resource.calendar`, fuso da empresa, duração
  e intervalo configuráveis por canal, **cancelar** (arquiva) e **remarcar**.
- Regex de agenda não captura mais "atendimento"/"marca".
- Odoo 19: domínios com `odoo.fields.Domain` (sem `odoo.osv` depreciado).
- Testes: +21 (idempotência com cursores concorrentes, compra repetida,
  lista de preços, variações, expediente, feriado, fuso, intervalo, agenda manual,
  cancelar/remarcar, reserva concorrente). Suíte `dz23`: 117/117.

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
