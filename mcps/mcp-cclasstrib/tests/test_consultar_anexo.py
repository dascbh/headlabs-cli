"""Regression tests for the consultar_anexo tool (mcp-cclasstrib), added
after discovering /consultas/anexos — a second real endpoint on
cff.svrs.rs.gov.br that the MCP did not consult before 2026-07-04.

The fixture below is shaped exactly like a real response from
/consultas/anexos (field names, nesting, value formats), confirmed by
inspecting a live payload of 3991 rows via mTLS with a real ICP-Brasil
cert. It includes:
  - nroAnexo=1 (a numbered Anexo, product/NCM-based)
  - nroAnexo=91271 (a "composite" Anexo number, service-based —
    "Prestação de serviços de profissões intelectuais", the same Anexo
    referenced by cClassTrib 200052 in test_data_mapping.py)
  - one row with a non-null dthFimVig in the past (expired), to test the
    vigência filter
"""
import sys
from datetime import datetime

import pytest


_RAW_ANEXOS_PAYLOAD = [
    {
        "nroAnexo": 1, "codNcmNbs": "10062010", "TipoNomenclatura": "NCM",
        "TipoPermissao": "Permitido", "descrCondicao": "DESTINADOS À ALIMENTAÇÃO HUMANA",
        "descrExcecao": None, "texObservacao": None,
        "dthIniVig": "2026-01-01T00:00:00", "dthFimVig": None,
        "nroItemAnexoLei": 1,
        "descrItemAnexo": "Arroz das subposições 1006.20 e 1006.30 e do código 1006.40.00 da NCM/SH",
        "descrAnexo": "PRODUTOS DESTINADOS À ALIMENTAÇÃO HUMANA SUBMETIDOS À REDUÇÃO A ZERO DAS ALÍQUOTAS DO IBS E DA CBS",
    },
    {
        "nroAnexo": 91271, "codNcmNbs": None, "TipoNomenclatura": "NBS",
        "TipoPermissao": "Permitido", "descrCondicao": None,
        "descrExcecao": None, "texObservacao": None,
        "dthIniVig": "2026-01-01T00:00:00", "dthFimVig": None,
        "nroItemAnexoLei": 5,
        "descrItemAnexo": "Serviços de consultoria financeira",
        "descrAnexo": "Prestação de serviços de profissões intelectuais",
    },
    {
        "nroAnexo": 91271, "codNcmNbs": None, "TipoNomenclatura": "NBS",
        "TipoPermissao": "Permitido", "descrCondicao": None,
        "descrExcecao": None, "texObservacao": None,
        "dthIniVig": "2020-01-01T00:00:00", "dthFimVig": "2024-12-31T00:00:00",  # expired
        "nroItemAnexoLei": 2,
        "descrItemAnexo": "Item expirado, não deve aparecer com apenasVigentes",
        "descrAnexo": "Prestação de serviços de profissões intelectuais",
    },
]


@pytest.fixture()
def server(monkeypatch):
    sys.path.insert(0, "mcps/mcp-cclasstrib" if __import__("os").path.isdir("mcps/mcp-cclasstrib") else ".")
    if "server" in sys.modules:
        del sys.modules["server"]
    import server as srv  # noqa: PLC0415

    def _fake_fetch_anexos():
        srv._anexos_cache = list(_RAW_ANEXOS_PAYLOAD)
        srv._anexos_updated_at = datetime.utcnow()
        return {"success": True}

    monkeypatch.setattr(srv, "_fetch_anexos", _fake_fetch_anexos)
    return srv


def test_requires_at_least_one_filter(server):
    r = server.consultar_anexo()
    assert r["erro"]["codigo"] == "PARAMETRO_INVALIDO"


def test_limite_out_of_range_rejected(server):
    r = server.consultar_anexo(nroAnexo=1, limite=0)
    assert r["erro"]["codigo"] == "PARAMETRO_INVALIDO"
    r = server.consultar_anexo(nroAnexo=1, limite=201)
    assert r["erro"]["codigo"] == "PARAMETRO_INVALIDO"


def test_filter_by_numbered_anexo(server):
    r = server.consultar_anexo(nroAnexo=1)
    assert r["total"] == 1
    assert r["itens"][0]["descrAnexo"].startswith("PRODUTOS DESTINADOS")
    assert r["itens"][0]["codNcmNbs"] == "10062010"


def test_filter_by_composite_anexo_number(server):
    """nroAnexo=91271 is a real, catalogued Anexo number — NOT an informal
    '9+artigo+1' pattern as initially (incorrectly) assumed. It must return
    real rows, not be treated as invalid or synthetic."""
    r = server.consultar_anexo(nroAnexo=91271, apenasVigentes=False)
    assert r["total"] == 2
    assert all(i["descrAnexo"] == "Prestação de serviços de profissões intelectuais" for i in r["itens"])


def test_unknown_anexo_number_returns_empty_not_invented(server):
    """anexo values seen in classTrib that have no match in this catalog
    (e.g. 2, 3, 91721-91726 in the real data) must return zero results —
    never a fabricated or guessed description."""
    r = server.consultar_anexo(nroAnexo=2)
    assert r["total"] == 0
    assert r["itens"] == []


def test_busca_by_free_text(server):
    r = server.consultar_anexo(busca="profissões intelectuais", apenasVigentes=False)
    assert r["total"] == 2


def test_busca_matches_item_description_too(server):
    r = server.consultar_anexo(busca="consultoria financeira")
    assert r["total"] == 1
    assert r["itens"][0]["nroItemAnexoLei"] == 5


def test_apenas_vigentes_filters_expired_by_default(server):
    r = server.consultar_anexo(nroAnexo=91271)
    assert r["total"] == 1
    assert r["itens"][0]["nroItemAnexoLei"] == 5  # the non-expired one


def test_filter_by_codNcmNbs(server):
    r = server.consultar_anexo(codNcmNbs="10062010")
    assert r["total"] == 1
    assert r["itens"][0]["nroAnexo"] == 1


def test_truncation_flag(server):
    r = server.consultar_anexo(nroAnexo=91271, apenasVigentes=False, limite=1)
    assert r["total"] == 2
    assert r["truncado"] is True
    assert len(r["itens"]) == 1


def test_status_cache_reports_anexos_not_loaded_before_first_use(server, monkeypatch):
    def _fake_fetch_upstream():
        server._cache_data = []
        server._cache_cst_groups = []
        server._cache_updated_at = datetime.utcnow()
        server._cache_origin = "rede"
        return {"success": True}
    monkeypatch.setattr(server, "_fetch_upstream", _fake_fetch_upstream)

    r = server.status_cache()
    assert r["cacheAnexos"]["carregado"] is False


def test_status_cache_reports_anexos_after_first_use(server, monkeypatch):
    def _fake_fetch_upstream():
        server._cache_data = []
        server._cache_cst_groups = []
        server._cache_updated_at = datetime.utcnow()
        server._cache_origin = "rede"
        return {"success": True}
    monkeypatch.setattr(server, "_fetch_upstream", _fake_fetch_upstream)

    server.consultar_anexo(nroAnexo=1)
    r = server.status_cache()
    assert r["cacheAnexos"]["carregado"] is True
    assert r["cacheAnexos"]["totalRegistros"] == len(_RAW_ANEXOS_PAYLOAD)
