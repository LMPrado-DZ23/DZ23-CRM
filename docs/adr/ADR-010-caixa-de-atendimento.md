# ADR-010 — Caixa de atendimento humano (conversas)

- **Status:** aceito (Fase 7, 2026-09-13)
- **Riscos tratados:** R16 (bot desligado = mensagem invisível), R17 (envio humano
  síncrono e fora da outbox)

## Contexto

Não havia onde um atendente visse a conversa, assumisse o atendimento ou pausasse o
robô. Com `agent_autoreply` desligado a mensagem nem criava lead. O envio manual
(`dz23.whatsapp.compose`) chamava o provedor de forma síncrona, sem retry nem status.

## Decisão

1. **`dz23.conversation`** (uma por contato de canal = `dz23.channel.contact`), com
   `mail.thread` para trilha de auditoria (tracking de estado/responsável/equipe) e
   **notas internas** (subtipo nota do chatter — nunca enviadas ao provedor).
2. **Estados**: `open`, `bot_active`, `human_active`, `waiting_customer`,
   `waiting_internal`, `resolved`, `blocked`. Mensagem nova do cliente reabre
   conversas `resolved`/`waiting_customer`; `blocked` nunca reabre.
3. **Ações**: assumir (vira `human_active`, pausa o bot, responsável = usuário),
   devolver ao bot (`bot_active`), transferir para equipe/responsável, resolver,
   bloquear/opt-out (sem respostas automáticas, sem envio de marketing).
4. **O robô respeita a conversa**: em `human_active`, `waiting_internal` ou `blocked`
   o agente só registra a mensagem — não responde, não cria pedido, não chama IA.
   Com o bot desligado no canal, a mensagem continua visível na conversa e no lead.
5. **Histórico unificado**: mensagens recebidas (inbox) e enviadas (outbox, com status
   de entrega/leitura/falha) na mesma conversa, com anexos.
6. **Envio humano pela outbox** (retry, DLQ, status, janela de 24 h): o assistente de
   resposta enfileira texto livre (dentro da janela) ou template aprovado.
7. **SLA e prioridade**: `last_customer_message_at`, `first_response_due_at` (SLA em
   minutos por canal), `sla_breached` calculado; prioridade 0–3.
8. **Busca**: telefone, lead, pedido de venda e texto das mensagens.
9. **Acesso**: grupo **"DZ23 Atendimento / Atendente"** vê e atende as conversas (e
   lê as mensagens) da própria empresa; **"Supervisor"** também pode excluir
   conversas. As filas técnicas (inbox/outbox/eventos/mídia) continuam restritas a
   administradores no menu. Usuário interno comum **não** lê mensagens de WhatsApp
   (antes qualquer usuário lia — R22). Isolamento por empresa via record rule.

## Consequências

- Todo atendimento (bot ou humano) passa pela mesma trilha auditável.
- Opt-out bloqueia envios proativos (templates) para aquele contato.
