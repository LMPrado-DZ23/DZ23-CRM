# Governança da IA (Fase 8), com requests.post simulado (sem rede e sem chaves reais):
# timeout por provedor, uso/custo registrados, 429 abre o circuit breaker, falhas
# seguidas abrem e o teste (half-open) fecha, limites diário e mensal por empresa,
# consentimento e provedor por empresa, erro de configuração não abre o breaker e
# o resumo de lead não envia e-mail/telefone para IA externa.
from datetime import timedelta
from unittest.mock import MagicMock, patch

import requests
from odoo import SUPERUSER_ID, api, fields
from odoo.addons.dz23_ai.models.ai_service import (
    AICircuitOpenError,
    AILimitExceededError,
    AIRateLimitedError,
)
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

_POST = "odoo.addons.dz23_ai.models.ai_service.requests.post"


def _resp(status=200, body=None, headers=None):
    resp = MagicMock()
    resp.status_code = status
    resp.headers = headers or {}
    resp.json.return_value = body if body is not None else {}
    return resp


def _openai_ok(text="Olá!", prompt_tokens=1000, completion_tokens=500):
    return _resp(
        body={
            "choices": [{"message": {"content": text}}],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
        }
    )


@tagged("post_install", "-at_install", "dz23")
class TestAIGovernanceRealCursor(TransactionCase):
    """Auditoria A-6: SEM o modo de teste do registry o cursor de governança é uma conexão
    real. Prova que o breaker aberto sobrevive ao rollback da transação do chamador."""

    def test_breaker_survives_caller_rollback(self):
        registry = self.env.registry
        company_id = self.env.company.id
        purpose = "teste_cursor_real_qa12"
        try:
            with registry.cursor() as caller_cr:
                env = api.Environment(caller_cr, SUPERUSER_ID, {})
                params = env["ir.config_parameter"].sudo()
                params.set_param("dz23.ai_provider", "openai")
                params.set_param("dz23.ai.external_allowed", "1")
                params.set_param("dz23.ai.openai_key", "chave-de-teste")
                with patch(_POST, return_value=_resp(429, headers={"Retry-After": "120"})):
                    try:
                        with caller_cr.savepoint():
                            env["dz23.ai"].chat("oi", purpose=purpose)
                    except AIRateLimitedError:
                        pass
                caller_cr.rollback()  # o chamador desfaz TUDO o que era dele
            with registry.cursor() as check_cr:
                check_cr.execute(
                    "SELECT state FROM dz23_ai_breaker WHERE company_id = %s AND provider = %s",
                    (company_id, "openai"),
                )
                row = check_cr.fetchone()
            self.assertEqual(row and row[0], "open")
        finally:
            with registry.cursor() as clean_cr:
                clean_cr.execute(
                    "DELETE FROM dz23_ai_breaker WHERE company_id = %s AND provider = %s",
                    (company_id, "openai"),
                )
                clean_cr.execute("DELETE FROM dz23_ai_usage WHERE purpose = %s", (purpose,))
            registry.clear_cache()  # o cache de parâmetros não pode herdar valor desfeito


@tagged("post_install", "-at_install", "dz23")
class TestAIGovernance(TransactionCase):
    def setUp(self):
        super().setUp()
        # Breaker/uso/limites gravam em cursor próprio (sobrevivem ao rollback do
        # chamador); em modo de teste esse cursor fica dentro da transação do teste.
        self.registry_enter_test_mode()
        self.company = self.env.company
        self.company.write(
            {
                "dz23_ai_provider": False,
                "dz23_ai_model": False,
                "dz23_ai_external_policy": "inherit",
                "dz23_ai_daily_call_limit": 0,
                "dz23_ai_monthly_cost_limit_usd": 0.0,
            }
        )
        self.ICP = self.env["ir.config_parameter"].sudo()
        self.ICP.set_param("dz23.ai_provider", "openai")
        self.ICP.set_param("dz23.ai_model", "gpt-4o-mini")
        self.ICP.set_param("dz23.ai.external_allowed", "1")
        self.ICP.set_param("dz23.ai.openai_key", "chave-de-teste")
        self.AI = self.env["dz23.ai"]
        self.Usage = self.env["dz23.ai.usage"]
        self.Breaker = self.env["dz23.ai.breaker"]

    def _raises(self, exc_class, func, *args):
        """Como assertRaises, mas SEM o savepoint com rollback do Odoo: aqui o ponto é
        justamente provar que o estado gravado sobrevive à exceção."""
        try:
            func(*args)
        except exc_class as error:
            return error
        self.fail("%s não foi levantada" % exc_class.__name__)

    def _breaker(self, provider="openai"):
        return self.Breaker.search(
            [("company_id", "=", self.company.id), ("provider", "=", provider)]
        )

    def test_per_provider_timeout(self):
        self.ICP.set_param("dz23.ai.timeout.openai", "7")
        with patch(_POST, return_value=_openai_ok()) as post:
            self.AI.chat("oi")
        self.assertEqual(post.call_args.kwargs["timeout"], 7)

    def test_usage_and_estimated_cost_are_recorded(self):
        with patch(_POST, return_value=_openai_ok()):
            result = self.AI._chat("oi", purpose="teste")
        usage = self.Usage.browse(result["usage_id"])
        self.assertEqual((usage.status, usage.input_tokens, usage.output_tokens), ("ok", 1000, 500))
        self.assertAlmostEqual(usage.cost_usd, (1000 * 0.15 + 500 * 0.60) / 1_000_000, places=9)
        self.assertEqual(usage.model_name, "gpt-4o-mini")
        self.assertEqual(usage.purpose, "teste")

    def test_rate_limit_opens_breaker_and_blocks_next_call(self):
        with patch(_POST, return_value=_resp(429, headers={"Retry-After": "120"})) as post:
            self._raises(AIRateLimitedError, self.AI.chat, "oi")
            self._raises(AICircuitOpenError, self.AI.chat, "oi de novo")
        self.assertEqual(post.call_count, 1, "com o breaker aberto a chamada nem sai")
        breaker = self._breaker()
        self.assertEqual(breaker.state, "open")
        self.assertGreaterEqual(breaker.open_until, fields.Datetime.now() + timedelta(seconds=119))
        self.assertEqual(self.Usage.search_count([("status", "=", "rate_limited")]), 1)

    def test_consecutive_failures_open_then_half_open_recovers(self):
        with patch(_POST, side_effect=requests.exceptions.ConnectionError("x")):
            for _i in range(3):
                self._raises(UserError, self.AI.chat, "oi")
        breaker = self._breaker()
        self.assertEqual((breaker.state, breaker.failure_count), ("open", 3))
        breaker.open_until = fields.Datetime.now() - timedelta(seconds=1)
        with patch(_POST, return_value=_openai_ok()):
            self.assertEqual(self.AI.chat("oi"), "Olá!")
        breaker.invalidate_recordset()
        self.assertEqual((breaker.state, breaker.failure_count), ("closed", 0))

    def test_daily_call_limit_per_company(self):
        self.company.dz23_ai_daily_call_limit = 1
        with patch(_POST, return_value=_openai_ok()) as post:
            self.AI.chat("primeira")
            self._raises(AILimitExceededError, self.AI.chat, "segunda")
        self.assertEqual(post.call_count, 1)
        self.assertEqual(self.Usage.search_count([("status", "=", "blocked")]), 1)

    def test_monthly_cost_limit_per_company(self):
        self.company.dz23_ai_monthly_cost_limit_usd = 5.0
        self.Usage._log(self.company, "openai", "gpt-4o-mini", "teste", status="ok", cost_usd=5.0)
        with patch(_POST) as post:
            with self.assertRaises(AILimitExceededError):
                self.AI.chat("oi")
        self.assertEqual(post.call_count, 0)

    def test_company_policy_overrides_global_consent(self):
        self.company.dz23_ai_external_policy = "deny"
        with patch(_POST) as post:
            with self.assertRaises(UserError) as error:
                self.AI.chat("oi")
        self.assertIn("política", str(error.exception).lower())
        self.assertEqual(post.call_count, 0)

    def test_company_provider_overrides_global(self):
        self.company.dz23_ai_provider = "ollama"
        self.ICP.set_param("dz23.ai.ollama_base", "http://ollama.local:11434")
        body = {"message": {"content": "local!"}, "prompt_eval_count": 12, "eval_count": 3}
        with patch(_POST, return_value=_resp(body=body)) as post:
            self.assertEqual(self.AI.chat("oi"), "local!")
        self.assertTrue(post.call_args.args[0].startswith("http://ollama.local:11434"))

    def test_config_error_does_not_open_breaker(self):
        self.ICP.set_param("dz23.ai.openai_key", "")
        error = self._raises(UserError, self.AI.chat, "oi")
        self.assertIn("chave", str(error).lower())
        self.assertEqual(self._breaker().failure_count, 0)

    def test_lead_summary_sends_no_contact_data_to_external_ai(self):
        lead = self.env["crm.lead"].create(
            {
                "name": "Lead QA IA",
                "contact_name": "Maria QA",
                "email_from": "maria.qa@example.com",
                "phone": "+55 61 99999-8888",
                "description": "Quer orçamento de reforma",
            }
        )
        with patch(_POST, return_value=_openai_ok("Resumo")) as post:
            lead.action_dz23_ai_summary()
        sent = str(post.call_args.kwargs["json"])
        self.assertNotIn("maria.qa@example.com", sent)
        self.assertNotIn("99999-8888", sent)
        self.assertIn("reforma", sent)

    def test_ollama_on_public_host_is_treated_as_external(self):
        # Auditoria B-7: "local" é onde o dado vai, não o nome do provedor.
        self.company.dz23_ai_provider = "ollama"
        self.ICP.set_param("dz23.ai.external_allowed", "0")
        self.ICP.set_param("dz23.ai.ollama_base", "https://ollama.exemplo-publico.com.br")
        self.assertTrue(self.AI._is_external(self.company))
        with patch(_POST) as post:
            error = self._raises(UserError, self.AI.chat, "oi")
        self.assertIn("política", str(error).lower())
        self.assertEqual(post.call_count, 0)
        for base in ("http://localhost:11434", "http://ollama:11434", "http://10.0.0.5:11434"):
            self.ICP.set_param("dz23.ai.ollama_base", base)
            self.assertFalse(self.AI._is_external(self.company), base)

    def test_usage_retention_purge(self):
        old = self.Usage._log(self.company, "ollama", "m", "teste", status="ok")
        recent = self.Usage._log(self.company, "ollama", "m", "teste", status="ok")
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE dz23_ai_usage SET create_date = now() - interval '500 days' WHERE id = %s",
            (old.id,),
        )
        self.Usage.invalidate_model(["create_date"])
        self.assertEqual(self.Usage._cron_purge(), 1)
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())
