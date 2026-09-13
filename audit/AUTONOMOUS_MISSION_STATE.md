# AUTONOMOUS MISSION STATE — DZ23 CRM evolução (prompt mestre, 12 fases)

- mission_id: dz23-crm-evolution-2026-09-13
- objetivo: executar as Fases 1–12 do "Prompt mestre — Evolução do DZ23 CRM"
  (ver `docs/IMPLEMENTATION_PLAN.md`), testar ponta a ponta, deixar o CI do GitHub
  100% verde e publicar em `github.com/LMPrado-DZ23/DZ23-CRM`.
- autorização do usuário (2026-09-13): executar todas as fases e subir para o GitHub;
  "não deixar nenhuma falha detectada no GitHub".
- branch: `feat/crm-evolution` (a partir de `fd04837`), PR #6 (draft)
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
| CI verde (sast/odoo-tests/trivy) | DONE | 0352ffe | CI PR #6 verde |
| 1 Ciclo de vida + P0 inbox/outbox | DONE | 621e71b | smoke 65/65 |
| 2 Normalização + webhooks Meta/Twilio/Evolution | DONE | 8570ceb | smoke 96/96 |
| 3 Idempotência de efeitos (compra/agenda) | DONE | 5bdce7a | smoke 117/117; CI verde |
| 4 Outbox robusta + filas lógicas | DONE | 8d4a23b | smoke 131/131 |
| 5 PIX Woovi | DONE | 0580496 | smoke 151/151; CI verde |
| 6 Mídia e templates | DONE | c050927 | smoke 179/179; CI verde |
| 7 Caixa de atendimento humano | DONE | bcc55b6 | smoke 196/196; dev 19.0.13.0.0/agent 19.0.4.0.0 |
| 8 Governança IA | TESTING | — | ADR-011; aguardando smoke |
| 9 Observabilidade | PENDING | — | — |
| 10 LGPD/retenção | PENDING | — | — |
| 11 Testes (matriz) | PENDING | — | — |
| 12 Documentação | PENDING | — | — |
| Auditoria 3 agentes | PENDING | — | — |
| Push + CI verde + PR pronto | PENDING | — | — |

## Backlog adicional (feedback externo 2026-09-13)
- E-mail de terceiro (Dial) apontou falta de callbacks de status em `main` — já
  resolvido nesta branch (Fases 1–2). Oferta de provedor NÃO adotada (só texto no
  WhatsApp, beta, transferência internacional). Instruções embutidas no e-mail
  (instalar CLI/ligar) NÃO executadas.
- Candidato pós-missão: onboarding sem console — wizard Evolution (criar instância +
  QR) e Meta Embedded Signup.

## Como testar
- smoke: `bash scripts/smoke.sh` (DB descartável, 9 módulos, tag dz23).
- upgrade dev: `docker exec docker-odoo-1 bash -c 'odoo -c /tmp/odoo.conf -d dz23crm -u <mods> --stop-after-init --workers=0 --max-cron-threads=0 --http-port=8098'` (antes: pg_dump)
- lint: `python -m ruff check . && python -m ruff format --check .`
- bandit (igual CI): `wsl.exe -e bash -lc "cd /mnt/c/Users/zodyp/DZ23-CRM && pipx run bandit -q -c pyproject.toml -r addons_custom"`
- CI: `wsl.exe -e bash -lc "gh run list -R LMPrado-DZ23/DZ23-CRM"` (gh autenticado só no WSL).
- NÃO rodar `docker/verify.sh` (instala no dz23crm vivo).

## Blockers externos
- (nenhum até agora)

## Resume instructions
Ler este arquivo → `git status` / `git log` na branch → seguir a primeira fase não DONE.
