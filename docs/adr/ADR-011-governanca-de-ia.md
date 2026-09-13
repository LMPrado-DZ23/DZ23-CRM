# ADR-011 — Governança de IA (Fase 8)

- Status: aceito
- Data: 2026-09-13
- Módulos: `dz23_ai` 19.0.2.0.0, `dz23_agent` 19.0.5.0.0

## Contexto
A IA respondia com configuração global única, sem limite de uso, sem proteção contra
provedor instável e sem regra para passar a conversa a um humano. Um robô que insiste
em responder uma reclamação, ou que "inventa" um desconto, é pior do que nenhum robô.

## Decisão
1. **Configuração por empresa** (`res.company`): provedor, modelo, política de IA
   externa (`inherit` usa o consentimento global; `allow`/`deny` sobrescrevem) e
   limites. Sem valor na empresa vale o padrão global.
2. **Privacidade**: IA externa só com consentimento; PII redigida (e-mail, telefone,
   CPF/CNPJ); imagem nunca vai a provedor externo; o contexto enviado é montado por
   allowlist (empresa, catálogo, preços da lista). O chatter só vai para IA local.
   O resumo de lead não envia e-mail/telefone para IA externa.
3. **Resiliência**: timeout por provedor (`dz23.ai.timeout.<provedor>`, padrão local
   60 s / externos 20–30 s); 429 tipado com `Retry-After`; **circuit breaker** por
   empresa+provedor (`dz23.ai.breaker`: 3 falhas → aberto com cooldown exponencial a
   partir de 60 s, máx. 1 h, respeitando o `Retry-After`; após o cooldown uma chamada
   de teste decide). Erro de configuração (sem chave) não abre o breaker: não houve
   chamada ao provedor.
4. **Custo e limites**: cada chamada grava `dz23.ai.usage` (empresa, provedor,
   modelo, finalidade, tokens, custo estimado, duração, status) — sem texto de
   conversa. Tokens vêm do provedor quando ele informa; senão estimativa `len/4`.
   Limites por empresa: chamadas/dia e custo US$/mês; ao atingir, a chamada é bloqueada
   e registrada.
5. **Transferência para humano** (conversa → `waiting_internal`, robô em silêncio):
   - assunto sensível ou pedido de atendente (reclamação, Procon, advogado, cobrança
     indevida, estorno, reembolso, cancelar compra/pedido/contrato) — prioridade alta;
   - mais de N respostas livres de IA seguidas (`agent_max_bot_turns`, padrão 5);
     intenções determinísticas (preço, compra, agenda) zeram o contador;
   - IA indisponível: breaker aberto, limite ou configuração → sem retry; falha
     transitória → retry com backoff e, esgotado, transferência.
   A mensagem ao cliente muda conforme o expediente do calendário do canal.
6. **A IA não decide negócio**: regras fixas no prompt (não removíveis pelo prompt do
   canal) e **guarda de saída determinística** — resposta que fale de valor, %,
   desconto, parcela, estorno, reembolso, estoque, nota fiscal, cancelamento, PIX ou
   boleto é trocada por texto fixo que leva ao fluxo de preço oficial. Preço, pedido,
   agenda e pagamento continuam só nos fluxos determinísticos (ADR-004/005).
7. **Resposta tardia**: se um humano assumiu depois do enfileiramento, a resposta da IA
   é descartada (registrada no pedido).
8. **Estado de governança em cursor próprio**: limites, breaker e registro de uso são
   lidos/gravados num cursor separado com commit imediato (`dz23.ai._governance`), e a
   exceção só é levantada depois desse commit. Motivo (bug real encontrado no smoke):
   gravado na transação do chamador, o registro da falha sumia no rollback do savepoint
   do worker ou da requisição HTTP — o breaker nunca abriria. Nada de breaker/uso é
   tocado na transação do chamador, o que evita auto-deadlock pela linha do breaker.
9. **Retenção**: textos de `dz23.ai.request` são anonimizados após
   `dz23.ai.request_retention_days` (90); `dz23.ai.usage` é apagado após
   `dz23.ai.usage_retention_days` (400). Metadados de auditoria (provedor, modelo,
   versão do prompt) permanecem.

## Consequências
- O cliente nunca fica sem retorno e nunca recebe condição comercial inventada.
- Falso positivo da guarda (ex.: IA cita "pix" em contexto neutro) gera uma resposta
  genérica — escolha consciente: errar para o lado seguro.
- Detecção de assunto sensível é por palavras-chave (determinística e testável), não
  por "confiança" do modelo; não cobre ironia ou pedidos muito indiretos. O limite de
  respostas livres é a rede de proteção para esses casos.
- A estimativa `len/4` pode errar para provedores que não informam tokens; os limites
  de custo são de contenção, não de faturamento.

## Revisão externa (DZ23 MCP, reviewer)
Pontos avaliados e não adotados: timeout sem padrão (já existe padrão), "imagem nunca
para externo contradiz WhatsApp" (a regra é só para envio à IA, mídia do WhatsApp segue
ADR-009), ambiguidade de `inherit` (definido acima). Adotados/confirmados: guarda de
saída contra prompt injection de condição comercial; tokens reais quando o provedor
informa.
