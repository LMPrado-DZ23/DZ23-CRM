#!/usr/bin/env bash
# Smoke E2E: instala TODOS os módulos DZ23 do zero num banco descartável e roda
# a suíte de testes (tag dz23) com dbfilter próprio (para o HttpCase do webhook
# rotear para o banco certo). Descarta o banco no fim. Prova "pronto do zero".
#
# Uso: bash scripts/smoke.sh
set -euo pipefail
DB="dz23_smoke_$$"
ODOO="${ODOO_CONTAINER:-docker-odoo-1}"
DBC="${DB_CONTAINER:-docker-db-1}"
DBUSER="${DB_USER:-odoo}"
MODS="dz23_branding,dz23_brasil_tools,dz23_crm,dz23_whatsapp,dz23_agent,dz23_ai,dz23_integrations,dz23_fiscal,dz23_payment_woovi"
LOG="/tmp/${DB}.log"

cleanup() { docker exec "$DBC" psql -U "$DBUSER" -d postgres -c "DROP DATABASE IF EXISTS $DB;" >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "==> Instalação limpa + testes em banco descartável: $DB"
docker exec "$ODOO" bash -c "odoo -c /tmp/odoo.conf -d $DB --db-filter='^${DB}\$' -i $MODS \
  --test-enable --test-tags dz23 --stop-after-init --workers=0 --max-cron-threads=0 \
  --http-port=8097 --without-demo=all" > "$LOG" 2>&1 || true

grep -aE "Modules loaded|tests.result:" "$LOG" || true

if ! grep -aq "Modules loaded" "$LOG"; then
  echo "FALHOU: módulos não carregaram (ver $LOG)"; exit 1
fi
# Gate POSITIVO: exige exatamente "0 failed, 0 error(s)". (O padrão antigo não
# pegava "0 failed, 1 error(s)" e dava falso verde.)
if ! grep -aqE "tests\.result: 0 failed, 0 error\(s\) of [1-9][0-9]* tests" "$LOG"; then
  echo "FALHOU: testes com falha/erro (ver $LOG)"; exit 1
fi
echo "SMOKE OK: instalação limpa + testes dz23 verdes em $DB"
