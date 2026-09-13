# ADR-013 — LGPD: retenção, pedidos do titular e auditoria de acesso

- Status: aceito
- Data: 2026-09-13
- Módulos: `dz23_whatsapp` 19.0.15.0.0, `dz23_agent` 19.0.7.0.0
- Documento de operação: [`docs/LGPD.md`](../LGPD.md)

## Contexto
Mensagens de WhatsApp, anexos, notas automáticas no lead e textos enviados à IA
guardavam dados pessoais sem prazo. Não havia como atender pedido de acesso ou de
eliminação de um titular, nem saber quem abriu uma conversa.

## Decisão
1. **Prazos por empresa** (Ajustes): mensagens finalizadas 365 dias, prévia/erro dos
   eventos de status 180, auditoria de acesso 730; anexos 180 (parâmetro existente);
   textos da fila de IA 90 e registro de uso de IA 400 (ADR-011). `0` desliga.
2. **Anonimizar em vez de apagar** mensagens: o registro fica (métricas, dedupe,
   trilha), mas texto, legenda, payload, prévia e resposta saem, e o telefone vira
   um **pseudônimo HMAC-SHA256 por empresa** (chave = `database.secret` + empresa).
   Só itens finalizados (`done/dead` na entrada, `sent/dead` na saída); nada na fila é
   tocado. Eventos de status continuam append-only: só o contexto de retenção pode
   **limpar** (nunca reescrever) prévia e mensagem de erro.
3. **Pedido do titular** (supervisor/administrador, por telefone e empresa):
   - *Exportar*: JSON com contatos, conversa, mensagens, status, metadados de anexos,
     leads, pedidos (referência) e pedidos de IA — anexo privado que expira em 7 dias.
   - *Anonimizar* (exige motivo/protocolo, confirmação): textos e anexos removidos,
     envios pendentes cancelados, notas da conversa e do lead limpas, lead arquivado
     sem dados de contato, textos de IA removidos, contato pseudonimizado, conversa
     bloqueada e **supressão** registrada. **Pedidos, faturas e pagamentos não são
     alterados** (obrigação fiscal/contábil — base legal art. 16, I, LGPD).
4. **Supressão** (`dz23.privacy.suppression`, só pseudônimo): criada por opt-out ou
   anonimização; se o titular voltar a escrever, a nova conversa nasce com opt-out
   (sem templates/marketing), mas o atendimento que ele iniciou continua possível.
5. **Auditoria de acesso** append-only (`dz23.access.log`): abrir conversa/mensagem
   no formulário, exportar, anonimizar, reenfileirar DLQ e alterar credencial do canal
   (só nomes dos campos). Leitura só por administrador; isolada por empresa.

## Consequências e limitações assumidas
- Anonimização por retenção e pedido do titular **não alcança backups**: o runbook
  [`backup_restore.md`](../runbooks/backup_restore.md) define rotação e o
  procedimento de reaplicar anonimizações após uma restauração.
- O pseudônimo HMAC usa o segredo do banco: é **pseudonimização** (reversível por força
  bruta para quem tem banco + segredo), não anonimização irreversível. Uma chave fora do
  banco (cofre) é a evolução natural.
- Auditoria final (B-2/B-3): a anonimização passou a remover também a cópia do anexo no
  lead, prévias dos eventos de status, valores rastreados e telefone/e-mail do cliente.
- A auditoria é append-only no ORM, mas um DBA ainda pode alterá-la; trilha
  imutável (WORM/assinada) exige infraestrutura externa e fica fora desta fase.
- A sincronização padrão do CRM pode limpar telefone/e-mail do parceiro vinculado ao
  anonimizar o lead; nome e dados fiscais do parceiro permanecem.
- Listas (`web_search_read`) não geram registro de auditoria — só a abertura de um
  registro; isso evita ruído, mas não prova quem viu uma listagem.
- Revisão DZ23 MCP (security): adotado HMAC por empresa em vez de sal global e
  expiração do arquivo exportado; registrados como limitação o log WORM e os backups.
