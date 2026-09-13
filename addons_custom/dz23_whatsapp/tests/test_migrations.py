# Migrações (Fase 11): todo script de migração carrega e expõe `migrate`; a
# 19.0.8.0.0 preenche linhas antigas (status de ciclo de vida, datas, correlation_id
# único por linha e hash do payload) sem apagar nada e pode rodar de novo.
import hashlib
import importlib.util
import os
import uuid

from odoo.modules.module import get_module_path
from odoo.tests import TransactionCase, tagged


def _migration_files():
    base = os.path.join(get_module_path("dz23_whatsapp"), "migrations")
    for version in sorted(os.listdir(base)):
        for name in ("pre-migration.py", "post-migration.py", "end-migration.py"):
            path = os.path.join(base, version, name)
            if os.path.isfile(path):
                yield version, path


def _load(path, version):
    spec = importlib.util.spec_from_file_location(
        "dz23_migration_%s_%s" % (version.replace(".", "_"), uuid.uuid4().hex[:6]), path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@tagged("post_install", "-at_install", "dz23")
class TestMigrations(TransactionCase):
    def setUp(self):
        super().setUp()
        self.channel = self.env["dz23.channel"].create(
            {
                "name": "Canal Migração",
                "company_id": self.env.company.id,
                "provider": "evolution",
                "evo_base": "http://migracao.local:8080",
                "evo_instance": "migracao_%s" % uuid.uuid4().hex[:8],
                "evo_apikey": "K",
            }
        )

    def test_every_migration_script_loads(self):
        files = list(_migration_files())
        self.assertTrue(files)
        for version, path in files:
            module = _load(path, version)
            self.assertTrue(callable(getattr(module, "migrate", None)), path)

    def test_19_0_8_backfills_old_rows(self):
        Outbox = self.env["dz23.message.outbox"]
        sent = Outbox._enqueue(self.channel, "5561900003333", "resposta antiga")
        dead = Outbox._enqueue(self.channel, "5561900003333", "falhou antes")
        inbox, _created = self.env["dz23.message.inbox"]._enqueue(
            self.channel, "MID-MIGRACAO", "5561900003333", "oi", {"legado": True}
        )
        self.env.flush_all()
        cr = self.env.cr
        cr.execute(
            """UPDATE dz23_message_outbox
                  SET status = 'sent', current_status = 'queued', sent_at = NULL,
                      last_status_at = NULL, correlation_id = 'legado'
                WHERE id = %s""",
            (sent.id,),
        )
        cr.execute(
            """UPDATE dz23_message_outbox
                  SET status = 'dead', current_status = 'queued', failed_at = NULL,
                      correlation_id = 'legado'
                WHERE id = %s""",
            (dead.id,),
        )
        cr.execute(
            "UPDATE dz23_message_inbox SET payload_hash = NULL, payload_preview = NULL WHERE id = %s",
            (inbox.id,),
        )
        path = dict(_migration_files())["19.0.8.0.0"]
        migration = _load(path, "19.0.8.0.0")

        migration.migrate(cr, "19.0.7.0.0")
        self.env.invalidate_all()
        self.assertEqual(sent.current_status, "sent")
        self.assertTrue(sent.sent_at and sent.last_status_at)
        self.assertEqual(dead.current_status, "failed")
        self.assertTrue(dead.failed_at)
        self.assertNotEqual(sent.correlation_id, dead.correlation_id)
        self.assertNotEqual(sent.correlation_id, "legado")
        self.assertEqual(
            inbox.payload_hash, hashlib.sha256((inbox.payload or "").encode()).hexdigest()
        )

        migration.migrate(cr, "19.0.7.0.0")  # reexecução não regride nada
        self.env.invalidate_all()
        self.assertEqual((sent.current_status, dead.current_status), ("sent", "failed"))
        self.assertEqual(sent.body, "resposta antiga", "migração não destrutiva")
