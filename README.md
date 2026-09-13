<div align="center">

# DZ23 CRM

**CRM + atendimento por WhatsApp com IA, pronto para o Brasil** — open source, em
português, construído sobre o [Odoo 19 Community](https://github.com/odoo/odoo) por
**módulos próprios** (nunca editando o núcleo).

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Base: Odoo 19](https://img.shields.io/badge/Base-Odoo%2019%20Community-875A7B.svg)](https://github.com/odoo/odoo)
[![PT-BR](https://img.shields.io/badge/Idioma-Portugu%C3%AAs%20(BR)-009c3b.svg)](#)
[![CI](https://github.com/LMPrado-DZ23/DZ23-CRM/actions/workflows/ci.yml/badge.svg)](https://github.com/LMPrado-DZ23/DZ23-CRM/actions/workflows/ci.yml)

</div>

---

## O que é

O **DZ23 CRM** transforma o Odoo Community num **CRM de atendimento brasileiro**:
leads, funil, agenda e vendas em português, **WhatsApp multi-empresa** (Meta Cloud
API, Twilio ou Evolution) com **atendente de IA governado**, caixa de atendimento
humano, **PIX via Woovi**, autopreenchimento por **CNPJ/CEP**, métricas de
atendimento e ferramentas de **LGPD**. Tudo em módulos separados — o Odoo continua
atualizável.

## O que ele tem além do Odoo Community puro

| Área | Odoo 19 CE puro | DZ23 CRM |
|---|---|---|
| WhatsApp | Não incluso | Um canal por empresa (Meta / Twilio / Evolution), webhooks autenticados por canal e *fail-closed*, **ciclo de vida da mensagem até entregue/lida** a partir dos callbacks de status |
| Confiabilidade | — | Inbox e outbox duráveis com claim + lease (`FOR UPDATE SKIP LOCKED`), retry com backoff e jitter, `Retry-After`, pausa por 429, DLQ investigável e reprocessável |
| Atendimento humano | — | App **Atendimento**: conversa por contato, assumir/devolver ao robô, transferir, notas internas, SLA de 1ª resposta, janela de 24 h e templates oficiais |
| Atendente de IA | — | Agenda sem dupla reserva, distingue preço de compra, pedido só com confirmação explícita; transfere para humano em assunto sensível, conversa sem avanço ou IA indisponível; **a IA nunca decide preço, desconto ou pagamento** |
| IA | — | Local grátis (Ollama) por padrão; externos (Groq, Gemini, OpenAI, Anthropic) só com consentimento por empresa, PII redigida, circuit breaker, limites e custo por empresa |
| Pagamentos | — | PIX Woovi: uma cobrança por transação, eventos deduplicados, valor/moeda conferidos, conciliação periódica |
| Operação | — | Saúde dos canais, métricas diárias (entrega, leitura, 1ª resposta, conversão), logs estruturados sem PII |
| LGPD | — | Retenção e anonimização por empresa, exportação e anonimização do titular, supressão por opt-out, auditoria de acesso |
| Brasil | Genérico | pt-BR padrão, CNPJ/CEP, feriados, câmbio (BrasilAPI) |
| Marca | "Powered by Odoo" | Debrand total, tema próprio, PWA + push |

## Garantias — e o que **não** garantimos

- **Entrega ao WhatsApp é *at-least-once*.** Se o processo cair depois de o provedor
  aceitar a mensagem e antes do registro, o item é reenviado e o cliente pode receber a
  mesma resposta duas vezes. Os provedores não aceitam chave de idempotência; quando o
  callback de status traz o id (Meta devolve o nosso `correlation_id`), o item é
  reconciliado sem reenviar. Ver [ADR-003](docs/adr/ADR-003-queue-claim-lease.md) e
  [ADR-008](docs/adr/ADR-008-filas-logicas-e-erros-de-provedor.md).
- **Efeito de negócio *exactly-once*** (orçamento, pedido, agendamento, confirmação de
  PIX): reentregas e reprocessamentos não duplicam o efeito
  ([ADR-004](docs/adr/ADR-004-agenda-sem-dupla-reserva.md),
  [ADR-005](docs/adr/ADR-005-business-actions-idempotentes.md)).
- **Status monotônico**: um callback atrasado nunca faz `lida` voltar para `enviada`
  ([ADR-006](docs/adr/ADR-006-message-lifecycle.md)).
- **"Enviada" ≠ "entregue"**: `sent` só significa que o provedor aceitou; entregue e
  lida vêm dos callbacks. Sem webhook de status configurado, a tela de saúde avisa.
- Envio de **mídia** pelo robô/atendente ainda não existe (recebimento sim); templates
  oficiais sim.
- NF-e, PIX ao vivo e WhatsApp oficial em produção dependem de contas, certificados e
  aprovações de terceiros.

## Módulos (`addons_custom/`)

| Módulo | Função |
|---|---|
| `dz23_branding` | Rebrand total, pt-BR padrão, PWA/push |
| `dz23_brasil_tools` | CNPJ/CEP, enriquecimento, feriados, câmbio |
| `dz23_crm` | Ajustes de CRM (atalho de WhatsApp no lead) |
| `dz23_whatsapp` | Canais, webhooks, inbox/outbox/eventos, mídia, templates, caixa de atendimento, saúde, métricas, LGPD |
| `dz23_agent` | Atendente de IA: agenda, preço, compra, handoff humano, governança, métricas de negócio |
| `dz23_ai` | IA plugável com governança (empresa, consentimento, breaker, limites, uso/custo) |
| `dz23_payment_woovi` | Provedor PIX Woovi |
| `dz23_fiscal` | Emissão de NF-e via provedor (fail-closed) |
| `dz23_integrations` | Central de integrações |

## Instalação rápida (Docker)

```bash
git clone https://github.com/LMPrado-DZ23/DZ23-CRM.git dz23-crm
cd dz23-crm
bash scripts/fetch_oca.sh
cp .env.example .env   # edite as senhas de desenvolvimento
cd docker && docker compose up -d
docker compose exec odoo odoo -d dz23crm \
  -i dz23_branding,dz23_brasil_tools,dz23_crm,dz23_whatsapp,dz23_agent,dz23_ai,dz23_payment_woovi,dz23_fiscal,dz23_integrations \
  --load-language=pt_BR --stop-after-init
docker compose restart odoo
```

Acesse `http://localhost:8069`. Configure os canais seguindo o
[guia de webhooks](docs/runbooks/webhooks.md).

## Qualidade e segurança

- **Testes** (tag `dz23`): **249 testes, 0 falhas e 0 erros** no commit da Fase 11 —
  instalação limpa + atualização (`-u`) em banco descartável (`scripts/smoke.sh`) e o
  mesmo no job `odoo-tests` do CI. Cobertura por requisito em
  [`docs/TEST_MATRIX.md`](docs/TEST_MATRIX.md). Nenhum teste usa rede ou credencial real.
- **CI**: ruff, gitleaks, bandit, semgrep, trivy, SBOM e testes Odoo; actions pinadas
  por SHA, imagens por digest.
- Webhooks autenticados por canal e *fail-closed*; segredos só em `.env`/banco (nunca
  no Git).
- Política de segurança: [SECURITY.md](SECURITY.md) · LGPD: [docs/LGPD.md](docs/LGPD.md).
  Vulnerabilidade? Escreva para **contato@dz23.com.br** (não abra issue pública).

## Documentação

[Índice](docs/README.md) · [Arquitetura](ARCHITECTURE.md) ·
[Integrações](docs/INTEGRATIONS.md) · [Webhooks](docs/runbooks/webhooks.md) ·
[Recuperação de filas](docs/runbooks/queue_recovery.md) ·
[Backup e restauração](docs/runbooks/backup_restore.md) · [ADRs](docs/adr/) ·
[CHANGELOG](CHANGELOG.md)

## Licença

Distribuído sob **MIT** (ver [LICENSE](LICENSE)). O DZ23 CRM **roda sobre** o Odoo
Community (LGPL-3, não redistribuído aqui) e pode usar módulos da OCA (AGPL/LGPL,
baixados à parte). "Odoo" é marca da Odoo S.A., usada apenas de forma nominativa.

## Contribuindo

Veja [CONTRIBUTING.md](CONTRIBUTING.md).
