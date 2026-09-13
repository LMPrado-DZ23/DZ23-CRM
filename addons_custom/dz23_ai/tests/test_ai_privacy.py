# Privacidade da IA (MEDIUM-02/03): externo negado por padrão; PII redigida
# antes de sair; chave Gemini fora da URL; logger não vaza query string.
from odoo.addons.dz23_ai.models.ai_service import _redact_pii, _safe_url
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install", "dz23")
class TestAIPrivacy(TransactionCase):
    def setUp(self):
        super().setUp()
        # A governança (breaker/uso) grava em cursor próprio; em teste, na mesma transação.
        self.registry_enter_test_mode()
        self.ICP = self.env["ir.config_parameter"].sudo()

    def test_external_denied_by_default(self):
        self.ICP.set_param("dz23.ai_provider", "groq")
        self.ICP.set_param("dz23.ai.external_allowed", "0")
        with self.assertRaises(UserError) as e:
            self.env["dz23.ai"].chat("oi")
        self.assertIn("política", str(e.exception).lower())

    def test_external_allowed_passes_gate(self):
        # Com política ligada e SEM chave, deve passar do gate e falhar por
        # falta de chave (prova que o gate liberou), não por política.
        self.ICP.set_param("dz23.ai_provider", "groq")
        self.ICP.set_param("dz23.ai.external_allowed", "1")
        self.ICP.set_param("dz23.ai.groq_key", "")
        with self.assertRaises(UserError) as e:
            self.env["dz23.ai"].chat("oi")
        self.assertIn("chave", str(e.exception).lower())

    def test_local_provider_not_gated(self):
        # Ollama (local) nunca é bloqueado por política; falha só por rede.
        self.ICP.set_param("dz23.ai_provider", "ollama")
        self.ICP.set_param("dz23.ai.ollama_base", "http://nao-existe.local:11434")
        with self.assertRaises(UserError) as e:
            self.env["dz23.ai"].chat("oi")
        self.assertNotIn("política", str(e.exception).lower())

    def test_redact_pii(self):
        red = _redact_pii("fale com joao@x.com ou 5561999998888, cpf 123.456.789-00")
        self.assertNotIn("joao@x.com", red)
        self.assertNotIn("999998888", red)
        self.assertIn("[email]", red)
        self.assertIn("[telefone]", red)
        self.assertIn("[documento]", red)

    def test_safe_url_strips_query(self):
        self.assertEqual(
            _safe_url("https://api.x.com/v1/gen?key=SEGREDO123&x=1"), "https://api.x.com/v1/gen"
        )
