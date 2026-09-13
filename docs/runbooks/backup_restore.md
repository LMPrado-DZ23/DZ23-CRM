# Runbook — backup e restauração (PostgreSQL + filestore)

Aplica-se ao ambiente Docker do DZ23 CRM (`docker-db-1` com PostgreSQL 16 e
`docker-odoo-1` com o filestore em `/var/lib/odoo`). Ajuste nomes de contêiner,
banco e caminhos ao seu ambiente. **Nunca** teste restauração sobre o banco de
produção: restaure em um banco novo.

## 1. O que precisa ser copiado
1. Banco (`pg_dump` formato custom `-Fc`).
2. Filestore do banco (`/var/lib/odoo/filestore/<banco>`): anexos, mídias recebidas,
   exportações do titular.
3. Configuração fora do banco: `odoo.conf`, variáveis de ambiente e segredos (em cofre,
   **nunca** junto do backup em texto puro).

## 2. Backup
```bash
DB=dz23crm
STAMP=$(date +%Y%m%d-%H%M)
docker exec docker-db-1 pg_dump -U odoo -Fc "$DB" -f "/tmp/$DB-$STAMP.dump"
docker cp "docker-db-1:/tmp/$DB-$STAMP.dump" "./backups/$DB-$STAMP.dump"
docker exec docker-odoo-1 tar -czf "/tmp/filestore-$STAMP.tgz" -C /var/lib/odoo/filestore "$DB"
docker cp "docker-odoo-1:/tmp/filestore-$STAMP.tgz" "./backups/filestore-$STAMP.tgz"
sha256sum "./backups/$DB-$STAMP.dump" "./backups/filestore-$STAMP.tgz" > "./backups/$STAMP.sha256"
```
- Criptografe antes de sair do servidor (ex.: `age`/`gpg` com chave fora do servidor).
- Guarde fora do host (outro provedor/região) com acesso restrito.

## 3. Rotação (LGPD)
Backups contêm dados pessoais que a rotina de retenção já anonimizou no banco vivo.
Mantenha a janela mínima necessária para recuperação:
- diários: 7; semanais: 4; mensais: 3 (padrão sugerido);
- não guarde backups além do maior prazo de retenção de mensagens (365 dias), exceto
  quando exigido por obrigação legal/fiscal documentada;
- registre descarte (data, arquivos, responsável).

## 4. Restauração (em banco novo)
```bash
NEW=dz23crm_restore
docker cp ./backups/dz23crm-STAMP.dump docker-db-1:/tmp/restore.dump
docker exec docker-db-1 createdb -U odoo "$NEW"
docker exec docker-db-1 pg_restore -U odoo -d "$NEW" --no-owner /tmp/restore.dump
docker cp ./backups/filestore-STAMP.tgz docker-odoo-1:/tmp/filestore.tgz
docker exec docker-odoo-1 bash -c "mkdir -p /var/lib/odoo/filestore && tar -xzf /tmp/filestore.tgz -C /var/lib/odoo/filestore && mv /var/lib/odoo/filestore/dz23crm /var/lib/odoo/filestore/$NEW"
```
Depois:
1. **Neutralize integrações** antes de subir o Odoo apontando para o banco restaurado:
   desative os crons (`UPDATE ir_cron SET active = false;`) e os canais
   (`UPDATE dz23_channel SET active = false;`) para não reenviar mensagens nem
   processar webhooks/pagamentos em duplicidade.
2. Suba o Odoo com `-d dz23crm_restore`, confira login, conversas e pedidos.
3. **Reaplique a LGPD**: rode a retenção
   (`odoo shell -d dz23crm_restore` → `env["dz23.privacy.retention"]._cron_apply()`)
   e **refaça as anonimizações de titulares** atendidas depois da data do backup
   (consulte a Auditoria de acesso do banco atual, ação "Anonimizou titular").
4. Só então reative crons/canais, um de cada vez, observando "Saúde dos canais".

## 5. Rollback de atualização de módulo
Antes de `-u` em produção, faça o backup acima. Se a atualização falhar:
1. pare o Odoo;
2. restaure o dump **em um banco novo** e aponte a instância para ele (ou renomeie os
   bancos com o Odoo parado);
3. volte o código dos módulos para o commit anterior (`git checkout <commit>`) e suba.
Migrações de dados deste projeto são aditivas; não há "downgrade" automático de módulo.

## 6. Teste periódico
A cada trimestre, restaure o último backup em um banco descartável, rode o login e
`scripts/smoke.sh` contra uma cópia, e registre o resultado.
