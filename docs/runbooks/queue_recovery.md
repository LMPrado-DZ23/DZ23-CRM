# Runbook — recuperação de filas

As filas vivem no PostgreSQL e são processadas por crons do Odoo com **claim + lease**
([ADR-003](../adr/ADR-003-queue-claim-lease.md)): cada item é reivindicado com
`FOR UPDATE SKIP LOCKED`, recebe um prazo (lease) e tem o nº de tentativas contado no
claim. Se o worker morrer, o lease vence e o item volta para a fila (ou vai para a DLQ
se esgotou as tentativas).

**Regra de ouro:** nunca apague linhas de fila para "destravar". Use os botões de
reprocessar (auditados) e, se precisar investigar no banco, faça só consultas de leitura.

## 1. Primeiro olhar: Saúde dos canais
Menu **DZ23 WhatsApp → Saúde dos canais**. Cada alerta aponta a fila:

| Alerta | Onde agir |
|---|---|
| Fila de entrada/saída atrasada ou parada | crons (§3) e item mais antigo (§2) |
| Itens na DLQ | §4 |
| Evolution desconectado | reconectar pelo QR no canal |
| Envio pausado pelo provedor (429) | aguardar `Envio pausado até` (§5) |
| Nenhum status de entrega desde o último envio | webhook de status ([webhooks.md](webhooks.md)) |

## 2. Filas e estados

| Fila | Menu | Estados | Tentativas | Observação |
|---|---|---|---|---|
| Entrada (`dz23.message.inbox`) | Inbox (recebidas) | pending → processing → done · failed (retry) · dead (DLQ) | 6 | processamento do robô/atendimento |
| Saída (`dz23.message.outbox`) | Outbox (respostas) | pending → sending → sent · failed · dead | 6 | `sent` = provedor aceitou; entrega/leitura em `Status atual` |
| Eventos de status (`dz23.message.event`) | Eventos de status | `processed` = aplicado à saída | cron reaplica | append-only |
| IA (`dz23.ai.request`) | Fila de IA | pending → processing → done · failed | 3 | sem botão de reprocessar: ao esgotar ou com IA indisponível a conversa é transferida para humano |
| Mídia (`dz23.message.media`) | Mídias recebidas | pending → processing → downloaded · failed · rejected · dead · expired | 5 | `rejected` = arquivo recusado (tipo/tamanho/host) |
| Woovi (`dz23.woovi.event`) | botão **Ver eventos Woovi** no provedor de pagamento Woovi | pending → done · ignored · rejected · failed | 5 | o webhook só dispara a releitura na API; a conciliação periódica corrige status perdido |

Motivos de DLQ (`dlq_reason`): **erro permanente** do provedor (ex.: número inválido,
template não aprovado), **tentativas esgotadas**, **lease expirado** no limite.

## 3. Crons
Menu **Configurações → Técnico → Ações agendadas** (requer o **modo desenvolvedor**
ativado), filtre por "DZ23":
entrada e saída (1 min), IA (1 min), mídia (1 min), reaplicar eventos de status,
SLA, métricas (1 h), retenção LGPD (diário), conciliação Woovi. Se uma fila está parada,
confira se o cron está **ativo** e a data da próxima execução; em produção com
`workers > 0`, confirme `max_cron_threads > 0`.

## 4. DLQ: investigar e reprocessar
1. Abra o item (a abertura fica na auditoria de acesso). Leia `Erro`, `Motivo DLQ` e,
   na saída, `Código/mensagem de erro do provedor`.
2. Corrija a causa:
   - credencial/token inválido → canal;
   - template não aprovado ou parâmetros errados → Templates oficiais;
   - fora da janela de 24 h → responder com template;
   - número inválido/bloqueado → não reprocessar;
   - bug de processamento na entrada → corrigir e atualizar o módulo.
3. Botão no item — **Reprocessar** (entrada), **Reenviar** (saída) ou **Tentar de novo**
   (mídia): volta para `pending` com tentativas zeradas. Na entrada e na saída a ação
   fica na auditoria de acesso.
4. **Atenção na saída**: reprocessar um item cujo provedor pode ter aceitado a mensagem
   (lease expirado sem id) pode gerar mensagem duplicada ao cliente — é a garantia
   *at-least-once* documentada.

Efeitos de negócio (orçamento, pedido, agendamento) são idempotentes: reprocessar a
entrada não os duplica ([ADR-005](../adr/ADR-005-business-actions-idempotentes.md)).

## 5. Pausa por limite do provedor (429)
Quando o provedor responde 429, o canal recebe `Envio pausado até` (mínimo 60 s ou o
`Retry-After`) e a saída desse canal não é reivindicada até lá; os demais canais seguem.
Não é preciso intervir. Para lotes grandes, ajuste
`dz23.whatsapp.outbox_per_channel_batch` (padrão 10 por execução).

## 6. Item preso em `processing`/`sending`
É normal durante o lease (entrada 10 min, saída 5 min, IA 5 min). Depois disso a
recuperação automática decide:
- **saída com id do provedor** → marcada como enviada (não reenvia);
- **saída sem id** → volta para retry (possível duplicata) ou DLQ;
- **entrada/IA** → retry ou DLQ/transferência.

Se ficar preso além do lease, o cron não está rodando (§3).

## 7. Consultas de diagnóstico (somente leitura)
```sql
-- volume por estado
SELECT status, count(*) FROM dz23_message_outbox GROUP BY status;
SELECT status, count(*) FROM dz23_message_inbox GROUP BY status;
-- item pendente mais antigo por canal
SELECT channel_id, min(create_date) FROM dz23_message_outbox
 WHERE status IN ('pending', 'sending', 'failed') GROUP BY channel_id;
-- leases vencidos
SELECT id, status, attempts, lease_until FROM dz23_message_outbox
 WHERE status = 'sending' AND lease_until < (now() AT TIME ZONE 'utc');
-- motivos da DLQ
SELECT dlq_reason, count(*) FROM dz23_message_outbox WHERE status = 'dead' GROUP BY dlq_reason;
```

## 8. Depois de uma queda longa
1. Confirme que o banco está íntegro e o Odoo sobe.
2. Deixe os crons drenarem a entrada antes da saída (a entrada gera respostas).
3. Mensagens de clientes que chegaram durante a queda: os provedores reentregam
   webhooks que receberam 5xx; o dedupe evita duplicidade.
4. Acompanhe "Saúde dos canais" e a métrica de idade da fila até normalizar.
5. Se restaurou backup, siga também [backup_restore.md](backup_restore.md) §4
   (desativar canais/crons antes para não reenviar mensagens antigas).
