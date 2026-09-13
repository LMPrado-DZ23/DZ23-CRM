# Camada de serviço para APIs públicas gratuitas do Brasil.
# Fonte padrão: BrasilAPI (https://brasilapi.com.br). Sem chave/segredo.
# Todas as chamadas têm timeout curto e tratamento de erro (nunca quebra o form).
import logging
import re

import requests
from odoo import api, models
from odoo.exceptions import UserError
from odoo.tools.translate import _

_logger = logging.getLogger(__name__)

_TIMEOUT = 8  # segundos


def only_digits(value):
    return re.sub(r"\D", "", value or "")


def _route(url):
    """Rota consultada sem query e sem os identificadores (CNPJ/CEP/ano)."""
    return re.sub(r"\d+", "#", (url or "").split("?", 1)[0])


class DZ23BrasilApi(models.AbstractModel):
    _name = "dz23.brasil.api"
    _description = "DZ23 — Serviço de APIs públicas do Brasil (BrasilAPI/OSM)"

    def _base_url(self):
        return (
            self.env["ir.config_parameter"]
            .sudo()
            .get_param("dz23.brasilapi_base", "https://brasilapi.com.br/api")
        ).rstrip("/")

    def _get_json(self, url):
        """GET com timeout; devolve dict ou levanta UserError amigável."""
        try:
            resp = requests.get(url, timeout=_TIMEOUT, headers={"Accept": "application/json"})
        except requests.exceptions.Timeout:
            raise UserError(_("A consulta demorou demais (timeout). Tente novamente.")) from None
        except requests.exceptions.RequestException as e:
            # Sem a exceção crua: ela traz a URL com CNPJ/CEP consultado (dado pessoal).
            _logger.warning("DZ23 BrasilAPI erro de rede (%s) em %s", type(e).__name__, _route(url))
            raise UserError(
                _("Não foi possível consultar a API agora. Verifique a conexão.")
            ) from None
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            _logger.info("DZ23 BrasilAPI status %s em %s", resp.status_code, _route(url))
            raise UserError(
                _("Consulta não encontrada ou inválida (código %s).") % resp.status_code
            )
        try:
            return resp.json()
        except ValueError:
            raise UserError(_("Resposta inválida da API.")) from None

    # ---- CEP -> endereço ----
    @api.model
    def fetch_cep(self, cep):
        cep = only_digits(cep)
        if len(cep) != 8:
            return None
        data = self._get_json("%s/cep/v2/%s" % (self._base_url(), cep))
        if not data:
            return None
        return {
            "zip": cep,
            "street_name": data.get("street") or "",
            "district": data.get("neighborhood") or "",
            "city": data.get("city") or "",
            "state_code": (data.get("state") or "").upper(),
        }

    # ---- CNPJ -> cadastro + enriquecimento ----
    @api.model
    def fetch_cnpj(self, cnpj):
        cnpj = only_digits(cnpj)
        if len(cnpj) != 14:
            return None
        data = self._get_json("%s/cnpj/v1/%s" % (self._base_url(), cnpj))
        if not data:
            return None
        cnae = ""
        if data.get("cnae_fiscal"):
            cnae = "%s - %s" % (data.get("cnae_fiscal"), data.get("cnae_fiscal_descricao") or "")
        return {
            "legal_name": data.get("razao_social") or "",
            "trade_name": data.get("nome_fantasia") or "",
            "street_name": " ".join(
                filter(None, [data.get("descricao_tipo_de_logradouro"), data.get("logradouro")])
            ),
            "number": data.get("numero") or "",
            "district": data.get("bairro") or "",
            "city": data.get("municipio") or "",
            "state_code": (data.get("uf") or "").upper(),
            "zip": only_digits(data.get("cep")),
            "phone": data.get("ddd_telefone_1") or "",
            "email": (data.get("email") or "").lower(),
            # enriquecimento
            "situacao": data.get("descricao_situacao_cadastral") or "",
            "porte": data.get("porte") or "",
            "cnae": cnae,
            "natureza": data.get("natureza_juridica") or "",
            "simples": bool(data.get("opcao_pelo_simples") or False),
            "mei": bool(data.get("opcao_pelo_mei") or False),
        }

    # ---- Feriados nacionais (para automações/agenda) ----
    @api.model
    def fetch_feriados(self, year):
        data = self._get_json("%s/feriados/v1/%s" % (self._base_url(), int(year)))
        return data or []

    # ---- Câmbio (AwesomeAPI, gratuito) ----
    @api.model
    def fetch_cambio(self, pair="USD-BRL"):
        pair = re.sub(r"[^A-Za-z-]", "", pair or "USD-BRL")
        url = "https://economia.awesomeapi.com.br/last/%s" % pair
        data = self._get_json(url)
        if not data:
            return None
        key = pair.replace("-", "")
        return data.get(key)
