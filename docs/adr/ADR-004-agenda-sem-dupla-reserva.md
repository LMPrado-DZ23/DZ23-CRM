# ADR-004 — Agenda sem dupla reserva (advisory lock + checagem na mesma transação)

- **Status:** aceito (Fase 3, 2026-09-13)
- **Riscos tratados:** R13, R14

## Contexto

O agente fazia *consultar-depois-inserir* em `calendar.event`: dois workers podiam
ver o horário livre e ambos criar o evento. A checagem ignorava eventos criados à
mão (sem oportunidade), não respeitava expediente/feriados e fixava 60 minutos.

## Decisão

1. **Serialização por `pg_advisory_xact_lock`** com chave derivada de
   `(empresa, responsável da agenda)`. O lock vale até o fim da transação do item
   da inbox (commit por item, ADR-003); o segundo worker espera, e ao obter o lock
   já enxerga o evento do primeiro.
2. **Conflito contra todos os eventos relevantes** no intervalo
   `[início − intervalo, fim + intervalo]`: eventos do responsável
   (`user_id` ou participante) **inclusive manuais**, e eventos ligados a
   oportunidades da empresa do canal.
3. **Expediente e feriados** via `resource.calendar` do canal (padrão: calendário
   da empresa): o horário precisa caber inteiro nos intervalos de trabalho, que já
   descontam as ausências globais (`resource.calendar.leaves`, usadas como feriados).
4. **Fuso** do calendário/empresa; **duração** e **intervalo entre atendimentos**
   configuráveis por canal.
5. **Cancelar/remarcar**: o evento futuro do próprio lead é arquivado (não
   apagado); remarcar = cancelar + reservar sob o mesmo lock.
6. **Idempotência** do efeito por mensagem via `dz23.business.action` (ADR-005): o
   retry do mesmo item da inbox não cria segundo evento.

## Alternativas rejeitadas

- **Exclusion constraint (`EXCLUDE USING gist`) em `calendar_event`** — exige alterar
  tabela do core, afetaria agendas manuais de todas as empresas e não modela
  expediente/intervalo. Rejeitado.
- **`SELECT … FOR UPDATE` nos eventos existentes** — não bloqueia a *ausência* de
  linhas (phantom); dois workers passariam.

## Consequências

Reservas para o mesmo responsável ficam serializadas (volume de WhatsApp torna o
custo irrelevante). Responsáveis diferentes não se bloqueiam.
