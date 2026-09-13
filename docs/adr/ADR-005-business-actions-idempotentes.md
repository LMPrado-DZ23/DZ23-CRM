# ADR-005 — Ações de negócio idempotentes (`dz23.business.action`)

- **Status:** aceito (Fase 3, 2026-09-13)
- **Riscos tratados:** R12, R15 e base para Woovi (R19)

## Contexto

A entrega dos provedores é *at-least-once* e o cliente repete mensagens ("quero
comprar X" duas vezes, com ids diferentes). Cada repetição criava um `sale.order`
novo. Retries da inbox também podiam repetir efeitos.

## Decisão

1. **Registro de ação** `dz23.business.action` com `idempotency_key` **única**,
   `action_type`, `state` (`pending/done/failed/cancelled`), `company_id`,
   `channel_id`, `source_inbox_id`, `target_model/target_res_id`,
   `result_payload` sanitizado, `completed_at`, `error`.
2. **`_run_once(key, action_type, callback)`**: cria a ação em savepoint (a
   unicidade barra concorrentes), trava a linha (`FOR UPDATE`), executa o efeito uma
   vez e grava o alvo. Chamada repetida com a mesma chave devolve o **mesmo alvo**.
3. **Chaves por efeito**:
   - orçamento: `quote:<canal>:<contato>:<token da confirmação>` — o token nasce
     quando o resumo é apresentado; confirmar duas vezes reutiliza o orçamento;
   - agendamento: `schedule:<canal>:<correlation_id do item da inbox>`;
   - cobrança PIX (Fase 5): `woovi:charge:<transação>`.
4. **Compra em etapas** (fluxo do prompt §7.1): produto → variação/quantidade →
   **resumo com preço calculado pelo Odoo** (lista de preços do cliente) →
   confirmação explícita → orçamento idempotente. Uma nova intenção para o mesmo
   produto enquanto houver orçamento aberto do agente **reutiliza** esse orçamento.
   A IA não participa de preço, estoque, desconto nem da criação do pedido.
5. O pedido carrega `origin`, canal, empresa e o `correlation_id` da mensagem de origem.

## Consequências

Efeitos passam a ter trilha auditável (quem, quando, qual mensagem, qual alvo).
Retenção das ações segue a política de LGPD (Fase 10).
