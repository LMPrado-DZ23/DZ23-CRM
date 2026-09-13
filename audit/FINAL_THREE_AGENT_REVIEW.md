# Auditoria final — três auditores independentes

- Escopo: `fd04837..e3e5cd1` (fases 0–12) na branch `feat/crm-evolution` (PR #6).
- Auditores (em paralelo, somente leitura, sem ver as conclusões uns dos outros):
  **A** Arquitetura/Engenharia · **B** Segurança/DevSecOps · **C** Produto/QA/UX.
- Gate de conclusão: **0 CRITICAL e 0 HIGH** abertos, testes e CI verdes.
- Correções: lote 1 (crítico/altos) em `1dad9a8`; lote 2 (médios/baixos) no commit
  seguinte a este documento. Evidência de testes na seção final.

## Resultado consolidado

| Severidade | Encontrados | Corrigidos | Aceitos/documentados | Abertos |
|---|---|---|---|---|
| CRITICAL | 1 | 1 | 0 | **0** |
| HIGH | 4 | 4 | 0 | **0** |
| MEDIUM | 17 | 16 | 1 | 0 |
| LOW | 13 | 9 | 4 | 0 |
| IMPROVEMENT | 5 | 3 | 2 | 0 |

"Aceitos/documentados" = limitação registrada em `SECURITY.md`/ADR com justificativa,
sem risco alto (ver tabela).

## CRITICAL

| ID | Origem | Achado | Causa | Correção | Teste |
|---|---|---|---|---|---|
| B-1 | Segurança | Webhook Woovi assinado por **outra conta** confirmava/cancelava a transação da vítima | A chave de assinatura Woovi é global; o evento era aplicado só pela referência, sem vínculo com a cobrança da loja | O webhook virou **gatilho**: a cobrança do evento precisa ser a da transação e o status aplicado é **relido na API** com o AppID da loja (`woovi_event._trusted_payment_data`) | `test_woovi::test_webhook_status_comes_from_api_not_payload`, `test_webhook_for_another_charge_is_rejected` (+ testes de evento atualizados para o status real da API) |

## HIGH

| ID | Origem | Achado | Correção | Teste |
|---|---|---|---|---|
| B-2 | Segurança | Cópia do anexo do cliente no lead sobrevivia à retenção e à anonimização | Vínculo `lead_attachment_id` na mídia; `_dz23_purge_files` apaga original e cópias (retenção e titular) | `test_media_to_lead::test_downloaded_file_is_attached_to_lead` |
| C-1 | Produto | Formulário da conversa quebrava para Atendente sem Vendas | `lead_id`/`sale_order_ids` restritos a vendedor; pedidos calculados com sudo | `test_conversation_bot::test_attendant_without_sales_rights_opens_conversation` |
| C-2 | Produto | "Atualizar agora" das métricas (método `@api.model` em botão de lista) e acessível a qualquer usuário | Método de registro, exige supervisor/admin, recalcula só as empresas do usuário | `test_health_metrics::test_refresh_button_requires_supervisor` |
| C-3 | Produto | Credenciais globais de WhatsApp em Ajustes sem efeito; catálogo apontava rota inexistente | Bloco substituído por atalho para Canais; catálogo com rota real por canal | revisão de view (smoke instala e valida) |

## MEDIUM

| ID | Origem | Achado | Disposição | Teste/evidência |
|---|---|---|---|---|
| A-1 | Arquitetura | Claim podia reivindicar o mesmo item duas vezes (status só no subselect) | Corrigido: status/vencimento reavaliados junto do `FOR UPDATE SKIP LOCKED` e no `UPDATE` | `test_queue_claim::test_concurrent_claim_is_disjoint` (regressão) |
| A-2 | Arquitetura | Status que chegava antes do commit do id na outbox era perdido | Corrigido: evento sem outbox fica pendente 15 min; a outbox aplica ao gravar o id | `test_outbox::test_status_callback_before_send_commit_is_applied`, `test_message_lifecycle::test_event_without_outbox_is_kept` |
| A-3 | Arquitetura | Lock da agenda mais estreito que a checagem de conflito | Corrigido: lock por empresa + responsável, ordem fixa | `test_tenancy_agent::test_agenda_lock_covers_the_whole_company`, `test_agent_flows::test_concurrent_booking_is_serialized` |
| A-4 | Arquitetura | Primeira mensagem simultânea de contato novo derrubava o webhook | Corrigido: criação com savepoint + releitura | `test_tenancy::test_get_or_create_survives_concurrent_insert` |
| A-5 | Arquitetura | Saúde/métricas varriam tabelas inteiras | Parcial: índices `(channel_id, received_at/sent_at/create_date)` e parcial de pendentes. **Aceito**: saúde calculada na leitura (poucos canais por empresa) — snapshot em cron fica como evolução | índices criados na instalação/upgrade |
| A-6 | Arquitetura | Testes do cursor de governança não provavam o rollback do chamador | Corrigido: teste com conexões reais, sem modo de teste | `test_ai_governance::TestAIGovernanceRealCursor` |
| B-3 | Segurança | Anonimização deixava telefone/e-mail no parceiro, prévia dos eventos e valores rastreados | Corrigido (nome do cliente só sai sem documento fiscal) | `test_privacy_agent::test_anonymize_keeps_sale_order` |
| B-4 | Segurança | Usuário interno comum lia/escrevia contatos e enviava WhatsApp | Corrigido: contatos e composição só Atendente; botão do lead restrito; SECURITY.md honesto sobre o chatter do lead | `test_conversation::test_plain_user_cannot_read_contacts_or_compose` |
| B-5 | Segurança | Trocar canal do contato / contato da conversa movia dados entre empresas | Corrigido: campos imutáveis após criação | `test_tenancy::test_contact_channel_cannot_move_to_other_company` |
| C-4 | Produto | "Aguardar cliente" devolvia a resposta ao robô | Corrigido: com responsável volta para `human_active` | `test_conversation::test_waiting_customer_answer_returns_to_attendant` |
| C-5 | Produto | Transferência do robô zerava o SLA e ninguém era avisado | Corrigido: SLA inicia na transferência, mensagem automática não zera, atividade para o responsável | `test_agent_governance::test_handoff_keeps_sla_running_and_schedules_activity` |
| C-6 | Produto | Assistente aceitava template com opt-out/variáveis erradas; "Bloquear" gravava opt-out permanente | Corrigido: validação no assistente; bloquear não marca opt-out | `test_conversation::test_reply_wizard_refuses_templates_that_would_fail` |
| C-7 | Produto | Telas de IA só no menu técnico (modo desenvolvedor) | Corrigido: menu em Configurações | view |
| C-8 | Produto | Prazos globais apresentados como "por empresa" | Corrigido: rótulos e LGPD.md | docs/view |
| C-9 | Produto | Rótulos em inglês nas filas/eventos/IA | Corrigido para inbox, outbox, eventos, breaker e uso de IA | views |

## LOW

| ID | Disposição |
|---|---|
| A-7 | Corrigido: falha na transferência da fila de IA isola o item (retry) |
| A-8 | Parcial: limites de lote na retenção de auditoria e uso de IA. **Aceito**: backlog de retenção de 1000 itens/empresa/dia documentado |
| A-9 | Corrigido: `FOR UPDATE` na aplicação de evento Woovi |
| A-10 | Corrigido: lock de linha no breaker e uma única chamada de teste (half-open) por janela |
| B-6 | **Aceito/documentado**: auditoria não cobre `read` em lote via RPC (SECURITY.md) |
| B-7 | Corrigido: Ollama em host público é IA externa; redação declarada como melhor esforço | 
| B-8 | **Aceito/documentado**: pseudonimização (reversível com banco + segredo), LGPD.md e ADR-013 |
| B-9 | Corrigido: `max_content_length` nos webhooks e botão de métricas restrito |
| C-10 | Corrigido: nomes de menus/botões/campos e tentativas nos runbooks |
| C-11 | Corrigido: botão de rotação só na Evolution, com notificação |
| C-12 | Corrigido: transferência só para atendente com acesso à empresa |
| C-13 | Corrigido: texto de confirmação honesto e estado vazio no pedido do titular |
| C-14 | Parcial: kanban sem widget enganoso e "Devolver ao robô" exige auto-resposta. **Aceito**: ícone do app |

## IMPROVEMENT

| ID | Disposição |
|---|---|
| B-10 | **Aceito/documentado**: revalidar redirecionamentos quebraria a mídia Twilio (CDN fora da allowlist) sem conhecer os hosts oficiais — registrado em SECURITY.md |
| B-11 | **Aceito/documentado**: fixar versões de bandit/semgrep e estreitar allowlist do gitleaks exige validação no CI — registrado em SECURITY.md |
| C-14 (restante) | ícone do app Atendimento — cosmético |

## Falsos positivos rejeitados pelos auditores
XSS no chatter (bodies são `str` escapados), SQL injection (`SQL()`/parâmetros), contexto
`dz23_retention` (só limpa), reconstrução de URL da Twilio, timing do token, replay Woovi
do mesmo corpo, cursor de governança causando deadlock, migração de `correlation_id`.

## Evidência de verificação
- Lote 1: `scripts/smoke.sh` 253/253 + upgrade (`dz23_smoke_16894`), commit `1dad9a8`.
- Lote 2: ver seção "Verificação final" abaixo (preenchida após a execução).

## Verificação final
- `bash scripts/smoke.sh` → `0 failed, 0 error(s) of 264 tests` + atualização `-u` sem
  erro, banco descartável `dz23_smoke_20117` (instalação limpa dos 9 módulos).
- Gate do CI replicado localmente: nenhuma linha `ERROR`/`CRITICAL` do banco no log.
  (Uma primeira execução acusou a linha "bad query" esperada do teste A-4 — corrigida
  silenciando o logger dentro do teste, não o gate.)
- `ruff check` e `ruff format --check`: limpos. `bandit` (mesma configuração do CI): limpo.
- Regressões achadas durante as correções e resolvidas antes do commit: restringir
  contatos ao grupo Atendente (B-4) quebrava o worker do robô (usuário técnico) — o
  agente passou a ler contatos com sudo e devolver o lead no ambiente do robô.
- **Gate: 0 CRITICAL / 0 HIGH abertos.**
