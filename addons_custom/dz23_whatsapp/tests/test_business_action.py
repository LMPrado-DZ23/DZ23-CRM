# Ações de negócio idempotentes (ADR-005): executa uma vez por chave, falha não
# registra (retry executa de novo) e concorrência na mesma chave espera e reutiliza.
import uuid

import psycopg2
from odoo import SUPERUSER_ID, api
from odoo.tests import TransactionCase, tagged
from odoo.tools import mute_logger


@tagged("post_install", "-at_install", "dz23")
class TestBusinessAction(TransactionCase):
    def setUp(self):
        super().setUp()
        self.Action = self.env["dz23.business.action"]
        self.calls = 0

    def _create_partner(self):
        self.calls += 1
        return self.env["res.partner"].create({"name": "Alvo QA %s" % self.calls})

    def test_runs_once_and_returns_same_target(self):
        key = "test:%s" % uuid.uuid4().hex
        a1, executed1 = self.Action._run_once(key, "other", self._create_partner)
        a2, executed2 = self.Action._run_once(key, "other", self._create_partner)
        self.assertTrue(executed1)
        self.assertFalse(executed2)
        self.assertEqual(a1, a2)
        self.assertEqual(self.calls, 1)
        self.assertEqual(a1.state, "done")
        self.assertEqual(a1._target()._name, "res.partner")
        self.assertTrue(a1.completed_at)

    def test_failure_is_not_recorded_and_retry_executes(self):
        key = "test:%s" % uuid.uuid4().hex

        def _boom():
            raise ValueError("falhou")

        with self.assertRaises(ValueError):
            self.Action._run_once(key, "other", _boom)
        self.assertEqual(self.Action.search_count([("idempotency_key", "=", key)]), 0)
        _action, executed = self.Action._run_once(key, "other", self._create_partner)
        self.assertTrue(executed)

    def test_distinct_keys_run_separately(self):
        self.Action._run_once("test:%s" % uuid.uuid4().hex, "other", self._create_partner)
        self.Action._run_once("test:%s" % uuid.uuid4().hex, "other", self._create_partner)
        self.assertEqual(self.calls, 2)

    def test_key_is_required(self):
        with self.assertRaises(ValueError):
            self.Action._run_once("", "other", self._create_partner)

    def test_concurrent_same_key_waits_then_reuses(self):
        key = "test:concorrente:%s" % uuid.uuid4().hex
        cr_a = self.registry.cursor()
        cr_b = self.registry.cursor()
        try:
            env_a = api.Environment(cr_a, SUPERUSER_ID, {})
            _action, executed_a = env_a["dz23.business.action"]._run_once(
                key, "other", lambda: None
            )
            self.assertTrue(executed_a)
            # B tenta a MESMA chave enquanto A não commitou: fica esperando o índice
            # único (aqui limitado por lock_timeout para o teste não travar).
            cr_b.execute("SET LOCAL lock_timeout = '300ms'")
            env_b = api.Environment(cr_b, SUPERUSER_ID, {})
            with (
                mute_logger("odoo.sql_db"),
                self.assertRaises(psycopg2.errors.LockNotAvailable),
            ):
                env_b["dz23.business.action"]._run_once(key, "other", lambda: None)
            cr_b.rollback()
            cr_a.commit()
            calls = {"n": 0}

            def _count():
                calls["n"] += 1

            env_b = api.Environment(cr_b, SUPERUSER_ID, {})
            _action_b, executed_b = env_b["dz23.business.action"]._run_once(key, "other", _count)
            self.assertFalse(executed_b, "depois do commit de A, B reutiliza a ação concluída")
            self.assertEqual(calls["n"], 0)
            cr_b.rollback()
        finally:
            cr_a.rollback()
            cr_b.rollback()
            cr_a.close()
            cr_b.close()
            with self.registry.cursor() as cr_clean:
                cr_clean.execute(
                    "DELETE FROM dz23_business_action WHERE idempotency_key = %s", (key,)
                )
                cr_clean.commit()
