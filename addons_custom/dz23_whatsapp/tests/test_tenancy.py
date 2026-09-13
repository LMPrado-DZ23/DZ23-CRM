# Provas adversariais de isolamento multi-tenant do dz23.channel:
# um usuário comum da empresa A não enxerga nem lê canais da empresa B.
from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install", "dz23")
class TestChannelTenancy(TransactionCase):
    def setUp(self):
        super().setUp()
        Company = self.env["res.company"]
        self.cA = Company.create({"name": "Tenant A DZ23"})
        self.cB = Company.create({"name": "Tenant B DZ23"})
        Ch = self.env["dz23.channel"]
        self.chA = Ch.create({"name": "Canal A", "company_id": self.cA.id, "provider": "evolution"})
        self.chB = Ch.create({"name": "Canal B", "company_id": self.cB.id, "provider": "evolution"})
        self.userA = self.env["res.users"].create(
            {
                "name": "Usuário A",
                "login": "ua_dz23_test",
                "company_id": self.cA.id,
                "company_ids": [(6, 0, [self.cA.id])],
                "group_ids": [(6, 0, [self.env.ref("base.group_user").id])],
            }
        )

    def test_search_hides_other_company(self):
        visiveis = self.env["dz23.channel"].with_user(self.userA).search([])
        self.assertIn(self.chA, visiveis)
        self.assertNotIn(self.chB, visiveis, "usuário de A não pode listar canal de B")

    def test_read_other_company_denied(self):
        with self.assertRaises(AccessError):
            self.chB.with_user(self.userA).read(["name"])

    def test_token_uniqueness(self):
        # tokens são gerados distintos por canal
        self.assertNotEqual(self.chA.webhook_token, self.chB.webhook_token)
        self.assertNotEqual(self.chA.callback_secret, self.chB.callback_secret)

    def test_provider_channel_id_uniqueness(self):
        # Mesma instância Evolution em dois canais é proibida (validação dispara
        # antes do SQL): esperamos ValidationError.
        from odoo.exceptions import ValidationError

        self.chA.evo_instance = "inst_dup"
        self.chA.flush_recordset()
        with self.assertRaises(ValidationError):
            self.chB.evo_instance = "inst_dup"
            self.chB.flush_recordset()

    def test_contact_channel_cannot_move_to_other_company(self):
        # Auditoria B-5: trocar o canal mudaria a empresa (e vazaria a conversa).
        from odoo.exceptions import UserError

        contact = self.env["dz23.channel.contact"]._get_or_create(self.chA, "5561900005555")
        with self.assertRaises(UserError):
            contact.write({"channel_id": self.chB.id})
        conversation = self.env["dz23.conversation"]._for_contact(contact)
        other = self.env["dz23.channel.contact"]._get_or_create(self.chB, "5561900005555")
        with self.assertRaises(UserError):
            conversation.write({"contact_id": other.id})

    def test_get_or_create_survives_concurrent_insert(self):
        # Auditoria A-4: o outro webhook já inseriu o contato (a busca não o viu).
        Contact = self.env["dz23.channel.contact"]
        existing = Contact._get_or_create(self.chA, "5561900004444")
        original_search = type(Contact).search
        calls = {"n": 0}

        def _stale_first_search(records, domain, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return records.browse()
            return original_search(records, domain, *args, **kwargs)

        self.patch(type(Contact), "search", _stale_first_search)
        from odoo.tools import mute_logger

        # A violação de unicidade é o cenário testado: o Postgres a registra como "bad query".
        with mute_logger("odoo.sql_db"):
            self.assertEqual(Contact._get_or_create(self.chA, "5561900004444"), existing)

    def test_same_phone_suffix_distinct_identities(self):
        # Mesmo número em canais/empresas diferentes => identidades DISTINTAS,
        # cada uma escopada ao seu canal/empresa (sem colisão global).
        Contact = self.env["dz23.channel.contact"]
        num = "5561999998888"
        cA = Contact._get_or_create(self.chA, num, num)
        cB = Contact._get_or_create(self.chB, num, num)
        self.assertNotEqual(cA.id, cB.id)
        self.assertEqual(cA.company_id, self.cA)
        self.assertEqual(cB.company_id, self.cB)
        # atendente de A (contatos são só do grupo Atendente) não enxerga a identidade de B
        self.userA.group_ids = [(4, self.env.ref("dz23_whatsapp.group_dz23_attendant").id)]
        visiveis = Contact.with_user(self.userA).search([("provider_user_id", "=", num)])
        self.assertIn(cA, visiveis)
        self.assertNotIn(cB, visiveis)
