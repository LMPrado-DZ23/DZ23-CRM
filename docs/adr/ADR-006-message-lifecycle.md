# ADR-006 — Ciclo de vida de mensagem e status monotônico

- **Status:** aceito (Fase 1, 2026-09-13)
- **Riscos tratados:** R07, R08

## Contexto

A outbox só sabia `pending/sent/failed/dead` do ponto de vista do **nosso** envio.
Os callbacks de status dos provedores (Meta `statuses`, Twilio `MessageStatus`,
Evolution `MESSAGES_UPDATE`) eram descartados; não havia como saber se a mensagem
foi entregue, lida ou rejeitada depois do aceite.

## Decisão

1. **`dz23.message.event`** (append-only): um registro por callback de status ou
   mensagem, com `provider_message_id`, direção, `status` normalizado,
   `provider_status` original, `occurred_at`/`received_at`, `payload_hash`,
   `payload_preview`, `error_code`, `error_message` sanitizada, `outbox_id`/`inbox_id`.
   Dedupe por `unique(channel_id, provider_message_id, message_direction, status)`:
   callback repetido é ignorado; status diferente é aceito.
2. **Status normalizados**: `queued, sent, delivered, read, failed, undelivered,
   expired, cancelled, unknown`.
3. **Monotonia por rank** aplicada em `current_status` da outbox, sob lock de linha:

   | status | rank | regra |
   |---|---|---|
   | unknown | 0 | nunca altera `current_status` |
   | queued | 10 | |
   | sent | 20 | |
   | delivered | 30 | |
   | read | 40 | |
   | failed / undelivered / expired / cancelled | 25 | aplica só se o atual for < 30 |

   Um evento atrasado nunca regride (`read` não volta para `sent`). Uma falha que
   chega depois de `delivered` não altera o status, mas **grava**
   `provider_error_code/message` e o evento fica no histórico.
4. **Datas por marco** (`sent_at`, `delivered_at`, `read_at`, `failed_at`) são
   gravadas uma única vez; `last_status_at` guarda o `occurred_at` mais recente.
5. **Correlação**: `correlation_id` (UUID) na outbox; `client_message_id` enviado ao
   provedor quando ele suporta (Twilio não; Meta `biz_opaque_callback_data`; Evolution não).

## Consequências

- Métricas de entrega/leitura por empresa/canal passam a ser calculáveis (Fase 9).
- Callbacks sem outbox correspondente (mensagem enviada fora do CRM) são gravados
  com `outbox_id` vazio e `status` normalizado — úteis para auditoria.

## Rollback

Modelo e colunas são aditivos; reverter o código não exige migração destrutiva.
