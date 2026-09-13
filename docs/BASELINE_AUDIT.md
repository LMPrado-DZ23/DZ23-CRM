# DZ23 CRM — Auditoria de Baseline (Fase 0)

- **Data:** 2026-09-13
- **Commit analisado:** `fd04837` (`main`) — idêntico ao `main` de
  `github.com/LMPrado-DZ23/DZ23-CRM` e ao `DZ23-CRM-main.zip` (diferença só em
  `__pycache__` locais). **Existe uma única cópia do projeto**:
  `C:\Users\zodyp\DZ23-CRM`, montada no container `docker-odoo-1`.
- **Método:** leitura integral do código Python dos módulos (não da documentação),
  inspeção dos containers e execução das verificações disponíveis.
- Severidade: **CRÍTICO / ALTO / MÉDIO / BAIXO**. Veredito: **CONFIRMADO** (visto
  no código/execução) ou **PLAUSÍVEL** (depende de comportamento externo não reproduzido).

---

## 1. Ambiente e runtime

| Item | Estado observado |
|---|---|
| Odoo | `Odoo Server 19.0-20260817` (imagem oficial), container `docker-odoo-1` |
| Postgres | `docker-db-1` (postgres:16), volume `docker_dz23-db` |
| Montagens | `addons_custom → /mnt/extra-addons`, `addons_oca → /mnt/oca-addons`, `docker/odoo.conf` |
| Config | `workers=2`, `max_cron_threads=1`, `limit_time_real=240`, `proxy_mode=True`, `dbfilter=^dz23crm$` |
| WhatsApp | `dz23evo-evolution-api-1` + `dz23evo-evo-db-1` + `dz23evo-evo-redis-1` em execução |
| IA local | `dz23-ollama` **parado** (Exited há 7 dias) e fora do compose → agente cai no fallback |
| Segredos | `.env` fora do Git; nenhum valor lido nesta auditoria |

## 2. Módulos e dependências

| Módulo | Versão | Depende de | Papel real (confirmado no código) |
|---|---|---|---|
| `dz23_branding` | — | web, mail, portal | Debrand, pt-BR, PWA |
| `dz23_brasil_tools` | — | contacts, crm, base_geolocalize | CNPJ/CEP/feriados/câmbio via BrasilAPI |
| `dz23_crm` | — | crm, mail | Link wa.me no lead |
| `dz23_whatsapp` | 19.0.7.0.0 | mail, phone_validation, sales_team | `dz23.channel`, contato por canal, inbox/outbox, webhooks Meta/Evolution, envio Meta/Twilio/Evolution |
| `dz23_agent` | 19.0.1.0.0 | dz23_whatsapp, dz23_ai, crm, calendar, phone_validation, sale_management | Sobrescreve `dz23.channel.handle_inbound` (agenda/compra/preço/IA) |
| `dz23_ai` | — | base, mail | `dz23.ai.chat` (Ollama/Groq/OpenAI/Gemini/Anthropic), gate de IA externa + redação regex |
| `dz23_payment_woovi` | — | payment | Provider PIX, webhook RSA |
| `dz23_fiscal` | — | account | NF-e fail-closed (sem emissão real) |
| `dz23_integrations` | — | base | Catálogo de integrações |

## 3. Fluxo inbound (atual)

1. Provedor chama `POST /dz23/whatsapp/<provider>/webhook/<token>`.
2. `_resolve_by_token` acha o canal (sudo); autenticação por canal (fail-closed).
3. Parser extrai **apenas (número, texto)**: Evolution `conversation`/`extendedTextMessage`
   (ignora `fromMe`); Meta só `entry[0].changes[0].value.messages[0]`.
4. `dz23.message.inbox._enqueue`: dedupe por `(provider, channel_id, message_id)`
   (id do provedor ou hash do envelope), grava `payload = json.dumps(...)[:20000]`.
5. Resposta `200 ok` — **também quando o enqueue falha** (exceção engolida).
6. `ir.cron` (1 min) seleciona até 20 itens `FOR UPDATE SKIP LOCKED` e processa o
   lote **na mesma transação**, cada item em savepoint.

## 4. Fluxo de decisão do agente (`dz23_agent`)

Executa como usuário técnico `dz23_whatsapp_bot`, `with_company(canal.company_id)`.

1. `dz23.channel.contact._get_or_create` → `crm.lead` (escopo do canal).
2. Posta a mensagem no chatter do lead.
3. Roteamento por regex, nesta ordem: `_SCHED_RE` (agenda) → `_BUY_RE` (compra) →
   `_PRICE_RE` (preço) → IA.
   - **Agenda:** exige data **e** hora explícitas; rejeita passado; checa conflito
     com eventos cuja `opportunity_id.company_id` = empresa; cria `calendar.event` de 60 min.
   - **Compra:** 1 produto casado → `sale.order` rascunho (qtd 1) **a cada mensagem**.
   - **Preço:** responde `list_price`; não cria pedido.
   - **IA:** `dz23.ai.chat` síncrono (timeout 120 s) com prompt + catálogo (+ histórico
     do chatter só se provedor = ollama); fallback fixo se falhar.
4. Resposta enfileirada em `dz23.message.outbox`.

## 5. Fluxo outbound

1. `ir.cron` (1 min) seleciona até 20 itens `FOR UPDATE SKIP LOCKED`.
2. `send_text` (sudo, credenciais do canal) → Evolution `/message/sendText`,
   Meta `/{phone_id}/messages`, Twilio `Messages.json`. Sucesso exige id no corpo.
3. `status='sent'` + `provider_message_id`; `cr.commit()` por registro (fora de teste).
4. Falha (qualquer tipo) → retry exponencial com jitter, DLQ em 6 tentativas.
5. **Não há** status `delivered/read/failed` posterior, nem `StatusCallback` Twilio.
6. Envio manual (`dz23.whatsapp.compose`) é **síncrono** e não passa pela outbox.

## 6. Fluxo PIX (Woovi)

1. `_get_specific_rendering_values` faz `POST /charge` (`correlationID = reference`)
   **toda vez** que a página de pagamento é renderizada; nada é persistido além do retorno.
2. Webhook `POST /payment/woovi/webhook`: teto 1 MiB, RSA-SHA256 com chave pública em
   `ir.config_parameter` (global); processa **síncrono** via `_process`.
3. `COMPLETED/PAID/CONFIRMED` → `_set_done` se `value` ausente **ou** igual ao esperado;
   `ACTIVE/PENDING` → pendente; `EXPIRED` → cancelado; demais → erro.

## 7. Pontos de entrada HTTP

| Rota | Método | Auth | Respostas | Persistência |
|---|---|---|---|---|
| `/dz23/whatsapp/evolution/webhook/<token>` | POST | `X-DZ23-Callback` por canal | 404/503/401/413/400/409/200 | inbox |
| `/dz23/whatsapp/meta/webhook/<token>` | GET | `hub.verify_token` | 404/403/challenge | — |
| `/dz23/whatsapp/meta/webhook/<token>` | POST | HMAC-SHA256 corpo bruto | 404/503/413/401/400/200 | inbox |
| `/payment/woovi/webhook` | POST | RSA-SHA256 corpo bruto | 413/401/200 | nenhuma (processa direto) |
| Twilio | — | — | **inexistente** | — |

## 8. Modelos de dados relevantes

| Modelo | Chaves/índices | Observações |
|---|---|---|
| `dz23.channel` | unique `webhook_token`; unique `(provider, provider_channel_id)`; `company_id` idx | Segredos `groups=base.group_system` |
| `dz23.channel.contact` | unique `(channel_id, provider_user_id)` | `lead_id` adicionado por `dz23_agent` |
| `dz23.message.inbox` | unique `(provider, channel_id, message_id)`; `status`, `next_attempt_at` idx | payload truncado; sem hash/preview |
| `dz23.message.outbox` | `status`, `next_attempt_at` idx | sem ciclo de status, sem correlation/client id |
| `payment.transaction` (herdado) | — | sem id da cobrança Woovi persistido |

## 9. Regras de segurança existentes

- Record rules globais por empresa em channel, contact, inbox e outbox.
- ACL: `base.group_user` lê canal/inbox/outbox (sem escrita); `base.group_system` total.
- Segredos de canal e `woovi_app_id` só para `base.group_system`.
- Webhooks comparam segredos com `hmac.compare_digest`; limite de corpo 1 MiB.
- IA externa desligada por padrão (`dz23.ai.external_allowed`), PII regex redigida,
  imagem bloqueada para externo, chave Gemini no header.

## 10. Testes existentes

41 funções de teste (tag `dz23`):

| Arquivo | Qtde | Cobre |
|---|---|---|
| `dz23_agent/tests/test_agent.py` | 11 | agenda (sem hora, sem data, passado, inválida, cria, conflito, isolamento), preço, compra única/ambígua, acento |
| `dz23_ai/tests/test_ai_privacy.py` | 5 | gate externo, redação, imagem |
| `dz23_whatsapp/tests/test_webhook_auth.py` | 9 | 404/401/400/409/503/200, isolamento de segredo |
| `dz23_whatsapp/tests/test_tenancy.py` | 5 | visibilidade/leitura cross-company, unicidade |
| `dz23_whatsapp/tests/test_outbox.py` | 7 | enqueue, sucesso, retry→DLQ, cron não reenvia, requeue |
| `dz23_whatsapp/tests/test_inbox.py` | 4 | dedupe sequencial, reprocesso de item `done`, retry→DLQ, requeue |

**Lacunas de teste:** Meta HMAC válido/inválido, payload grande, falha de enqueue,
evento duplicado concorrente, status monotônico, Twilio, compra **repetida**, dupla
reserva concorrente, fuso/feriado/expediente, todo o módulo Woovi (0 testes), fiscal,
brasil_tools, upgrade com dados antigos.

## 11. Riscos confirmados

| ID | Sev. | Veredito | Evidência | Cenário de falha |
|---|---|---|---|---|
| R01 | MÉDIO | CONFIRMADO | `message_outbox.py:94-103` | `cr.commit()` por item libera os `FOR UPDATE` do lote. O lock do `ir.cron` (`_acquire_one_job` com `FOR NO KEY UPDATE SKIP LOCKED`, usado também por `method_direct_trigger`) impede duas execuções do **mesmo job**, mas qualquer chamada fora do cron (ação manual, shell, segundo job) pode reenviar itens ainda `pending`. Queda entre o 2xx do provedor e o commit → reenvio (at-least-once) |
| R02 | ALTO | CONFIRMADO | `message_inbox.py:79` | `json.dumps(payload)[:20000]` gera JSON inválido para envelopes grandes (mídia/lote) → `json.loads` falha em `_process_one` → 6 tentativas → **DLQ**, mensagem do cliente nunca atendida |
| R03 | ALTO | CONFIRMADO | `message_inbox.py:85-89`, `controllers/main.py:71-75,131-135` | Erro de banco no enqueue é engolido e o webhook devolve `200 ok` → provedor não reentrega → **perda silenciosa** |
| R04 | ALTO | CONFIRMADO (mecanismo) | `message_inbox.py:109-110`, `ai_service.py:17`, `odoo.conf:17`; Odoo `service/server.py`: `limit_time_real_cron=-1` ⇒ timeout do worker de cron = `limit_time_real` | Lote de 20 itens numa transação com IA de até 120 s cada excede `limit_time_real=240` → worker morto → rollback de tudo, **`attempts` nunca incrementa** → lote venenoso reprocessado para sempre, sem DLQ |
| R05 | MÉDIO | CONFIRMADO | `whatsapp_channel.py:257-265`, `message_outbox.py:120-141` | 4xx permanente (número inválido, template exigido) é tratado como transitório: 6 tentativas inúteis; `Retry-After`/429 ignorados |
| R06 | ALTO | CONFIRMADO | `whatsapp_service.py:33-37,53-55` | Webhook Meta com várias mensagens/changes processa só a primeira → **demais mensagens perdidas** |
| R07 | MÉDIO | CONFIRMADO | `whatsapp_service.py:30-39`, `controllers/main.py:127-135` | `statuses` da Meta descartados; não existe ciclo sent→delivered→read nem registro de falha pós-envio |
| R08 | MÉDIO | CONFIRMADO | `message_outbox.py:35-58` | Outbox sem `current_status`, datas de status, `correlation_id`, `client_message_id` |
| R09 | MÉDIO | CONFIRMADO | ausência de rota; `whatsapp_channel.py:305-321` | Twilio: sem webhook de entrada, sem assinatura, sem `StatusCallback` — README anuncia Twilio como canal completo |
| R10 | ALTO | PLAUSÍVEL | `whatsapp_service.py:97-101` | Evolution: `remoteJid` de grupo (`@g.us`) e `status@broadcast` não são filtrados → lead falso e resposta do bot em grupo/status |
| R11 | MÉDIO | CONFIRMADO | `whatsapp_service.py:100`, `controllers/main.py:67` | Mídia (imagem/áudio/documento/localização) sem texto é ignorada e respondida `200` — cliente fica sem atendimento, nada registrado |
| R12 | ALTO | CONFIRMADO | `whatsapp_agent.py:333-343,243-258` | "quero comprar X" repetido (novo message_id) cria **um `sale.order` novo por mensagem** |
| R13 | ALTO | CONFIRMADO | `whatsapp_agent.py:322-324,189-205` | Agenda faz *consultar-depois-inserir* sem lock → dois workers reservam o mesmo horário; ignora eventos manuais sem oportunidade; duração fixa 60 min; sem expediente/feriado/responsável |
| R14 | MÉDIO | CONFIRMADO | `whatsapp_agent.py:30` | `_SCHED_RE` casa "atend", "marc" (ex.: "atendimento", "marca") → conversa geral cai no fluxo de agenda |
| R15 | MÉDIO | CONFIRMADO | `whatsapp_agent.py:339-342,355-359` | Resposta cita `list_price`, mas o orçamento aplica pricelist/descontos → valor informado pode divergir do pedido |
| R16 | ALTO | CONFIRMADO | `whatsapp_channel.py:324-335` | Com `agent_autoreply` desligado, a mensagem **não cria lead nem aparece para humanos** (só log) — não há atendimento humano |
| R17 | MÉDIO | CONFIRMADO | `wizard/whatsapp_compose.py:14-25` | Envio humano síncrono, fora da outbox: sem retry, sem status, sem auditoria |
| R18 | MÉDIO | CONFIRMADO | `whatsapp_channel.py:328-334` | Log de inbound grava número completo e 80 caracteres do texto (PII em log) |
| R19 | ALTO | CONFIRMADO | `payment_transaction.py:13-34` | Cada renderização da página de pagamento cria **nova cobrança PIX** |
| R20 | ALTO | CONFIRMADO | `payment_transaction.py:53-61` | `COMPLETED` **sem** `value` confirma o pagamento (`paid is None → _set_done`) |
| R21 | MÉDIO | CONFIRMADO | `controllers/main.py:55-65` (woovi) | Evento não persistido nem deduplicado, processamento síncrono, sem checagem de moeda, `REFUND` vira erro, chave pública global (não por empresa/provider) |
| R22 | MÉDIO | CONFIRMADO | `ir.model.access.csv:8,10` | Todo usuário interno da empresa lê texto/payload de todas as conversas (inbox/outbox) |
| R23 | BAIXO | CONFIRMADO | `evolution` `events: ["MESSAGES_UPSERT"]` | Estado de conexão Evolution não é monitorado |
| R24 | MÉDIO | CONFIRMADO | `ai_service.py:50-71` | Provedor, modelo, chaves e consentimento de IA são **globais ao banco**, não por empresa |
| R25 | MÉDIO | CONFIRMADO | `ai_service.py:73-83` | Sem tratamento de 429, circuit breaker, registro de modelo/prompt/custo; timeout único de 120 s |
| R26 | MÉDIO | CONFIRMADO | ausência de cron | Sem retenção/anonimização de inbox, outbox e chatter |
| R27 | BAIXO | CONFIRMADO | `ai_service.py:25-43` | Redação só por regex (não cobre nome/endereço); contexto não usa allowlist |

## 12. Incompatibilidades documentação × código

| Documento | Afirmação | Realidade no código |
|---|---|---|
| `README.md` | "Testes 33/33" | 41 funções de teste |
| `audit/QUALITY_SCORECARD.md` | "40/40" e "smoke 41/41" | contagens divergentes no mesmo arquivo |
| `audit/REAUDIT_2026-09-08.md` | "39/39" | idem |
| `CHANGELOG.md`, `QUALITY_SCORECARD.md` | "envio **exatamente-uma-vez**" | outbox é *at-least-once* (o próprio cabeçalho de `message_outbox.py` e o README dizem isso) |
| `README.md` | agente "não duplica pedido" | compra repetida duplica (R12) |
| `README.md` | WhatsApp plugável "Meta Cloud / Twilio / Evolution" | Twilio só envia (R09) |
| `ARCHITECTURE.md` §3 | "Todos `license: 'LGPL-3'`" | manifests usam `Other OSI approved licence` (MIT) |
| `ARCHITECTURE.md` §3 | `dz23_whatsapp` depende de "base, mail, crm" | depende de mail, phone_validation, sales_team |
| `ARCHITECTURE.md` §4 | agente herda `dz23.whatsapp` e sobrescreve `_on_inbound`; funções `_agent_parse_datetime`/`_agent_match_product`; compra "responde via IA" | herda `dz23.channel.handle_inbound`; funções `_agent_parse_when`/`_agent_match_products`; resposta determinística |
| `ARCHITECTURE.md` §4/§7 | config via `ir.config_parameter`; IA `llama3.2:1b` | credenciais/prompt por canal; padrão `llama3.2:3b` |
| `ARCHITECTURE.md` §9 | "hoje a verificação é via shell E2E manual" | existe suíte automatizada + CI |
| `dz23_whatsapp/__manifest__.py` | "Credenciais ficam em ir.config_parameter" | ficam no `dz23.channel` |
| `docs/README.md` | estrutura "Fase 1" e comando de instalação sem `dz23_agent`/`dz23_integrations` | desatualizado |
| `README.md` | badge de CI em `lmpradodz23-design/DZ23-CRM` | repositório atual `LMPrado-DZ23/DZ23-CRM` (redireciona) |

## 13. Comandos de verificação disponíveis

| Comando | Onde | Observação |
|---|---|---|
| `python -m compileall -q addons_custom` | host | — |
| `python -m ruff check .` / `ruff format --check .` | host | CI fixa ruff 0.6.9 |
| `bash scripts/smoke.sh` | host com Docker | instala os 9 módulos num DB descartável e roda `--test-tags dz23` |
| `bash docker/verify.sh` | host com Docker | checagem de debrand — **instala módulos no banco `dz23crm`** (não é somente leitura) |
| Suíte do CI (`.github/workflows/ci.yml`) | GitHub Actions | lint, gitleaks, bandit, semgrep, trivy, SBOM, testes Odoo |

## 14. Resultado das verificações neste baseline

| Verificação | Resultado | Evidência |
|---|---|---|
| `python -m compileall -q addons_custom` | **PASS** (exit 0) | host Windows, Python 3.14 |
| `python -m ruff check .` | **PASS** — "All checks passed!" | ruff 0.16.4 (CI usa 0.6.9) |
| `python -m ruff format --check .` | **PASS** — "82 files already formatted" | idem |
| `bash scripts/smoke.sh` | **PASS** — `Modules loaded.` + `0 failed, 0 error(s) of 41 tests` | DB descartável `dz23_smoke_1367` (removido ao fim), 2026-09-13 14:18–14:20 |
| `bash docker/verify.sh` | ver §14.1 | — |
| Suíte Odoo do CI | NOT RUN: GitHub Actions não executado localmente (a suíte `dz23` equivalente rodou via smoke) | — |

| `gitleaks detect` | NOT RUN: comando indisponível no ambiente (nem Windows nem WSL) | roda no CI |

### 14.1 `docker/verify.sh` — NÃO executado (decisão consciente)

`verify.sh` **não é somente leitura**: executa `fetch_oca.sh`, `docker compose up` e
`odoo -d dz23crm -i base,crm,...` contra o **banco de desenvolvimento em uso**
(`dz23crm`, com o canal WhatsApp conectado). Rodá-lo altera dados reais do ambiente.
No lugar, foi feita a checagem equivalente somente leitura:

| Checagem (runtime vivo, `127.0.0.1:8069`) | Resultado |
|---|---|
| `GET /web/login` | HTTP 200, `<title>Login \| DZ23 CRM</title>` |
| Login contém "Powered by Odoo" | 0 ocorrências |
| `GET /web/manifest.webmanifest` | `"name": "DZ23 CRM"` |

**Conclusão do baseline:** o que existe está íntegro (instala do zero, 41/41 verdes,
lint limpo). Os riscos da §11 não aparecem nos testes porque **não há teste para
eles** — são lacunas de cobertura, não regressões.
