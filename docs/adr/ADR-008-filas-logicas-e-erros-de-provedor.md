# ADR-008 — Filas lógicas e classificação de erros de provedor

- **Status:** aceito (Fase 4, 2026-09-13)
- **Riscos tratados:** R04 (IA no caminho crítico), R05 (4xx tratado como transitório)

## Contexto

A stack é fixa (Odoo + Postgres, sem broker). O worker da inbox chamava a IA de
forma síncrona (até 120 s), e qualquer erro de envio — inclusive um 400 definitivo
— consumia 6 tentativas antes da DLQ, ignorando `Retry-After` e rate limit.

## Decisão

### 1. Filas lógicas no Postgres (um estado + um worker por fila)

| Fila do prompt | Implementação | Worker |
|---|---|---|
| `inbox` | `dz23.message.inbox` (`pending → processing → done/failed/dead`) | cron inbox |
| `agent_decision` | processamento do item da inbox (regras determinísticas, rápido) | cron inbox |
| `ai_request` | **`dz23.ai.request`** (novo): conversa livre com a IA fora do item da inbox | cron ai_request |
| `business_effect` | `dz23.business.action` (idempotente, ADR-005) | síncrono no item |
| `outbox` | `dz23.message.outbox` (`pending → sending → sent/failed/dead`) | cron outbox |
| `status_event` | `dz23.message.event` (`processed`), reaplicação de pendentes | cron eventos |

Todas usam claim + lease (ADR-003). A inbox não espera a IA: a pergunta livre vira
um `dz23.ai.request`; se a IA falhar após as tentativas, o cliente recebe a resposta
determinística de fallback (nunca fica sem retorno).

### 2. Erros de provedor tipados

- `ProviderTransientError`: rede/timeout, HTTP 408, 425, 429 e 5xx. Respeita
  `Retry-After` (segundos ou data HTTP) como piso do próximo retry. **429** também
  marca `rate_limited_until` no canal: nenhum item daquele canal é reivindicado até lá.
- `ProviderPermanentError`: demais 4xx (payload inválido, número inexistente,
  janela de 24 h/template exigido, credencial revogada). Vai **direto para a DLQ**
  com `dlq_reason = permanent_error` e o código de erro do provedor.
- Mensagens de erro guardam só status HTTP + código do provedor (sem corpo, sem PII).

### 3. Concorrência por canal

Cada execução do worker reivindica no máximo `dz23.whatsapp.outbox_per_channel_batch`
(padrão 10) itens por canal — um canal em rajada não monopoliza o envio dos demais.

### 4. Consulta ao provedor antes de reenviar

Nenhum dos três provedores oferece consulta de mensagem por id do cliente. A
reconciliação possível é pela **Meta**: o `biz_opaque_callback_data`
(= `correlation_id` da outbox) volta no callback de status; se o item ainda não tem
`provider_message_id` (ack perdido), o callback grava o id e marca como enviado,
impedindo o reenvio. Twilio/Evolution: N/A (sem suporte) — entrega at-least-once.

## Consequências

- DLQ passa a ter motivo (`permanent_error`, `max_attempts`, `lease_expired`).
- Latência de resposta por IA fica desacoplada do throughput da inbox.
