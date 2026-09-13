<div align="center">

# DZ23 CRM

**CRM + Atendimento por WhatsApp com IA, pronto para o Brasil** — open source,
em português, construído sobre o [Odoo 19 Community](https://github.com/odoo/odoo)
por **módulos próprios** (nunca editando o núcleo).

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Base: Odoo 19](https://img.shields.io/badge/Base-Odoo%2019%20Community-875A7B.svg)](https://github.com/odoo/odoo)
[![PT-BR](https://img.shields.io/badge/Idioma-Portugu%C3%AAs%20(BR)-009c3b.svg)](#)
[![CI](https://github.com/LMPrado-DZ23/DZ23-CRM/actions/workflows/ci.yml/badge.svg)](https://github.com/LMPrado-DZ23/DZ23-CRM/actions/workflows/ci.yml)
[![Testes](https://img.shields.io/badge/Testes%20dz23-verdes-brightgreen.svg)](#qualidade--seguran%C3%A7a)

</div>

---

## O que é

O **DZ23 CRM** pega o Odoo Community e o transforma num **CRM de atendimento
brasileiro**: leads, funil e agenda em **português**, **WhatsApp multi-tenant**
com **atendente de IA**, autopreenchimento por **CNPJ/CEP**, marca própria e
app **PWA** com push. Tudo em módulos separados — o Odoo continua atualizável.

## ⭐ O que ele tem de melhor que o Odoo Community original

> O Odoo 19 CE puro **não** traz nada disto pronto. Estes são os diferenciais
> que este projeto adiciona:

| Área | Odoo 19 CE puro | **DZ23 CRM** |
|---|---|---|
| **Idioma/Brasil** | Genérico, sem integrações locais | **pt-BR padrão**, autofill **CNPJ/CEP** + enriquecimento de lead, **feriados** e **câmbio** via BrasilAPI (grátis) |
| **WhatsApp** | Não incluso | Adaptador plugável **Meta Cloud / Twilio / Evolution**, **um canal por empresa** com credenciais e agente próprios (isolamento multi-tenant por *record rules*) |
| **Confiabilidade de mensagens** | — | **Inbox durável idempotente** (dedupe por message_id) + worker com **retry/backoff/jitter + DLQ**, e **outbox durável** para as respostas. Webhooks **autenticados e fail-closed** (HMAC Meta / RSA Woovi / segredo de callback Evolution) e resposta rápida (persiste e processa em background) |
| **Atendente de IA** | Não incluso | **Sofia**, agente **determinístico e honesto**: agenda sem inventar horário (rejeita passado, evita conflito), distingue **preço** de **compra**, não duplica pedido, e se apresenta como assistente virtual |
| **IA plugável + privacidade** | Não incluso | **Local grátis (Ollama) por padrão**; provedores externos (Groq/Gemini/OpenAI/Anthropic) só com **consentimento LGPD** e **redação de PII** (e-mail/telefone/CPF/CNPJ) antes de sair; imagem nunca vai a provedor externo |
| **Marca / white-label** | "Powered by Odoo", tema roxo | **Debrand total**: login, tema navy, favicon, **PWA + push**, sem CTA do odoo.com |
| **Fiscal & Pagamentos BR** | Não incluso | Estrutura **NF-e** (fail-closed) + **PIX/Woovi** *(requer credenciais do provedor)* |
| **Segurança & engenharia** | Base | Segredos só em `.env`/vault, credenciais de canal **só-admin**, imagens Docker **pinadas por digest**, OCA travado por **SHA**, **CI** (ruff/gitleaks/bandit/semgrep/trivy/SBOM) e suíte de testes própria |

## Recursos

- 🇧🇷 **Brasil-first**: pt-BR, CNPJ/CEP, enriquecimento, feriados, câmbio.
- 💬 **WhatsApp multi-tenant** (Meta/Twilio/Evolution) com QR de conexão.
- 🧠 **Atendente de IA determinístico** + IA local grátis (Ollama) e externa opcional.
- 🛡️ **Mensageria confiável**: inbox/outbox duráveis, idempotência, retry e DLQ com tela de administração.
- 🎨 **Marca própria** + **PWA/push**.
- 🧾 **NF-e** e 💳 **PIX/Woovi** *(dependem de credenciais externas)*.

## Módulos (`addons_custom/`)

| Módulo | Função |
|---|---|
| `dz23_branding` | Rebrand total + pt-BR padrão + PWA/push |
| `dz23_brasil_tools` | Autofill CNPJ/CEP, enriquecimento, feriados, câmbio, WhatsApp (wa.me) |
| `dz23_crm` | Ajustes de CRM (link rápido de WhatsApp no lead) |
| `dz23_whatsapp` | WhatsApp multi-tenant por canal + inbox/outbox durável (retry/DLQ) |
| `dz23_agent` | Atendente de IA (agenda/vende/responde) determinístico |
| `dz23_ai` | Camada de IA plugável (local/externa) com privacidade LGPD |
| `dz23_integrations` | Central para cadastrar e ativar integrações/APIs |
| `dz23_fiscal` | Emissão de NF-e via provedor (fail-closed) |
| `dz23_payment_woovi` | Pagamento PIX via Woovi |

## Instalação rápida (Docker)

```bash
git clone <url-do-seu-repositorio> dz23-crm
cd dz23-crm
bash scripts/fetch_oca.sh            # baixa dependências OCA (travadas por SHA)
cp .env.example .env                 # edite as senhas de desenvolvimento
cd docker && docker compose up -d
docker compose exec odoo odoo -d dz23crm \
  -i base,dz23_branding,dz23_brasil_tools,dz23_crm,dz23_whatsapp,dz23_agent,dz23_ai \
  --load-language=pt_BR --stop-after-init
docker compose restart odoo
```
Acesse **http://localhost:8069** (base `dz23crm`). Detalhes e verificação em
[`docs/`](docs/).

## Qualidade & Segurança

- **Testes** (tag `dz23`): **33/33** (0 falhas/0 erros) — `dz23_agent`, `dz23_ai`, `dz23_whatsapp`.
- **Lint/format**: `ruff` limpo. **Secret scan**: `gitleaks` sem vazamentos.
- **Webhooks** autenticados e *fail-closed*; **segredos** só em `.env`/vault.
- Auditoria de segurança e arquitetura documentada em [`audit/`](audit/).
- Política de divulgação e boas práticas em [SECURITY.md](SECURITY.md).
  Encontrou uma vulnerabilidade? Escreva para **contato@dz23.com.br** (não abra
  issue pública).

## Em construção (requer credenciais/passos externos)

- **NF-e**: adaptador por provedor (Focus NFe / NFe.io / Nuvem Fiscal) — precisa de certificado A1 + conta.
- **Woovi/PIX**: precisa de conta Woovi + webhook público.

> Entrega de resposta ao WhatsApp é *at-least-once* (o provedor pode, em raras
> falhas de rede pós-envio, receber a mesma resposta 2×); o `provider_message_id`
> é registrado para rastreio e dedup futura. Ver [CHANGELOG](CHANGELOG.md).

## Licença

Distribuído sob **MIT** (ver [LICENSE](LICENSE)) — use, modifique e redistribua
livremente. O DZ23 CRM **roda sobre** o Odoo Community (LGPL-3, não redistribuído
aqui) e pode usar módulos da OCA (AGPL/LGPL, baixados à parte). "Odoo" é marca da
Odoo S.A., usada apenas de forma nominativa.

## Contribuindo

Contribuições são bem-vindas — código, tradução, design ou documentação.
Veja [CONTRIBUTING.md](CONTRIBUTING.md).
