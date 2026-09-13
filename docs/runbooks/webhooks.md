# Runbook — configuração dos webhooks

Cada **canal de WhatsApp** (DZ23 WhatsApp → Canais) tem uma URL própria com um token
opaco (campo *URL do webhook (provedor)*, com botão de copiar). A autenticação é **por
canal** e *fail-closed*. Use sempre **HTTPS** e um domínio público.

## 0. Pré-requisitos
1. Odoo atrás de um reverse proxy com HTTPS, sobrescrevendo `X-Forwarded-*`.
2. Parâmetro de sistema `dz23.whatsapp.public_base_url` com a URL pública exata
   (ex.: `https://crm.suaempresa.com.br`) — a Twilio assina a URL completa.
3. Canal criado na **empresa certa**, com as credenciais do provedor preenchidas
   (somente administrador vê/edita; alterações ficam na auditoria de acesso).

As URLs seguem o padrão:
```
https://<domínio>/dz23/whatsapp/meta/webhook/<token>
https://<domínio>/dz23/whatsapp/twilio/webhook/<token>
https://<domínio>/dz23/whatsapp/evolution/webhook/<token>
https://<domínio>/payment/woovi/webhook
```

## 1. Meta — WhatsApp Cloud API
No canal (aba *Meta Cloud*): `Meta phone_number_id`, `Meta token`, `Meta API version`,
**`Meta App Secret`** (obrigatório para aceitar webhooks), `Meta verify token` (valor
aleatório criado por você) e `Meta WABA id` (para sincronizar templates).

No painel da Meta (App → WhatsApp → Configuração):
1. **Callback URL**: a URL `/meta/webhook/<token>` do canal.
2. **Verify token**: o mesmo valor do canal. A Meta faz um `GET` com
   `hub.mode=subscribe`, `hub.verify_token` e `hub.challenge`; o canal responde o
   challenge quando o token confere (senão 403).
3. Assine o campo **`messages`** (traz mensagens e `statuses`).

Validações aplicadas: `X-Hub-Signature-256` (HMAC-SHA256 do corpo com o App Secret) e
`phone_number_id` do payload igual ao do canal. Templates: botão **Sincronizar
templates** no canal (requer `WABA ID`).

## 2. Twilio
No canal (aba *Twilio*): `Twilio SID` (Account SID), `Twilio token` (Auth Token) e
`Twilio from` (`+14155238886` ou o seu número aprovado).

Na Twilio (Messaging → WhatsApp sender):
1. **When a message comes in**: URL `/twilio/webhook/<token>`, método `POST`.
2. **Status callback URL**: a mesma URL (o canal também envia `StatusCallback` em cada
   mensagem).

Validações: `X-Twilio-Signature` (HMAC-SHA1 da URL pública + parâmetros com o Auth
Token), `AccountSid` e número do canal. Se a assinatura falhar atrás de proxy, confira
`dz23.whatsapp.public_base_url` (esquema, domínio e sem barra final).

## 3. Evolution API (não oficial)
No canal (aba *Evolution*): `Evolution base URL`, `Evolution instance` e
`Evolution apikey` (administrativa). O canal gera um **segredo de callback** próprio,
diferente da apikey.

1. Botão **Conectar WhatsApp (QR)** no canal: cria/conecta a instância e configura o
   webhook da Evolution apontando para `/evolution/webhook/<token>`, com o cabeçalho
   `X-DZ23-Callback: <segredo>` e os eventos `MESSAGES_UPSERT`, `MESSAGES_UPDATE` e
   `CONNECTION_UPDATE`.
2. Leia o QR pelo WhatsApp do número.
3. Para trocar o segredo (suspeita de vazamento): botão **Rotacionar segredo de
   callback** no canal — gera um novo segredo e já reconfigura o webhook da instância
   na Evolution. A troca fica registrada na auditoria de acesso.

Validações: `X-DZ23-Callback` igual ao segredo do canal (a API key **não** serve) e
`instance` do payload igual à do canal. Mensagens de grupos, `status@broadcast` e
newsletters são ignoradas; `fromMe` vira evento de saída, nunca entrada.

## 4. Woovi (PIX)
1. Parâmetro de sistema **`dz23.woovi.webhook_pubkey`** com a chave pública (PEM) do
   webhook informada pela Woovi.
2. No provedor de pagamento Woovi: `AppID` e estado (teste/habilitado).
3. No painel Woovi: webhook para `https://<domínio>/payment/woovi/webhook` com os
   eventos de cobrança (paga, expirada, estornada).

Validação: `x-webhook-signature` (RSA-SHA256 do corpo bruto). Como a chave da Woovi é a
mesma para todas as contas, a assinatura só prova que o evento veio da Woovi: o evento é
persistido e deduplicado e funciona apenas como **gatilho** — a cobrança precisa ser a da
transação e o status aplicado é sempre relido na API com o AppID da própria loja. A
conciliação periódica corrige eventos perdidos.

## 5. Códigos de resposta (diagnóstico)

| Código | Significado | O que fazer |
|---|---|---|
| 200 | Persistido (ou duplicado, sem efeito extra) | — |
| 400 | JSON inválido | conferir o formato enviado pelo provedor |
| 401 | Assinatura/segredo ausente ou inválido | App Secret / Auth Token / segredo de callback / chave pública Woovi; `public_base_url` na Twilio |
| 403 | Verify token da Meta não confere | igualar o verify token |
| 404 | Token do canal desconhecido | copiar a URL do canal certo (token muda se o canal for recriado) |
| 409 | Payload de outro canal (phone_number_id, AccountSid/número, instância) | webhook apontado para o canal errado |
| 413 | Corpo > 1 MiB ou > 1000 eventos | reduzir lote no provedor |
| 500 | Falha ao persistir | o provedor reentrega; ver log `Falha ao persistir webhook` e o banco |
| 503 | Canal sem credencial de validação | preencher App Secret / Auth Token / segredo |

## 6. Verificação depois de configurar
1. Envie uma mensagem de teste **do seu próprio número** para o número do canal.
2. **DZ23 WhatsApp → Saúde dos canais**: `Último webhook recebido` e `Última mensagem recebida`
   devem atualizar; para Evolution, `Estado da conexão = open`.
3. Responda pela conversa (Atendimento) e confira em **Outbox** o `Status atual`
   avançar para *entregue*/*lida*. Se ficar em *enviada* por mais de 30 min, a saúde
   do canal alerta: o webhook de status não está chegando.
4. Nos logs, procure `dz23_event=webhook_ingested` (canal, provedor, nº de eventos e
   duração — sem dados pessoais).

Não use números, tokens ou cobranças reais de clientes para testar; em
desenvolvimento, use contas e chaves de teste.
