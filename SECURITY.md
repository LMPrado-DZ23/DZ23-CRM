# Política de Segurança

## Como reportar uma vulnerabilidade

**Não** abra uma issue pública. Envie um e-mail para **contato@dz23.com.br** com:

- descrição do problema e impacto;
- passos para reproduzir (PoC, se houver);
- versão/commit afetado.

Você receberá confirmação em até **72 horas** e um plano de correção. Pedimos um prazo
razoável de divulgação coordenada antes de tornar o detalhe público.

## Escopo e versões

Este repositório contém os módulos DZ23 (`addons_custom/`), `docker/` e `scripts/`.
Correções de segurança são feitas na branch principal (versão de módulo mais recente).
Vulnerabilidades no **Odoo** (não redistribuído aqui) vão para a Odoo S.A.; nos módulos
**OCA**, para a OCA.

## Controles implementados

### Webhooks (WhatsApp e Woovi)
- URL por canal com **token opaco**; token desconhecido → 404.
- Autenticação **por canal e fail-closed**: Meta HMAC-SHA256 (`X-Hub-Signature-256`,
  App Secret), Twilio HMAC-SHA1 (`X-Twilio-Signature`), Evolution segredo de callback
  próprio (`X-DZ23-Callback`, diferente da apikey administrativa), Woovi RSA-SHA256.
  Credencial ausente → 503; assinatura inválida → 401.
- Payload de outro canal (phone_number_id, AccountSid/número, instância) → 409.
- Limites: corpo > 1 MiB → 413; mais de 1000 eventos por requisição → 413.
- Persistência antes do 200; falha → 500 (o provedor reentrega); dedupe impede efeito
  duplicado em replay.

### Multi-empresa e acesso
- *Record rules* por empresa em canais, contatos, conversas, mensagens, eventos, mídias,
  templates, ações de negócio, métricas, supressões, auditoria, IA e eventos Woovi.
- Credenciais de canal visíveis/editáveis só por administrador; alteração registrada
  na auditoria (nomes dos campos, nunca valores).
- Usuário interno comum não lê mensagens de WhatsApp; atendentes e supervisores só da
  própria empresa.

### Mídia recebida
- Download só de hosts permitidos (anti-SSRF), sem credenciais na URL, com limite de
  tamanho; tipo real detectado pelo conteúdo; executáveis/HTML disfarçado recusados;
  sha256 conferido; anexos privados por empresa; retenção configurável.

### IA
- Provedores externos só com consentimento por empresa; PII redigida; imagem nunca vai
  a provedor externo; contexto por allowlist; chaves fora do código; URL com segredo
  nunca logada. Guarda de saída impede a IA de informar preço/desconto/pagamento.

### Dados pessoais e logs
- Logs estruturados sem PII (telefones/e-mails mascarados); erros saneados antes de
  gravar.
- Retenção/anonimização, pedido do titular e auditoria de acesso — ver
  [docs/LGPD.md](docs/LGPD.md).

### Cadeia de suprimentos e CI
- Segredos nunca no Git; `gitleaks` no CI; `.env.example` só com nomes.
- `bandit`, `semgrep`, `trivy` (vulnerabilidades, segredos e misconfig) e SBOM no CI.
- Imagens Docker pinadas por digest, dependências OCA por commit e actions por SHA.

## Limitações conhecidas (assumidas)
- Credenciais de canal e chaves de IA ficam no banco do Odoo (campos restritos a
  administrador), sem criptografia em repouso própria: proteja o banco e os backups
  (criptografia de disco/backup, acesso restrito).
- Auditoria de acesso é append-only no ORM, mas não é WORM: um DBA pode alterá-la.
- Backups não são alcançados pela anonimização; siga
  [docs/runbooks/backup_restore.md](docs/runbooks/backup_restore.md).
- Evolution API é um provedor não oficial do WhatsApp (risco de bloqueio do número).

## Produção
- Odoo **atrás de reverse proxy** que sobrescreva/limpe `X-Forwarded-*` do cliente
  (o `proxy_mode` confia neles); nunca exponha o Odoo diretamente.
- HTTPS obrigatório para webhooks; configure `dz23.whatsapp.public_base_url` com a URL
  pública real (validação de assinatura da Twilio).
- `list_db = False` e `dbfilter` restrito; senha mestre forte; admin sem senha padrão.
- Rotacione tokens/segredos de canal ao suspeitar de vazamento (botão de rotação do
  segredo de callback no canal).

## Resposta a incidente
1. Rotacionar credenciais do canal/IA/Woovi afetadas e o segredo de callback.
2. Preservar logs e a auditoria de acesso; congelar backups do período.
3. Avaliar o impacto aos titulares com o encarregado (DPO) da empresa cliente.
4. Comunicar ANPD e titulares quando houver risco ou dano relevante (LGPD art. 48).
5. Registrar causa, correção e teste de regressão no CHANGELOG/ADR.
