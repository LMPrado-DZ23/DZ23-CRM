# LGPD — tratamento de dados pessoais no DZ23 CRM

Documento operacional. Decisões técnicas: [ADR-011](adr/ADR-011-governanca-de-ia.md)
(IA) e [ADR-013](adr/ADR-013-lgpd-retencao.md) (retenção e titular). Este texto não
substitui a análise jurídica de cada empresa cliente, que é a **controladora** dos
dados; a DZ23, ao operar o SaaS, atua como **operadora**.

## 1. Finalidades e bases legais sugeridas

| Dado | Finalidade | Base legal típica (art. 7º) |
|---|---|---|
| Telefone, nome e mensagens de WhatsApp | Atender o cliente que iniciou o contato, responder dúvidas, agendar e vender | Execução de contrato / procedimentos preliminares (V); legítimo interesse (IX) |
| Leads e histórico no CRM | Gestão comercial do relacionamento | Legítimo interesse (IX) |
| Pedidos, faturas, pagamentos | Venda, cobrança e obrigações fiscais | Obrigação legal (II) e execução de contrato (V) |
| Envio proativo (templates/marketing) | Comunicação ativa | Consentimento (I) ou legítimo interesse com opt-out |
| Texto enviado a IA externa | Resposta automática | Só com consentimento/base registrada pela empresa (política por empresa) |
| Auditoria de acesso | Segurança e prestação de contas | Legítimo interesse / art. 46 |

## 2. Retenção (padrões, ajustáveis por empresa em Ajustes → DZ23 WhatsApp)

| Dado | Prazo padrão | O que acontece |
|---|---|---|
| Mensagens recebidas/enviadas finalizadas | 365 dias | Texto, legenda, payload e telefone anonimizados (registro mantido) |
| Prévia/erro dos eventos de status | 180 dias | Campos limpos; status e datas mantidos |
| Anexos recebidos | 180 dias | Arquivo apagado |
| Notas automáticas do robô no lead | 365 dias (mesmo prazo das mensagens) | Texto substituído |
| Textos da fila de IA | 90 dias | Mensagem e resposta removidas; provedor/modelo/versão mantidos |
| Registro de uso de IA (sem texto) | 400 dias | Apagado |
| Auditoria de acesso | 730 dias | Apagada |
| Arquivo de exportação do titular | 7 dias | Apagado |
| Pedidos, faturas, pagamentos | **nunca pela rotina** | Seguem o prazo fiscal (em regra 5 anos) e as ferramentas do Odoo |

A rotina roda diariamente (cron "DZ23: retenção e anonimização (LGPD)") em lotes; itens
ainda na fila nunca são tocados.

## 3. Direitos do titular (art. 18)

Menu **Atendimento → Pedido do titular (LGPD)** (supervisor) ou **DZ23 WhatsApp →
Pedido do titular** (administrador):

1. Informe o telefone e a empresa; confira "Contatos encontrados".
2. **Exportar dados (JSON)** — acesso/portabilidade: baixa um arquivo com contatos,
   conversas, mensagens, status de entrega, anexos (metadados), leads, pedidos e
   textos enviados à IA. Entregue ao titular por canal seguro e **não reenvie** o
   arquivo por e-mail aberto; ele expira no servidor em 7 dias.
3. **Anonimizar titular** — eliminação quando legalmente possível: informe o protocolo,
   confirme. Remove textos, anexos, notas, dados de contato do lead e textos de IA;
   cancela envios pendentes; bloqueia a conversa; registra supressão. **Mantém**
   pedidos, faturas e pagamentos (obrigação legal) — informe isso ao titular.
4. Correção de dados cadastrais: pelo próprio parceiro/lead no Odoo.

Todo pedido fica na **Auditoria de acesso** (quem, quando, quantos contatos — sem
conteúdo).

## 4. Opt-out e bloqueio de marketing
- O cliente escreve "SAIR", "PARAR" ou "STOP" (mensagem inteira) ou o atendente marca
  "Não receber mensagens proativas": templates e mensagens proativas ficam bloqueados.
- O opt-out cria uma supressão (pseudônimo): mesmo que a conversa seja recriada, ela
  nasce com opt-out. O atendimento iniciado pelo cliente continua permitido.
- "Bloquear" impede qualquer envio e resposta automática.

## 5. IA externa
- Desligada por padrão; ligar exige consentimento/base legal registrada por empresa.
- Saem apenas: prompt do canal, regras, nome da empresa, catálogo/preços e a mensagem
  do cliente com e-mail, telefone e CPF/CNPJ redigidos. Imagens nunca vão a provedor
  externo; o histórico (chatter) só vai para IA local.
- Transferência internacional: provedores externos processam fora do Brasil — a empresa
  deve avaliar cláusulas contratuais (art. 33) antes de ligar.

## 6. Auditoria de acesso
Registra abertura de conversa e de mensagem, exportação e anonimização de titular,
reenfileiramento de DLQ e alteração de credenciais (só nomes dos campos). Leitura
restrita a administradores. Não registra listagens.

## 7. Backups
Backups contêm dados pessoais e **não são alcançados** pela anonimização. Siga
[`runbooks/backup_restore.md`](runbooks/backup_restore.md): criptografia, acesso
restrito, rotação alinhada aos prazos acima e reaplicação das anonimizações após
qualquer restauração.

## 8. Incidentes
Em suspeita de vazamento: isolar credenciais (rotacionar tokens do canal e chaves de
IA), preservar logs, avaliar risco aos titulares e comunicar ANPD e titulares quando
houver risco ou dano relevante (art. 48). Veja também `SECURITY.md`.
