# ADR-007 — Camada de normalização de provedores (webhooks)

- **Status:** aceito (Fase 2, 2026-09-13)
- **Riscos tratados:** R06, R07, R09, R10, R11, R23

## Contexto

Cada controller extraía "(número, texto)" do payload do seu provedor e descartava
todo o resto: Meta processava só a primeira mensagem do lote e ignorava `statuses`;
Evolution não filtrava grupos/`status@broadcast` nem recebia `MESSAGES_UPDATE`;
Twilio não tinha webhook. O domínio do CRM precisava conhecer detalhes de cada API.

## Decisão

1. **Normalizadores puros** (`dz23_whatsapp/models/provider_normalizers.py`): funções
   sem acesso a banco que recebem o payload já autenticado e devolvem uma **lista**
   de eventos no contrato interno:

   ```python
   {
       "provider": "meta_cloud" | "twilio" | "evolution",
       "event_id": "<id único do evento no provedor>",
       "kind": "message" | "status" | "connection",
       "direction": "inbound" | "outbound",
       "provider_message_id": "...",
       "provider_status": "delivered",          # original
       "status": "delivered",                   # normalizado (ADR-006)
       "sender": "5561999990000",              # E.164 sem '+'
       "recipient": "5561988887777",
       "message_type": "text" | "image" | "audio" | "video" | "document"
                       | "location" | "contact" | "interactive" | "reaction"
                       | "sticker" | "unsupported",
       "text": "...", "caption": "...",
       "media": {"media_id", "mime_type", "file_size", "sha256", "filename"} | None,
       "reply_to": "<provider_message_id>" | None,
       "occurred_at": datetime (UTC, naive),
       "error_code": "...", "error_message": "...",
       "channel_ref": "<phone_number_id | instance | AccountSid+To>",
       "payload": {<fragmento do payload daquele evento>},
   }
   ```

2. **Validação do contrato** (`validate_event`) antes de gravar: campos obrigatórios
   por `kind`, `direction`/`status`/`message_type` em listas fechadas, datas válidas.
   Evento inválido é descartado com log de metadados (sem PII) — não derruba o lote.

3. **Validação do canal**: o normalizador expõe `channel_ref` e o controller recusa
   com **409** quando diverge do canal resolvido pelo token (`phone_number_id` da
   Meta, `instance` da Evolution, `AccountSid`/número `To` da Twilio).

4. **Roteamento no controller** (fino): `message` inbound → `dz23.message.inbox`
   (dedupe por `provider_message_id`); `status` e `fromMe` → `dz23.message.event`
   (outbound, nunca inbox); `connection` → estado do canal. Tudo persiste **antes**
   do 200; falha de persistência → 500.

5. **Autenticação por provedor**:
   - Meta: HMAC-SHA256 do corpo bruto com o App Secret do canal.
   - Twilio: `X-Twilio-Signature` = Base64(HMAC-SHA1(auth_token, URL + params
     ordenados)); URL pública configurável (proxy) em `dz23.whatsapp.webhook_base`.
     IP de origem nunca é usado como autenticação.
   - Evolution: segredo de callback do canal no header `X-DZ23-Callback`.

6. **Filtros de ruído** (Evolution/Meta): grupos (`@g.us`), `status@broadcast`,
   newsletters/canais (`@newsletter`) e reações sem texto não viram atendimento.

## Consequências

- Mídia, localização, contato e interativos passam a ser **registrados** (nunca mais
  descartados em silêncio); o tratamento de mídia/anexo vem na Fase 6.
- Os testes de contrato usam fixtures anonimizadas por provedor, sem rede.
