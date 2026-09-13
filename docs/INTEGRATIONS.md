# Matriz de integrações

Estado verificado por testes automatizados (APIs simuladas) no commit da Fase 11.
"Ao vivo" exige conta, credenciais e, em alguns casos, aprovação do provedor.

## WhatsApp

| Capacidade | Meta Cloud API (oficial) | Twilio (BSP oficial) | Evolution API (não oficial) |
|---|---|---|---|
| Mensagem recebida (texto, botão/lista, localização, contato) | ✅ lote completo (`entry/changes/messages`) | ✅ | ✅ `messages.upsert` (grupos/broadcast ignorados) |
| Status de entrega/leitura | ✅ `statuses` (erros inclusos) | ✅ status callback | ✅ `messages.update` |
| Correlação do status com o envio | id do provedor + `correlation_id` (`biz_opaque_callback_data`) | `MessageSid` | `key.id` |
| Mídia recebida | ✅ download em 2 etapas | ✅ basic auth | ✅ base64 |
| Envio de texto | ✅ | ✅ | ✅ |
| Envio de mídia | ❌ não implementado | ❌ não implementado | ❌ não implementado |
| Templates oficiais | ✅ envio + sincronização | ✅ `ContentSid` | ⚠️ envia o texto renderizado (sem aprovação) |
| Janela de 24 h | ✅ aplicada (fora dela só template) | ✅ aplicada | não se aplica |
| Autenticação do webhook | HMAC-SHA256 `X-Hub-Signature-256` + `phone_number_id` | HMAC-SHA1 `X-Twilio-Signature` + AccountSid/número | segredo por canal `X-DZ23-Callback` + instância |
| Verificação de assinatura (GET) | ✅ `hub.verify_token` | — | — |
| Erros tipados, `Retry-After`, pausa por 429 | ✅ | ✅ | ✅ |
| Estado da conexão | — | — | ✅ `connection.update` + conexão por QR |
| Risco | aprovação de templates e número pela Meta | custo por mensagem | bloqueio do número (não oficial) |

## Pagamento — Woovi (PIX)

| Capacidade | Estado |
|---|---|
| Cobrança PIX (QR + copia e cola) | ✅ uma por transação; reaproveitada ao recarregar; não recria expirada |
| Webhook | ✅ RSA-SHA256 (fail-closed), persistido e deduplicado antes de processar |
| Confirmação | ✅ exige valor, moeda BRL e valor igual ao da transação |
| Eventos fora de ordem, expiração, estorno | ✅ |
| Conciliação periódica | ✅ cron consulta a cobrança e aplica o status |
| Ao vivo | requer conta Woovi (AppID) + webhook público HTTPS |

## Inteligência artificial

| Provedor | Tipo | Observações |
|---|---|---|
| Ollama | local, grátis | padrão; único que recebe histórico (chatter) e imagem |
| Groq | externo (free-tier) | só com consentimento por empresa; PII redigida |
| Google Gemini | externo (free-tier) | chave no cabeçalho, nunca na URL |
| OpenAI | externo (pago) | custo estimado por modelo |
| Anthropic | externo (pago) | custo estimado por modelo |

Comum a todos: timeout por provedor, 429 tipado, circuit breaker por empresa+provedor,
limites diários/mensais, registro de uso sem texto ([ADR-011](adr/ADR-011-governanca-de-ia.md)).

## Outras

| Integração | Estado |
|---|---|
| BrasilAPI (CNPJ, CEP, feriados) e AwesomeAPI (câmbio) | ✅ sem chave; timeout 8 s; logs sem o identificador consultado |
| NF-e (`dz23_fiscal`) | ⚠️ estrutura fail-closed por provedor; não evoluída nesta rodada; requer certificado A1 + conta no provedor |
| Google Agenda | módulo nativo do Odoo (opcional); OAuth configurado pelo usuário |
| Central de integrações (`dz23_integrations`) | catálogo de integradores com passo a passo |
