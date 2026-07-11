"""Regression tests for mcp-cclasstrib's data mapping — covers the 5
corrections applied after auditing real payloads from cff.svrs.rs.gov.br
(via mTLS with a real ICP-Brasil cert) and cross-checking against real-world
usage data (Ferroeste/AVB). See CHANGELOG in ../spec-mcp-cclasstrib.md.

These tests populate the module's cache directly with a fixture shaped
EXACTLY like the real upstream payload (field names, nesting, value
formats) rather than mocking the network — the actual bug this whole
investigation started from was a mapper trusting an assumed shape that
didn't match the real API, so a test fixture with the same problem would
prove nothing. The fixture was built by inspecting a live response, not by
guessing.

One entry (CST 999) is a SYNTHETIC "CST with zero classifications" case —
the real API currently sends 18 CSTs, all with at least one classification
(confirmed 2026-07-03), so this scenario cannot be exercised against
production today. It's kept here specifically so the "include CSTs with no
active cClassTrib" behavior has a regression test independent of whether
the live API happens to have an empty CST at any given moment.
"""
import importlib
import sys
from datetime import datetime

import pytest


# Shaped exactly like a real cff.svrs.rs.gov.br response (subset): one CST
# with a presumed-credit ZFM indicator at the CST level but NOT at the
# classification level (the real bug case — CST 810), one ordinary CST with
# two classifications (for filtering/pagination), and one synthetic
# zero-classification CST (see module docstring).
_RAW_PAYLOAD = [
    {
        "CST": "810", "DescricaoCST": "Ajuste de IBS na ZFM",
        "IndIBSCBS": False, "IndRedBC": False, "IndRedAliq": False,
        "IndTransfCred": False, "IndDif": False, "IndAjusteCompet": False,
        "IndIBSCBSMono": False, "IndCredPresIBSZFM": True,
        "Publicacao": "2025-05-12T00:00:00", "InicioVigencia": "2025-05-01T00:00:00",
        "FimVigencia": None,
        "classificacoesTributarias": [
            {
                "cClassTrib": "810001", "DescricaoClassTrib": "Crédito presumido ZFM.",
                "pRedIBS": 0.0, "pRedCBS": 0.0,
                "IndTribRegular": False, "IndCredPresOper": False, "IndEstornoCred": False,
                "MonofasiaSujeitaRetencao": False, "MonofasiaRetidaAnt": False,
                "MonofasiaDiferimento": False, "MonofasiaPadrao": False,
                "Publicacao": "2026-06-22T00:00:00", "InicioVigencia": "2025-05-05T00:00:00",
                "FimVigencia": None, "TipoAliquota": "2 - Padrão",
                "IndNFe": True, "IndNFCe": False, "IndCTe": False, "IndCTeOS": False,
                "IndBPe": False, "IndNF3e": False, "IndNFCom": False, "IndNFSE": False,
                "IndBPeTM": False, "IndBPeTA": False, "IndNFAg": False, "IndNFSVIA": False,
                "IndNFABI": False, "IndNFGas": False, "IndDERE": False,
                "Anexo": None, "Link": "https://www.planalto.gov.br/ccivil_03/leis/lcp/lcp214.htm#art450",
                "IndPercentualDiferencaBioCombustivel": False, "IndDIR": False, "IndDUIMP": False,
                "TipoReceitaBrutaSN": "1 - Receita Bruta Interna",
            },
        ],
    },
    {
        "CST": "200", "DescricaoCST": "Alíquota reduzida",
        "IndIBSCBS": True, "IndRedBC": False, "IndRedAliq": True,
        "IndTransfCred": False, "IndDif": False, "IndAjusteCompet": False,
        "IndIBSCBSMono": False, "IndCredPresIBSZFM": False,
        "Publicacao": "2025-05-12T00:00:00", "InicioVigencia": "2025-05-01T00:00:00",
        "FimVigencia": None,
        "classificacoesTributarias": [
            {
                "cClassTrib": "200052",
                "DescricaoClassTrib": "Profissões intelectuais regulamentadas por conselho, observado o art. 127.",
                "pRedIBS": 30.0, "pRedCBS": 30.0,
                "IndTribRegular": False, "IndCredPresOper": False, "IndEstornoCred": False,
                "MonofasiaSujeitaRetencao": False, "MonofasiaRetidaAnt": False,
                "MonofasiaDiferimento": False, "MonofasiaPadrao": False,
                "Publicacao": "2026-06-22T00:00:00", "InicioVigencia": "2025-05-05T00:00:00",
                "FimVigencia": None, "TipoAliquota": "2 - Padrão",
                "IndNFe": False, "IndNFCe": False, "IndCTe": False, "IndCTeOS": False,
                "IndBPe": False, "IndNF3e": False, "IndNFCom": False, "IndNFSE": True,
                "IndBPeTM": False, "IndBPeTA": False, "IndNFAg": False, "IndNFSVIA": False,
                "IndNFABI": False, "IndNFGas": False, "IndDERE": False,
                "Anexo": 91271, "Link": "https://www.planalto.gov.br/ccivil_03/leis/lcp/lcp214.htm#art127",
                "IndPercentualDiferencaBioCombustivel": False, "IndDIR": True, "IndDUIMP": False,
                "TipoReceitaBrutaSN": "1 - Receita Bruta Interna",
            },
            {
                "cClassTrib": "200003",
                "DescricaoClassTrib": "Outra classificação do CST 200, para testar filtro/paginação.",
                "pRedIBS": 10.0, "pRedCBS": 10.0,
                "IndTribRegular": True, "IndCredPresOper": False, "IndEstornoCred": False,
                "MonofasiaSujeitaRetencao": False, "MonofasiaRetidaAnt": False,
                "MonofasiaDiferimento": True, "MonofasiaPadrao": False,
                "Publicacao": "2026-06-22T00:00:00", "InicioVigencia": "2025-05-05T00:00:00",
                "FimVigencia": None, "TipoAliquota": "5 - Uniforme Setorial",
                "IndNFe": True, "IndNFCe": True, "IndCTe": False, "IndCTeOS": False,
                "IndBPe": False, "IndNF3e": False, "IndNFCom": False, "IndNFSE": False,
                "IndBPeTM": False, "IndBPeTA": False, "IndNFAg": False, "IndNFSVIA": False,
                "IndNFABI": False, "IndNFGas": False, "IndDERE": False,
                "Anexo": 1, "Link": "https://www.planalto.gov.br/ccivil_03/leis/lcp/lcp214.htm#art125",
                "IndPercentualDiferencaBioCombustivel": False, "IndDIR": False, "IndDUIMP": False,
                "TipoReceitaBrutaSN": "0 - Não Receita Bruta",
            },
        ],
    },
    {
        # SYNTHETIC — see module docstring. Not observed in the live API as
        # of 2026-07-03 (all 18 real CSTs currently have >=1 classification).
        "CST": "999", "DescricaoCST": "CST sintético sem classificações (regressão)",
        "IndIBSCBS": False, "IndRedBC": False, "IndRedAliq": False,
        "IndTransfCred": False, "IndDif": False, "IndAjusteCompet": False,
        "IndIBSCBSMono": False, "IndCredPresIBSZFM": False,
        "Publicacao": "2025-05-12T00:00:00", "InicioVigencia": "2025-05-01T00:00:00",
        "FimVigencia": None,
        "classificacoesTributarias": [],
    },
]


@pytest.fixture()
def server(monkeypatch):
    """Import a fresh copy of server.py with the network call replaced by
    the fixture above, and module-level cache state reset between tests."""
    sys.path.insert(0, "mcps/mcp-cclasstrib" if __import__("os").path.isdir("mcps/mcp-cclasstrib") else ".")
    if "server" in sys.modules:
        del sys.modules["server"]
    import server as srv  # noqa: PLC0415

    def _fake_fetch_upstream():
        srv._cache_data = []
        srv._cache_cst_groups = []
        for cst_group in _RAW_PAYLOAD:
            for classe in cst_group["classificacoesTributarias"]:
                merged = dict(classe)
                merged["_cst"] = cst_group["CST"]
                merged["_descricaoCst"] = cst_group["DescricaoCST"]
                merged["_cstIndIBSCBS"] = cst_group["IndIBSCBS"]
                merged["_cstIndRedBC"] = cst_group["IndRedBC"]
                merged["_cstIndRedAliq"] = cst_group["IndRedAliq"]
                merged["_cstIndTransfCred"] = cst_group["IndTransfCred"]
                merged["_cstIndDif"] = cst_group["IndDif"]
                merged["_cstIndAjusteCompet"] = cst_group["IndAjusteCompet"]
                merged["_cstIndIBSCBSMono"] = cst_group["IndIBSCBSMono"]
                merged["_cstIndCredPresIBSZFM"] = cst_group["IndCredPresIBSZFM"]
                srv._cache_data.append(merged)
            srv._cache_cst_groups.append(
                {k: v for k, v in cst_group.items() if k != "classificacoesTributarias"}
            )
        srv._cache_updated_at = datetime.utcnow()
        srv._cache_origin = "rede"
        return {"success": True}

    monkeypatch.setattr(srv, "_fetch_upstream", _fake_fetch_upstream)
    return srv


# ── Correction #1: ind_gpBioDiferenca -> ind_gMonoDif ─────────────────────────

def test_ind_gMonoDif_present_and_old_name_gone(server):
    r = server.consultar_classificacao_tributaria(limite=10, apenasVigentes=False)
    item = next(i for i in r["itens"] if i["cClassTrib"] == "200003")
    assert "ind_gMonoDif" in item["indicadoresGrupo"]
    assert "ind_gpBioDiferenca" not in item["indicadoresGrupo"]
    assert item["indicadoresGrupo"]["ind_gMonoDif"] is True  # MonofasiaDiferimento in fixture


# ── Correction #2/#3: CST-level indicators exposed ────────────────────────────

def test_cst_810_exposes_presumed_credit_zfm_in_listar_cst(server):
    r = server.listar_cst(comContagem=True)
    cst_810 = next(i for i in r["itens"] if i["cst"] == "810")
    assert cst_810["indicadoresGrupo"]["ind_gCredPresIBSZFM"] is True
    assert cst_810["qtdeClassificacoes"] == 1


def test_cclasstrib_810001_no_longer_a_false_negative(server):
    """The real-world bug: reading only the classification-level
    IndCredPresOper for 810001 says 'no presumed credit' when the CST-level
    IndCredPresIBSZFM says otherwise. The record must expose the CST-level
    signal too so a caller can't be misled by checking the wrong field."""
    r = server.detalhar_cclasstrib("810001")
    assert r["indicadoresGrupo"]["ind_gCredPresOper"] is False   # unrelated indicator, correctly false
    assert r["indicadoresCst"]["ind_gCredPresIBSZFM"] is True    # the actual signal


def test_synthetic_cst_with_zero_classifications_still_listed(server):
    """Regression for the 'CST disappears if it has no active cClassTrib'
    bug — exercised via the synthetic CST 999 since the real API doesn't
    currently have an empty CST (see module docstring)."""
    r = server.listar_cst(comContagem=True)
    cst_999 = next((i for i in r["itens"] if i["cst"] == "999"), None)
    assert cst_999 is not None, "CST com zero classificações desapareceu de listar_cst"
    assert cst_999["qtdeClassificacoes"] == 0


# ── Correction #4: tipoAliquota / tpRBSN split into codigo + texto ────────────

def test_tipo_aliquota_and_tp_rbsn_split(server):
    r = server.detalhar_cclasstrib("200052")
    assert r["tipoAliquotaCodigo"] == "2"
    assert r["tipoAliquota"] == "Padrão"
    assert r["tpRBSNCodigo"] == "1"
    assert r["tpRBSN"] == "Receita Bruta Interna"

    r2 = server.detalhar_cclasstrib("200003")
    assert r2["tipoAliquotaCodigo"] == "5"
    assert r2["tipoAliquota"] == "Uniforme Setorial"
    assert r2["tpRBSNCodigo"] == "0"
    assert r2["tpRBSN"] == "Não Receita Bruta"


# ── Correction #5: validar_par returns PARAMETRO_INVALIDO, never a silent
#    false negative, for an unrecognized modeloDocumento ────────────────────

def test_validar_par_unknown_model_is_structured_error_not_silent_false(server):
    r = server.validar_par_cst_cclasstrib(cst="200", cClassTrib="200052", modeloDocumento="indNFe")
    assert "erro" in r
    assert r["erro"]["codigo"] == "PARAMETRO_INVALIDO"
    assert "prefixo" in r["erro"]["acao_sugerida"].lower()


def test_validar_par_correct_model_name_validates(server):
    r = server.validar_par_cst_cclasstrib(cst="200", cClassTrib="200052", modeloDocumento="NFSe",
                                          data="2026-07-03")
    assert r["valido"] is True
    assert r["prefixoCoincide"] is True
    assert r["vigente"] is True
    assert r["permitidoNoModelo"] is True
    assert r["avisos"] == []


# ── lcRedacao/lc214: explicit null, not a stray timestamp (regression for
#    the earlier mapper bug where a Publicacao date leaked into lcRedacao) ────

def test_lc_fields_are_explicit_null_not_a_timestamp(server):
    r = server.detalhar_cclasstrib("200052")
    assert r["lcRedacao"] is None
    assert r["lc214"] is None
