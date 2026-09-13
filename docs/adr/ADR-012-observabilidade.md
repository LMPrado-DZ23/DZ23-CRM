# ADR-012 — Observabilidade: saúde do canal, métricas diárias e logs estruturados

- Status: aceito
- Data: 2026-09-13
- Módulos: `dz23_whatsapp` 19.0.14.0.0, `dz23_agent` 19.0.6.0.0, `dz23_brasil_tools`

## Contexto
O operador não tinha como responder "o WhatsApp está funcionando?" sem abrir as filas
uma a uma, nem medir entrega, leitura, 1ª resposta ou conversão. Parte dos logs ainda
podia carregar dado pessoal (URL com CNPJ/CEP, texto de exceção).

## Decisão
1. **Saúde do canal** (campos calculados na leitura, só administradores; nada é
   gravado): último webhook, última mensagem recebida, último envio aceito, último
   status confirmado, item pendente mais antigo de entrada e saída, contagens de
   pendentes/DLQ/mídias com falha, conexão Evolution e pausa por 429. A regra
   `evaluate_health` é uma função pura:
   - **crítico**: fila parada ≥ 30 min; Evolution desconectado;
   - **atenção**: fila atrasada ≥ 5 min; item na DLQ; estado Evolution desconhecido;
     canal pausado por 429; envio há mais de 30 min (nas últimas 24 h) sem nenhum
     status de entrega depois dele — sintoma clássico de webhook de status mal
     configurado.
   Tela "Saúde dos canais" e aba "Saúde" no canal.
2. **Métricas diárias** (`dz23.metrics.daily`, um registro por canal e dia no fuso da
   empresa; cron de hora em hora recalcula hoje e ontem, de forma idempotente):
   recebidas, enviadas, entregues, lidas (taxas por **coorte** das enviadas no dia),
   falhas do provedor, DLQ, latência do webhook (recebido − ocorrido no provedor),
   1ª resposta média (da 1ª mensagem ainda sem resposta até o próximo envio aceito),
   idade máxima da fila (só no dia corrente). `dz23_agent` acrescenta: transferências
   para humano, orçamentos, pedidos confirmados, pagamentos confirmados desses pedidos,
   conversão em orçamento/pedido (sobre conversas com mensagem do cliente no dia),
   respostas de IA, IA sem resposta, guarda acionada e latência média da IA. Custo de
   IA por empresa fica em "Uso de IA" (ADR-011). Visões lista/pivô/gráfico; empresa
   isolada por regra; supervisor lê, administrador gerencia.
3. **Logs estruturados sem PII**: `log_event(logger, evento, **valores)` gera
   `dz23_event=<nome> chave=valor` com valores saneados (e-mail e sequências longas de
   dígitos mascarados). Webhook registra canal, provedor, nº de eventos e duração.
   Logs que imprimiam a exceção crua passam a registrar só o tipo/motivo saneado; a
   BrasilAPI deixa de logar a URL com o identificador consultado.

## Consequências
- Métricas são **derivadas** das filas: se a retenção (Fase 10) apagar mensagens
  antigas, recalcular um dia antigo dá números menores — o cron só recalcula hoje e
  ontem, e os registros anteriores permanecem.
- "Transferidas" conta a última transferência de cada conversa no dia (o campo guarda
  a mais recente), não cada transferência.
- Saúde calculada na leitura custa 4 consultas agregadas por tela (uma por fila) —
  aceitável para a quantidade de canais de uma empresa; não é um endpoint público.
- Não há exportação para Prometheus/Grafana nesta fase; o formato chave=valor dos logs
  permite coletar com qualquer agregador de logs.
