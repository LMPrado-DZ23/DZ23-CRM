# Documentação do DZ23 CRM

## Comece por aqui
- [README do projeto](../README.md) — o que é, garantias e instalação.
- [ARCHITECTURE.md](../ARCHITECTURE.md) — módulos, fluxos e decisões.
- [SECURITY.md](../SECURITY.md) — controles, limitações e resposta a incidente.
- [CHANGELOG.md](../CHANGELOG.md) — mudanças por fase e versão de módulo.

## Operação (runbooks)
- [Configuração dos webhooks](runbooks/webhooks.md) — Meta, Twilio, Evolution e Woovi.
- [Recuperação de filas](runbooks/queue_recovery.md) — pendentes, leases, DLQ, pausa por 429.
- [Backup e restauração](runbooks/backup_restore.md) — inclusive rollback de atualização e reaplicação da LGPD.
- [Papéis do PostgreSQL](runbooks/postgres_roles.md).

## Produto e conformidade
- [Matriz de integrações](INTEGRATIONS.md) — o que cada provedor suporta.
- [LGPD](LGPD.md) — finalidades, retenção, titular, IA externa.
- [Matriz de testes](TEST_MATRIX.md) — requisito → teste.
- [Paridade com open source](OPEN_SOURCE_PARITY_MATRIX.md).

## Decisões de arquitetura (ADRs)
| ADR | Tema |
|---|---|
| [001](adr/ADR-001-tenancy.md) | Multi-empresa (tenancy) |
| [002](adr/ADR-002-odoo-version-brazil.md) | Versão do Odoo e Brasil |
| [003](adr/ADR-003-queue-claim-lease.md) | Filas com claim + lease |
| [004](adr/ADR-004-agenda-sem-dupla-reserva.md) | Agenda sem dupla reserva |
| [005](adr/ADR-005-business-actions-idempotentes.md) | Ações de negócio idempotentes |
| [006](adr/ADR-006-message-lifecycle.md) | Ciclo de vida da mensagem |
| [007](adr/ADR-007-provider-normalization.md) | Normalização por provedor |
| [008](adr/ADR-008-filas-logicas-e-erros-de-provedor.md) | Filas lógicas e erros de provedor |
| [009](adr/ADR-009-midia-e-templates.md) | Mídia e templates |
| [010](adr/ADR-010-caixa-de-atendimento.md) | Caixa de atendimento humano |
| [011](adr/ADR-011-governanca-de-ia.md) | Governança de IA |
| [012](adr/ADR-012-observabilidade.md) | Observabilidade |
| [013](adr/ADR-013-lgpd-retencao.md) | LGPD e retenção |

## Histórico da evolução
- [Baseline (Fase 0)](BASELINE_AUDIT.md) — diagnóstico antes da evolução.
- [Plano de implementação](IMPLEMENTATION_PLAN.md) — fases e status.
- [Diagrama do fluxo original](diagrams/current-message-flow.mmd).

## Ambiente de desenvolvimento
```bash
bash scripts/fetch_oca.sh            # dependências OCA (pinadas por commit)
cp .env.example .env                 # senhas de desenvolvimento
cd docker && docker compose up -d
bash scripts/smoke.sh                # instalação limpa + testes + upgrade em banco descartável
python -m ruff check . && python -m ruff format --check .
```
Regras: nunca editar o núcleo do Odoo; nunca commitar segredos; testes sem rede e sem
credenciais reais.
