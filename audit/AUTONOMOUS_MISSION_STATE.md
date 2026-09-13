# AUTONOMOUS MISSION STATE — DZ23 CRM evolução (prompt mestre, 12 fases)

- mission_id: dz23-crm-evolution-2026-09-13
- objetivo: executar as Fases 1–12 do "Prompt mestre — Evolução do DZ23 CRM"
  (ver `docs/IMPLEMENTATION_PLAN.md`), testar ponta a ponta, deixar o CI do GitHub
  100% verde e publicar em `github.com/LMPrado-DZ23/DZ23-CRM`.
- autorização do usuário (2026-09-13): executar todas as fases e subir para o GitHub;
  "não deixar nenhuma falha detectada no GitHub".
- branch: `feat/crm-evolution` (a partir de `fd04837`)
- estado: EXECUTING
- histórico da missão anterior: ver `git log` (commits até `fd04837`).

## Critérios de aceite (prompt mestre §17 + usuário)
1. Instala em banco limpo (smoke). 2. Upgrade do `dz23crm` com dados antigos funciona.
3. Testes antigos + novos verdes. 4. Idempotência de efeitos provada por teste.
5. Status monotônicos. 6. Reentrega de webhook não duplica. 7. Compra repetida não
duplica orçamento. 8. Sem dupla reserva. 9. Re-render PIX não duplica cobrança.
10. Meta/Twilio/Evolution validam canal. 11. Nenhum segredo em código/log/doc.
12. Isolamento entre empresas. 13. IA externa só com dados autorizados/redigidos.
14. Bot pausável/assumível por humano. 15. DLQ investigável e reprocessável.
16. README honesto. 17. Lint/compile/testes registrados. 18. Nenhuma integração real
em teste. 19. **Todos os jobs do CI verdes no GitHub** (lint, secrets, sast,
deps-container, odoo-tests) + PRs do dependabot sem falha. 20. 3 auditores: 0 CRITICAL/0 HIGH.

## Baseline
- Fase 0: commit `d170085` — smoke 41/41, ruff limpo.
- CI GitHub em `main` (run 34231829185): lint ✅, secrets ✅, **sast ❌, odoo-tests ❌,
  deps-container ❌**; 5 PRs dependabot ❌ (mesma causa).

## Fases
| Fase | Estado | Commit | Evidência |
|---|---|---|---|
| 0 Baseline | DONE | d170085 | smoke 41/41 |
| CI verde (sast/odoo-tests/trivy) | TESTING (PR #6) | 0352ffe | local: smoke 41/41, bandit/semgrep/trivy limpos; aguardando CI do PR #6 |
| 1 Ciclo de vida + P0 inbox/outbox | DONE (commit a seguir) | — | smoke 65/65 (dz23_smoke_1272); ruff/bandit limpos; bug real achado e corrigido: flush da recuperação de lease antes do claim SQL |
| 2 Normalização + webhooks Meta/Twilio/Evolution | PENDING | — | — |
| 3 Idempotência de efeitos (compra/agenda) | PENDING | — | — |
| 4 Outbox robusta + filas lógicas | PENDING | — | — |
| 5 PIX Woovi | PENDING | — | — |
| 6 Mídia e templates | PENDING | — | — |
| 7 Caixa de atendimento humano | PENDING | — | — |
| 8 Governança IA | PENDING | — | — |
| 9 Observabilidade | PENDING | — | — |
| 10 LGPD/retenção | PENDING | — | — |
| 11 Testes (matriz) | PENDING | — | — |
| 12 Documentação | PENDING | — | — |
| Auditoria 3 agentes | PENDING | — | — |
| Push + CI verde | PENDING | — | — |

## Como testar
- smoke: `bash scripts/smoke.sh` (DB descartável, 9 módulos, tag dz23).
- upgrade dev: `docker exec docker-odoo-1 bash -c 'odoo -c /tmp/odoo.conf -d dz23crm -u <mods> --stop-after-init --workers=0 --http-port=8098'`
- lint: `python -m ruff check . && python -m ruff format --check .`
- CI: `wsl.exe -e bash -lc "gh run list -R LMPrado-DZ23/DZ23-CRM"` (gh autenticado só no WSL).
- NÃO rodar `docker/verify.sh` (instala no dz23crm vivo).

## Blockers externos
- (nenhum até agora)

## Resume instructions
Ler este arquivo → `git status` / `git log` na branch → seguir a primeira fase não DONE.
