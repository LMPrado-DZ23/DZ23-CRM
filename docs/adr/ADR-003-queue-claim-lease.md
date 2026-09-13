# ADR-003 — Claim com lease nas filas inbox/outbox

- **Status:** aceito (Fase 1, 2026-09-13)
- **Riscos tratados:** R01, R02, R03, R04 (ver `docs/BASELINE_AUDIT.md`)

## Contexto

Os workers selecionavam 20 itens com `FOR UPDATE SKIP LOCKED` e processavam o lote
na mesma transação. Na inbox, uma chamada lenta de IA (até 120 s) somada a outras
estourava `limit_time_real` do worker de cron: rollback do lote inteiro, `attempts`
nunca incrementava e o lote era reprocessado para sempre. Na outbox, o
`cr.commit()` por item liberava os locks dos itens seguintes do lote.

## Decisão

1. **Claim atômico e curto**: `UPDATE … SET state='processing'|'sending', lease_until
   = agora + TTL, attempts = attempts + 1 WHERE id IN (SELECT … FOR UPDATE SKIP
   LOCKED LIMIT n) RETURNING id`, seguido de `commit`. O lock de linha dura
   milissegundos; o *lease* é quem protege o item enquanto é processado.
2. **Processamento item a item com commit** (fora de testes). Um item lento ou um
   worker morto afeta só aquele item.
3. **`attempts` incrementa no claim**, não no fim — um item que derruba o worker
   repetidamente chega à DLQ.
4. **Lease vencido** (`_cron_recover_leases`):
   - outbox com `provider_message_id` ⇒ `sent` (o provedor confirmou);
   - outbox sem id ⇒ volta para `failed` com o aviso "lease expirado — possível
     reenvio" (entrega *at-least-once*, documentada);
   - inbox ⇒ volta para `failed` (efeitos de negócio são idempotentes, ADR-005).
5. **Webhook**: falha ao persistir na inbox responde **HTTP 500** para o provedor
   reentregar (antes respondia 200 e perdia a mensagem). Payload JSON completo é
   armazenado (sem corte), com `payload_hash` (SHA-256) e `payload_preview`.

## Alternativas rejeitadas

- **Lote numa única transação** — é a causa do R04.
- **Redis/Celery/pg-boss** — fora da stack fixada (Odoo + Postgres).
- **Advisory lock por item** — some com a conexão e não sobrevive a um commit.

## Consequências

- Várias execuções simultâneas (cron + "Executar agora" + shell) não duplicam
  trabalho: o `SKIP LOCKED` do claim e o estado `sending/processing` excluem o item.
- A entrega continua *at-least-once* quando o worker morre entre o 2xx do provedor e
  o commit; mitigado por `client_message_id`/correlação (Fase 4) e registrado em log.

## Rollback

Reverter o commit da fase e rodar `-u dz23_whatsapp`. As colunas novas
(`lease_until`, `payload_hash`…) são aditivas; os estados antigos continuam válidos.
Itens em `sending`/`processing` devem ser devolvidos para `failed` antes do rollback:
`UPDATE dz23_message_outbox SET status='failed' WHERE status='sending';` (idem inbox).
