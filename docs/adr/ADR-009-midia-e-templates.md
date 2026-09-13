# ADR-009 — Mídia recebida e templates oficiais (janela de 24 h)

- **Status:** aceito (Fase 6, 2026-09-13)
- **Riscos tratados:** R11 (mídia descartada) e política de envio fora da janela

## Contexto

Desde a Fase 2 a mídia é *registrada* no inbox (tipo, legenda, referência), mas o
arquivo não é baixado nem guardado. Os provedores oficiais (Meta, Twilio) só aceitam
texto livre dentro da janela de atendimento de 24 h após a última mensagem do
cliente; fora dela, apenas templates aprovados.

## Decisão

### Mídia (`dz23.message.media`)

1. Criada junto com o item da inbox (estado `pending`) e baixada por um **worker
   próprio** (claim + lease, ADR-003) — o webhook continua rápido.
2. **Download por provedor**: Meta (`GET /{media_id}` → URL assinada → bytes com o
   token), Evolution (`/chat/getBase64FromMediaMessage`), Twilio (`MediaUrl` com
   basic auth).
3. **Anti-SSRF**: só HTTPS e hosts permitidos (`graph.facebook.com`, `*.fbsbx.com`,
   `api.twilio.com`); a URL da Evolution é a base configurada no canal.
4. **Validação**: tamanho máximo (`dz23.whatsapp.media_max_bytes`, padrão 16 MiB,
   com leitura em streaming abortada ao estourar), **MIME real** detectado pelo
   conteúdo (não pelo que o provedor declara), lista de MIME permitidos por tipo,
   **extensões/MIME perigosos bloqueados** (executáveis, scripts, HTML, SVG) e
   **sha256** conferido com o informado pelo provedor.
5. **Armazenamento privado**: `ir.attachment` não público, vinculado à empresa do
   canal; nunca URL pública permanente. Origem registrada (canal, provedor, inbox).
6. **IA**: arquivos nunca são enviados à IA externa (o `dz23_ai` já bloqueia imagem
   para provedor externo; o agente não envia mídia à IA).
7. **Retenção**: `dz23.whatsapp.media_retention_days` (padrão 180; 0 = manter);
   cron remove o arquivo vencido e marca a mídia como `expired` (metadados ficam).
8. `transcription`: preenchida pelo atendente (não há STT local no ambiente).

### Templates (`dz23.message.template`)

1. Cadastro por canal: nome no provedor, idioma, categoria, corpo de referência,
   nº de variáveis e **status** (`approved/pending/rejected/paused/disabled`).
   Meta: sincronização via `GET /{waba_id}/message_templates`. Twilio: `ContentSid`.
2. **Janela de 24 h** (Meta/Twilio): `last_inbound_at` por contato. Texto livre fora
   da janela vira **erro permanente** na outbox ("use template aprovado"); nunca há
   tentativa de contornar a política. Template só é enviado se `approved`.
3. Evolution (não oficial) não tem templates nem janela: o corpo é enviado como texto.

## Consequências

- Anexos do cliente passam a chegar ao lead (chatter) com acesso controlado.
- Mensagens proativas (ex.: lembrete de agendamento) exigem template aprovado.
