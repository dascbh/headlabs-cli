from mcp.server.fastmcp import FastMCP
import httpx
import ssl
import os
import json
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Optional, List, Dict, Any
import re

mcp = FastMCP("mcp-cclasstrib", host="0.0.0.0", stateless_http=True)

BASE_URL = os.environ.get("CFF_BASE_URL", "https://cff.svrs.rs.gov.br/api/v1")
CERT_PATH = os.environ.get("CFF_CERT_PATH", "")
CERT_PATH_B64 = os.environ.get("CFF_CERT_PATH_B64", "")
CERT_KEY_PATH = os.environ.get("CFF_CERT_KEY_PATH", "")
CERT_PASSPHRASE = os.environ.get("CFF_CERT_PASSPHRASE", "")
CACHE_TTL_HORAS = int(os.environ.get("CFF_CACHE_TTL_HORAS", "24"))
CACHE_PATH = os.environ.get("CFF_CACHE_PATH", "")
TIMEOUT_MS = int(os.environ.get("CFF_TIMEOUT_MS", "15000"))

# A .pfx (base64) can exceed the AgentCore environmentVariables per-value
# limit (4096 chars) — for real ICP-Brasil certs it always does. The runtime
# instead gets a short HEADLABS_MCP_SECRET_ID pointing at Secrets Manager
# (which allows up to 64KB) and fetches the actual cert bytes + passphrase
# itself at startup, using the AgentCore runtime role's own AWS credentials
# (it already has secretsmanager:GetSecretValue via ReadOnlyAccess).
_SECRET_ID = os.environ.get("HEADLABS_MCP_SECRET_ID", "")
if _SECRET_ID and not CERT_PATH_B64:
    try:
        import boto3 as _boto3
        _sm_client = _boto3.client("secretsmanager", region_name="us-east-1")
        _secret = json.loads(_sm_client.get_secret_value(SecretId=_SECRET_ID)["SecretString"])
        CERT_PATH_B64 = _secret.get("CFF_CERT_PATH_B64", "")
        CERT_PASSPHRASE = _secret.get("CFF_CERT_PASSPHRASE", CERT_PASSPHRASE)
    except Exception:
        pass  # _create_http_client() surfaces AUTH_CERT_INVALIDO if this stays empty

_cache_data: List[Dict[str, Any]] = []
_cache_cst_groups: List[Dict[str, Any]] = []
_cache_updated_at: Optional[datetime] = None
_cache_origin: str = "none"

# Separate cache for /consultas/anexos — a different, much larger dataset
# (NCM/NBS-level product classification, ~4k rows) than classTrib (~160
# rows). Fetched independently, on first use, not as part of the main
# atualizar_tabela cycle — most callers never need it.
_anexos_cache: List[Dict[str, Any]] = []
_anexos_updated_at: Optional[datetime] = None


def _erro(codigo: str, mensagem: str, retryable: bool, acao_sugerida: str) -> Dict[str, Any]:
    return {
        "erro": {
            "codigo": codigo,
            "mensagem": mensagem,
            "retryable": retryable,
            "acao_sugerida": acao_sugerida
        }
    }


# Public API param -> real upstream field name (irregular casing/spelling,
# confirmed against a live cff.svrs.rs.gov.br payload — e.g. "IndNFSE" not
# "IndNFSe", "IndNFSVIA" not "IndNFSeVia", "IndNFABI" not "IndNFeABI").
_MODELO_DOCUMENTO_CAMPO = {
    "NFe": "IndNFe", "NFCe": "IndNFCe", "CTe": "IndCTe", "CTeOS": "IndCTeOS",
    "BPe": "IndBPe", "NF3e": "IndNF3e", "NFCom": "IndNFCom", "NFSe": "IndNFSE",
    "BPeTM": "IndBPeTM", "BPeTA": "IndBPeTA", "NFAg": "IndNFAg",
    "NFSeVia": "IndNFSVIA", "NFeABI": "IndNFABI", "NFGas": "IndNFGas",
    "DERE": "IndDERE", "DIR": "IndDIR", "DUIMP": "IndDUIMP",
}


def _load_cache_from_disk() -> bool:
    global _cache_data, _cache_cst_groups, _cache_updated_at, _cache_origin
    if not CACHE_PATH:
        return False
    try:
        path = Path(CACHE_PATH)
        if not path.exists():
            return False
        with open(path, "r", encoding="utf-8") as f:
            snapshot = json.load(f)
        _cache_data = snapshot.get("data", [])
        _cache_cst_groups = snapshot.get("cst_groups", [])
        timestamp_str = snapshot.get("updated_at")
        if timestamp_str:
            _cache_updated_at = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
        _cache_origin = "disco"
        return True
    except Exception:
        return False


def _save_cache_to_disk():
    if not CACHE_PATH:
        return
    try:
        path = Path(CACHE_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        snapshot = {
            "data": _cache_data,
            "cst_groups": _cache_cst_groups,
            "updated_at": _cache_updated_at.isoformat() if _cache_updated_at else None
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False)
    except Exception:
        pass


def _create_http_client() -> httpx.Client:
    # Two supported credential sources, in order of preference:
    #   1. CFF_CERT_PATH_B64 — base64-encoded .pfx/.p12 content (env-var-only,
    #      no persistent filesystem across AgentCore deploys — this is the
    #      real mechanism in production; see POST /mcps/{id}/secrets).
    #   2. CFF_CERT_PATH — a filesystem path to a PEM (kept for local/dev use
    #      where a real path exists).
    if CERT_PATH_B64:
        try:
            import base64
            import tempfile
            from cryptography.hazmat.primitives.serialization import (
                pkcs12, Encoding, PrivateFormat, NoEncryption,
            )
            pfx_data = base64.b64decode(CERT_PATH_B64)
            passphrase = CERT_PASSPHRASE.encode() if CERT_PASSPHRASE else None
            private_key, certificate, _ = pkcs12.load_key_and_certificates(pfx_data, passphrase)
            cert_pem = certificate.public_bytes(Encoding.PEM)
            key_pem = private_key.private_bytes(Encoding.PEM, PrivateFormat.TraditionalOpenSSL, NoEncryption())

            cert_fd, cert_tmp = tempfile.mkstemp(suffix=".pem")
            key_fd, key_tmp = tempfile.mkstemp(suffix=".pem")
            with os.fdopen(cert_fd, "wb") as f:
                f.write(cert_pem)
            with os.fdopen(key_fd, "wb") as f:
                f.write(key_pem)

            ssl_context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
            ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2
            ssl_context.maximum_version = ssl.TLSVersion.TLSv1_3
            ssl_context.load_cert_chain(certfile=cert_tmp, keyfile=key_tmp)
            return httpx.Client(verify=ssl_context, timeout=TIMEOUT_MS / 1000.0, follow_redirects=True)
        except Exception as e:
            raise Exception(_erro("AUTH_CERT_INVALIDO", f"Falha ao carregar certificado (base64): {str(e)}",
                                 False, "Verifique CFF_CERT_PATH_B64 e CFF_CERT_PASSPHRASE"))

    if not CERT_PATH:
        raise Exception(_erro("AUTH_CERT_INVALIDO", "Certificado não configurado", False,
                     "Configure CFF_CERT_PATH_B64 (produção) ou CFF_CERT_PATH (caminho local de um PEM)"))
    
    try:
        cert_path = Path(CERT_PATH)
        if not cert_path.exists():
            raise FileNotFoundError("Certificado não encontrado")
        
        ssl_context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
        ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2
        ssl_context.maximum_version = ssl.TLSVersion.TLSv1_3
        
        if CERT_KEY_PATH:
            key_path = Path(CERT_KEY_PATH)
            if not key_path.exists():
                raise FileNotFoundError("Chave privada não encontrada")
            ssl_context.load_cert_chain(
                certfile=str(cert_path),
                keyfile=str(key_path),
                password=CERT_PASSPHRASE if CERT_PASSPHRASE else None
            )
        else:
            ssl_context.load_cert_chain(
                certfile=str(cert_path),
                password=CERT_PASSPHRASE if CERT_PASSPHRASE else None
            )
        
        return httpx.Client(
            verify=ssl_context,
            timeout=TIMEOUT_MS / 1000.0,
            follow_redirects=True
        )
    except FileNotFoundError as e:
        raise Exception(_erro("AUTH_CERT_INVALIDO", str(e), False,
                             "Verifique os caminhos CFF_CERT_PATH e CFF_CERT_KEY_PATH"))
    except ssl.SSLError as e:
        raise Exception(_erro("TLS_INCOMPATIVEL", f"Erro TLS: {str(e)}", False,
                             "Verifique se o certificado é válido e suporta TLS 1.2+"))
    except Exception as e:
        raise Exception(_erro("AUTH_CERT_INVALIDO", f"Erro ao carregar certificado: {str(e)}", False,
                             "Verifique a configuração do certificado e senha"))


def _fetch_upstream() -> Dict[str, Any]:
    global _cache_data, _cache_cst_groups, _cache_updated_at, _cache_origin
    
    try:
        client = _create_http_client()
    except Exception as e:
        if isinstance(e.args[0], dict) and "erro" in e.args[0]:
            return e.args[0]
        return _erro("AUTH_CERT_INVALIDO", str(e), False,
                    "Verifique a configuração do certificado")
    
    try:
        response = client.get(f"{BASE_URL}/consultas/classTrib")
        
        if response.status_code >= 500:
            return _erro("UPSTREAM_INDISPONIVEL", f"Erro {response.status_code} da API",
                        True, "Aguarde alguns minutos e tente novamente")
        
        if response.status_code == 401 or response.status_code == 403:
            return _erro("AUTH_CERT_INVALIDO", "Certificado rejeitado pela API",
                        False, "Verifique se o certificado é válido e está dentro da validade")
        
        if response.status_code != 200:
            return _erro("UPSTREAM_INDISPONIVEL", f"Status {response.status_code}",
                        True, "Verifique a conectividade com a API")
        
        data = response.json()
        if not isinstance(data, list):
            return _erro("UPSTREAM_INDISPONIVEL", "Formato de resposta inválido",
                        True, "Contate o suporte da API")

        # The real API returns a HIERARCHICAL shape: a list of CST groups,
        # each carrying its own nested "classificacoesTributarias" list AND
        # its own CST-level indicators (IndIBSCBS, IndRedBC, IndRedAliq,
        # IndTransfCred, IndDif, IndAjusteCompet, IndIBSCBSMono,
        # IndCredPresIBSZFM) — confirmed against the real payload. These are
        # NOT duplicated per classification; e.g. CST 810 (ZFM adjustment)
        # has IndCredPresIBSZFM=true at the CST level while every one of its
        # classifications has IndCredPresOper=false (a DIFFERENT, unrelated
        # indicator) — reading only the classification-level field produces
        # a false negative for "does this generate a presumed credit?".
        # Flatten to one record per cClassTrib, carrying both levels.
        flat: List[Dict[str, Any]] = []
        for cst_group in data:
            if not isinstance(cst_group, dict):
                continue
            classificacoes = cst_group.get("classificacoesTributarias") or []
            for classe in classificacoes:
                if not isinstance(classe, dict):
                    continue
                merged = dict(classe)
                merged["_cst"] = cst_group.get("CST", "")
                merged["_descricaoCst"] = cst_group.get("DescricaoCST", "")
                merged["_cstIndIBSCBS"] = cst_group.get("IndIBSCBS")
                merged["_cstIndRedBC"] = cst_group.get("IndRedBC")
                merged["_cstIndRedAliq"] = cst_group.get("IndRedAliq")
                merged["_cstIndTransfCred"] = cst_group.get("IndTransfCred")
                merged["_cstIndDif"] = cst_group.get("IndDif")
                merged["_cstIndAjusteCompet"] = cst_group.get("IndAjusteCompet")
                merged["_cstIndIBSCBSMono"] = cst_group.get("IndIBSCBSMono")
                merged["_cstIndCredPresIBSZFM"] = cst_group.get("IndCredPresIBSZFM")
                flat.append(merged)

        # Preserve the CST groups themselves too (not just flattened into
        # classifications) — a CST with zero classifications today (e.g. 210
        # in some snapshots) would otherwise disappear entirely from
        # listar_cst, silently hiding a valid CST that simply has no active
        # cClassTrib right now.
        cst_groups = [
            {k: v for k, v in cst_group.items() if k != "classificacoesTributarias"}
            for cst_group in data if isinstance(cst_group, dict)
        ]

        _cache_data = flat
        _cache_cst_groups = cst_groups
        _cache_updated_at = datetime.utcnow()
        _cache_origin = "rede"
        _save_cache_to_disk()
        
        return {"success": True}
        
    except httpx.TimeoutException:
        return _erro("UPSTREAM_INDISPONIVEL", "Timeout ao conectar com a API",
                    True, "Verifique a conectividade de rede e tente novamente")
    except httpx.ConnectError:
        return _erro("UPSTREAM_INDISPONIVEL", "Falha de conexão com a API",
                    True, "Verifique a conectividade de rede")
    except Exception as e:
        return _erro("UPSTREAM_INDISPONIVEL", f"Erro inesperado: {str(e)}",
                    True, "Tente novamente mais tarde")
    finally:
        try:
            client.close()
        except:
            pass


def _fetch_anexos() -> Dict[str, Any]:
    """Fetch /consultas/anexos — the LC 214/2025 Anexo catalog (product-level
    NCM/NBS classification into the numbered Anexos I-XVII plus the
    "composite" Anexo numbers used for service-based exemptions, e.g.
    nroAnexo=91271 -> "Prestação de serviços de profissões intelectuais",
    the same Anexo referenced by cClassTrib 200052). ~4k rows — fetched
    lazily on first use of consultar_anexo, not part of the main
    atualizar_tabela cycle (most callers never need it)."""
    global _anexos_cache, _anexos_updated_at

    try:
        client = _create_http_client()
    except Exception as e:
        if isinstance(e.args[0], dict) and "erro" in e.args[0]:
            return e.args[0]
        return _erro("AUTH_CERT_INVALIDO", str(e), False,
                    "Verifique a configuração do certificado")

    try:
        response = client.get(f"{BASE_URL}/consultas/anexos")

        if response.status_code >= 500:
            return _erro("UPSTREAM_INDISPONIVEL", f"Erro {response.status_code} da API",
                        True, "Aguarde alguns minutos e tente novamente")
        if response.status_code in (401, 403):
            return _erro("AUTH_CERT_INVALIDO", "Certificado rejeitado pela API",
                        False, "Verifique se o certificado é válido e está dentro da validade")
        if response.status_code != 200:
            return _erro("UPSTREAM_INDISPONIVEL", f"Status {response.status_code}",
                        True, "Verifique a conectividade com a API")

        data = response.json()
        if not isinstance(data, list):
            return _erro("UPSTREAM_INDISPONIVEL", "Formato de resposta inválido",
                        True, "Contate o suporte da API")

        _anexos_cache = data
        _anexos_updated_at = datetime.utcnow()
        return {"success": True}

    except httpx.TimeoutException:
        return _erro("UPSTREAM_INDISPONIVEL", "Timeout ao conectar com a API",
                    True, "Verifique a conectividade de rede e tente novamente")
    except httpx.ConnectError:
        return _erro("UPSTREAM_INDISPONIVEL", "Falha de conexão com a API",
                    True, "Verifique a conectividade de rede")
    except Exception as e:
        return _erro("UPSTREAM_INDISPONIVEL", f"Erro inesperado: {str(e)}",
                    True, "Tente novamente mais tarde")
    finally:
        try:
            client.close()
        except:
            pass


def _ensure_anexos_cache() -> Optional[Dict[str, Any]]:
    if _anexos_cache and _anexos_updated_at:
        idade = (datetime.utcnow() - _anexos_updated_at).total_seconds() / 3600
        if idade < CACHE_TTL_HORAS:
            return None
    result = _fetch_anexos()
    if "erro" in result:
        if _anexos_cache:
            return None
        return result
    return None


def _ensure_cache() -> Optional[Dict[str, Any]]:
    global _cache_data, _cache_cst_groups, _cache_updated_at, _cache_origin
    
    if _cache_data and _cache_updated_at:
        idade = (datetime.utcnow() - _cache_updated_at).total_seconds() / 3600
        if idade < CACHE_TTL_HORAS:
            return None
    
    if not _cache_data:
        loaded = _load_cache_from_disk()
        if loaded and _cache_updated_at:
            idade = (datetime.utcnow() - _cache_updated_at).total_seconds() / 3600
            if idade < CACHE_TTL_HORAS:
                return None
    
    result = _fetch_upstream()
    if "erro" in result:
        if _cache_data:
            return None
        return result
    
    return None


def _split_codigo_texto(valor: Optional[str]) -> tuple:
    """The upstream API prefixes several enum-like fields with a numeric code
    ("2 - Padrão", "1 - Receita Bruta Interna") — confirmed real, not a
    mapping artifact. Split into (codigo, texto) so callers don't have to
    parse the string themselves."""
    if not valor:
        return None, None
    codigo, sep, texto = valor.partition(" - ")
    if sep:
        return codigo.strip(), texto.strip()
    return None, valor


def _map_classificacao(item: Dict[str, Any]) -> Dict[str, Any]:
    # Field names below match the REAL cff.svrs.rs.gov.br payload exactly
    # (confirmed against a live mTLS call) — the API's actual shape differs
    # from what an earlier draft of this file assumed (e.g. "IndNFSE", not
    # "indNFSe"; "Anexo"/"Link" at top level of the classification, not the
    # CST group; percentuais are floats already, no parsing needed).
    tipo_aliquota_codigo, tipo_aliquota_texto = _split_codigo_texto(item.get("TipoAliquota"))
    tp_rbsn_codigo, tp_rbsn_texto = _split_codigo_texto(item.get("TipoReceitaBrutaSN"))
    return {
        "cClassTrib": item.get("cClassTrib", ""),
        "cst": item.get("_cst", ""),
        "descricaoCst": item.get("_descricaoCst", ""),
        "nome": item.get("DescricaoClassTrib", ""),
        "descricao": item.get("DescricaoClassTrib", ""),
        # lcRedacao/lc214: the upstream API does NOT expose the integral
        # legal text or a short article reference as separate fields —
        # confirmed by inspecting the raw response (only `descricao` and
        # `link` carry any legal reference, and only as a passing mention
        # inside the free-text description). Left explicitly null rather
        # than guessing a value from an unrelated field (a prior version of
        # this mapper put a timestamp here, which is worse than null).
        "lcRedacao": None,
        "lc214": None,
        "regulamentoCbs": "",
        "regulamentoIbs": "",
        "tipoAliquotaCodigo": tipo_aliquota_codigo,
        "tipoAliquota": tipo_aliquota_texto,
        "pRedIBS": item.get("pRedIBS"),
        "pRedCBS": item.get("pRedCBS"),
        "indicadoresGrupo": {
            "ind_gTribRegular": item.get("IndTribRegular"),
            "ind_gCredPresOper": item.get("IndCredPresOper"),
            "ind_gMonoPadrao": item.get("MonofasiaPadrao"),
            "ind_gMonoReten": item.get("MonofasiaSujeitaRetencao"),
            "ind_gMonoRet": item.get("MonofasiaRetidaAnt"),
            "ind_gMonoDif": item.get("MonofasiaDiferimento"),
            "ind_gEstornoCred": item.get("IndEstornoCred")
        },
        # CST-level indicators (NOT per-classification — see the note in
        # _fetch_upstream). Exposed here too so a caller reading one
        # cClassTrib record doesn't have to make a second call to listar_cst
        # to know e.g. whether it sits under a ZFM-presumed-credit CST.
        "indicadoresCst": {
            "ind_gIBSCBS": item.get("_cstIndIBSCBS"),
            "ind_gRedBC": item.get("_cstIndRedBC"),
            "ind_gRedAliq": item.get("_cstIndRedAliq"),
            "ind_gTransfCred": item.get("_cstIndTransfCred"),
            "ind_gDif": item.get("_cstIndDif"),
            "ind_gAjusteCompet": item.get("_cstIndAjusteCompet"),
            "ind_gIBSCBSMono": item.get("_cstIndIBSCBSMono"),
            "ind_gCredPresIBSZFM": item.get("_cstIndCredPresIBSZFM")
        },
        "tpRBSNCodigo": tp_rbsn_codigo,
        "tpRBSN": tp_rbsn_texto,
        "dIniVig": item.get("InicioVigencia"),
        "dFimVig": item.get("FimVigencia"),
        "dataAtualizacao": item.get("Publicacao"),
        "modelosPermitidos": {
            "indNFeABI": item.get("IndNFABI"),
            "indNFe": item.get("IndNFe"),
            "indNFCe": item.get("IndNFCe"),
            "indCTe": item.get("IndCTe"),
            "indCTeOS": item.get("IndCTeOS"),
            "indBPe": item.get("IndBPe"),
            "indBPeTA": item.get("IndBPeTA"),
            "indBPeTM": item.get("IndBPeTM"),
            "indNF3e": item.get("IndNF3e"),
            "indNFSe": item.get("IndNFSE"),
            "indNFSeVia": item.get("IndNFSVIA"),
            "indNFCom": item.get("IndNFCom"),
            "indNFAg": item.get("IndNFAg"),
            "indNFGas": item.get("IndNFGas"),
            "indDERE": item.get("IndDERE"),
            "indDIR": item.get("IndDIR"),
            "indDUIMP": item.get("IndDUIMP")
        },
        # anexo: NOT a mapping bug — confirmed two legitimate formats coexist
        # in the real API. A plain small int (1-17ish) is the Anexo number
        # (I-XVII) of LC 214/2025. A larger composite value like 90111/91271
        # follows the pattern 9+<artigo>+1 (e.g. 90111 -> art. 11, 91271 ->
        # art. 127) and appears when the classification's legal basis is an
        # article without a formal numbered Anexo. Both are passed through
        # as-is (int|null) — do not attempt to normalize one into the other.
        "anexo": item.get("Anexo"),
        "link": item.get("Link")
    }


def _is_vigente(item: Dict[str, Any], data_ref: date) -> bool:
    # Real API fields are ISO datetimes ("2025-05-05T00:00:00"), not
    # "%d/%m/%Y" — confirmed against a live response.
    dIniVig_str = item.get("InicioVigencia")
    dFimVig_str = item.get("FimVigencia")

    if dIniVig_str:
        try:
            dIniVig = datetime.fromisoformat(dIniVig_str).date()
            if data_ref < dIniVig:
                return False
        except Exception:
            pass

    if dFimVig_str:
        try:
            dFimVig = datetime.fromisoformat(dFimVig_str).date()
            if data_ref > dFimVig:
                return False
        except Exception:
            pass

    return True


def _filter_cache(cst: Optional[str], nomeCst: Optional[str], busca: Optional[str],
                  apenasVigentes: bool, limite: int, data_ref: date) -> List[Dict[str, Any]]:
    resultado = []
    
    for item in _cache_data:
        if cst and item.get("_cst", "") != cst:
            continue
        
        if nomeCst:
            desc = item.get("_descricaoCst", "").lower()
            if nomeCst.lower() not in desc:
                continue
        
        if busca:
            desc_class = item.get("DescricaoClassTrib", "").lower()
            busca_lower = busca.lower()
            if busca_lower not in desc_class:
                continue
        
        if apenasVigentes and not _is_vigente(item, data_ref):
            continue
        
        resultado.append(_map_classificacao(item))
        if len(resultado) >= limite:
            break
    
    return sorted(resultado, key=lambda x: x["cClassTrib"])


@mcp.tool()
def consultar_classificacao_tributaria(
    cst: Optional[str] = None,
    nomeCst: Optional[str] = None,
    busca: Optional[str] = None,
    apenasVigentes: bool = True,
    limite: int = 50
) -> Dict[str, Any]:
    """Consulta a tabela CST/cClassTrib com filtros opcionais (CST, nome, busca textual). Retorna lista de classificações tributárias com dados completos de vigência, alíquotas e modelos de documento. Read-only, sem side effects, serve do cache local (TTL 24h)."""
    
    if limite < 1 or limite > 500:
        return _erro("PARAMETRO_INVALIDO", "Limite deve estar entre 1 e 500", False,
                    "Ajuste o parâmetro limite para um valor válido")
    
    if cst and not re.match(r"^\d{3}$", cst):
        return _erro("PARAMETRO_INVALIDO", "CST deve ter 3 dígitos", False,
                    "Informe o CST no formato XXX (3 dígitos)")
    
    erro_cache = _ensure_cache()
    if erro_cache:
        return erro_cache
    
    data_ref = date.today()
    itens = _filter_cache(cst, nomeCst, busca, apenasVigentes, limite, data_ref)
    
    return {
        "total": len(itens),
        "itens": itens,
        "fonteCache": True,
        "atualizadoEm": _cache_updated_at.isoformat() + "Z" if _cache_updated_at else None
    }


@mcp.tool()
def detalhar_cclasstrib(cClassTrib: str) -> Dict[str, Any]:
    """Retorna o registro completo de um cClassTrib específico, incluindo todos os indicadores de grupo, percentuais de redução, vigência e vínculo legal. Read-only, sem side effects. Retorna erro RECURSO_NAO_ENCONTRADO se código inexistente."""
    
    if not re.match(r"^\d{6}$", cClassTrib):
        return _erro("PARAMETRO_INVALIDO", "cClassTrib deve ter 6 dígitos", False,
                    "Informe o cClassTrib no formato XXXXXX (6 dígitos)")
    
    erro_cache = _ensure_cache()
    if erro_cache:
        return erro_cache
    
    for item in _cache_data:
        if item.get("cClassTrib") == cClassTrib:
            return _map_classificacao(item)
    
    return _erro("RECURSO_NAO_ENCONTRADO", f"cClassTrib {cClassTrib} não encontrado", False,
                "Verifique o código informado ou consulte a lista completa de classificações")


@mcp.tool()
def listar_cst(comContagem: bool = True) -> Dict[str, Any]:
    """Lista os grupos de CST-IBS/CBS com descrição, indicadores de grupo (crédito presumido ZFM, redutor de BC, redução de alíquota, diferimento, monofasia, ajuste de competência, transferência de crédito) e opcionalmente a quantidade de cClassTrib vigentes por CST. Inclui CSTs mesmo sem classificação vigente no momento. Útil para navegação hierárquica e para identificar CSTs com regimes especiais (ex.: CST 810 tem ind_gCredPresIBSZFM=true) antes de consultar classificações específicas. Read-only, sem side effects."""
    
    erro_cache = _ensure_cache()
    if erro_cache:
        return erro_cache

    # Count vigente classifications per CST from the flattened cache.
    contagens: Dict[str, int] = {}
    if comContagem:
        for item in _cache_data:
            cst = item.get("_cst", "")
            contagens[cst] = contagens.get(cst, 0) + 1

    itens = []
    for grupo in _cache_cst_groups:
        cst = grupo.get("CST", "")
        entry = {
            "cst": cst,
            "descricao": grupo.get("DescricaoCST", ""),
            "indicadoresGrupo": {
                "ind_gIBSCBS": grupo.get("IndIBSCBS"),
                "ind_gRedBC": grupo.get("IndRedBC"),
                "ind_gRedAliq": grupo.get("IndRedAliq"),
                "ind_gTransfCred": grupo.get("IndTransfCred"),
                "ind_gDif": grupo.get("IndDif"),
                "ind_gAjusteCompet": grupo.get("IndAjusteCompet"),
                "ind_gIBSCBSMono": grupo.get("IndIBSCBSMono"),
                "ind_gCredPresIBSZFM": grupo.get("IndCredPresIBSZFM"),
            },
        }
        if comContagem:
            entry["qtdeClassificacoes"] = contagens.get(cst, 0)
        itens.append(entry)

    itens.sort(key=lambda x: x["cst"])
    return {"itens": itens}


@mcp.tool()
def validar_par_cst_cclasstrib(
    cst: str,
    cClassTrib: str,
    modeloDocumento: Optional[str] = None,
    data: Optional[str] = None
) -> Dict[str, Any]:
    """Valida se um par CST + cClassTrib é coerente (prefixo coincide), se está vigente na data especificada e se é permitido no modelo de documento. Retorna dict com flags booleanas e avisos. Read-only, sem side effects."""
    
    if not re.match(r"^\d{3}$", cst):
        return _erro("PARAMETRO_INVALIDO", "CST deve ter 3 dígitos", False,
                    "Informe o CST no formato XXX (3 dígitos)")
    
    if not re.match(r"^\d{6}$", cClassTrib):
        return _erro("PARAMETRO_INVALIDO", "cClassTrib deve ter 6 dígitos", False,
                    "Informe o cClassTrib no formato XXXXXX (6 dígitos)")
    
    erro_cache = _ensure_cache()
    if erro_cache:
        return erro_cache
    
    prefixo_coincide = cClassTrib[:3] == cst
    
    item_encontrado = None
    for item in _cache_data:
        if item.get("cClassTrib") == cClassTrib:
            item_encontrado = item
            break
    
    if not item_encontrado:
        return _erro("RECURSO_NAO_ENCONTRADO", f"cClassTrib {cClassTrib} não encontrado", False,
                    "Verifique o código informado")
    
    if data:
        try:
            data_ref = datetime.strptime(data, "%Y-%m-%d").date()
        except:
            return _erro("PARAMETRO_INVALIDO", "Data deve estar no formato YYYY-MM-DD", False,
                        "Informe a data no formato correto")
    else:
        data_ref = date.today()

    # An unrecognized modeloDocumento is a CALLER ERROR, not a fact about the
    # cClassTrib — it must be a hard PARAMETRO_INVALIDO, never silently
    # folded into "valido": false. Doing the latter is indistinguishable
    # from a genuinely invalid pair and produces a false negative (confirmed
    # real-world case: passing "indNFe" instead of "NFe" here previously
    # made a valid pair look invalid, with no signal pointing at the typo).
    if modeloDocumento and modeloDocumento not in _MODELO_DOCUMENTO_CAMPO:
        return _erro("PARAMETRO_INVALIDO", f"Modelo de documento '{modeloDocumento}' não reconhecido", False,
                    f"Use um dos modelos válidos (sem prefixo 'ind'): {', '.join(_MODELO_DOCUMENTO_CAMPO.keys())}")

    vigente = _is_vigente(item_encontrado, data_ref)
    
    permitido_no_modelo = True
    avisos = []
    
    if modeloDocumento:
        campo_ind = _MODELO_DOCUMENTO_CAMPO[modeloDocumento]
        valor_ind = item_encontrado.get(campo_ind)
        if not valor_ind:
            avisos.append(f"Classificação não permitida para {modeloDocumento}")
            permitido_no_modelo = False
    
    if not prefixo_coincide:
        avisos.append("Prefixo do cClassTrib não coincide com o CST informado")
    
    if not vigente:
        avisos.append("Classificação fora do período de vigência na data especificada")
    
    valido = prefixo_coincide and vigente and permitido_no_modelo
    
    return {
        "valido": valido,
        "prefixoCoincide": prefixo_coincide,
        "vigente": vigente,
        "permitidoNoModelo": permitido_no_modelo,
        "avisos": avisos,
        "detalhe": _map_classificacao(item_encontrado)
    }


@mcp.tool()
def consultar_por_modelo_documento(
    modeloDocumento: str,
    cst: Optional[str] = None,
    apenasVigentes: bool = True
) -> Dict[str, Any]:
    """Retorna os cClassTrib permitidos para um modelo específico de DF-e (indicador indNFe, indNFCe, etc. = true), com filtro opcional por CST e vigência. Read-only, sem side effects."""
    
    if cst and not re.match(r"^\d{3}$", cst):
        return _erro("PARAMETRO_INVALIDO", "CST deve ter 3 dígitos", False,
                    "Informe o CST no formato XXX (3 dígitos)")
    
    erro_cache = _ensure_cache()
    if erro_cache:
        return erro_cache
    
    campo_ind = _MODELO_DOCUMENTO_CAMPO.get(modeloDocumento)
    if campo_ind is None:
        return _erro("PARAMETRO_INVALIDO", f"Modelo de documento '{modeloDocumento}' não reconhecido", False,
                    f"Use um dos modelos válidos: {', '.join(_MODELO_DOCUMENTO_CAMPO.keys())}")
    data_ref = date.today()
    resultado = []
    
    for item in _cache_data:
        valor_ind = item.get(campo_ind)
        if valor_ind is None or not valor_ind:
            continue
        
        if cst and item.get("_cst", "") != cst:
            continue
        
        if apenasVigentes and not _is_vigente(item, data_ref):
            continue
        
        resultado.append(_map_classificacao(item))
    
    return {
        "total": len(resultado),
        "itens": sorted(resultado, key=lambda x: x["cClassTrib"])
    }


@mcp.tool()
def consultar_anexo(
    nroAnexo: Optional[int] = None,
    codNcmNbs: Optional[str] = None,
    busca: Optional[str] = None,
    apenasVigentes: bool = True,
    limite: int = 50
) -> Dict[str, Any]:
    """Consulta o catálogo de Anexos da LC 214/2025 (endpoint /consultas/anexos da SVRS): título oficial do Anexo (descrAnexo), descrição do item/NCM-NBS específico (descrItemAnexo), condição, exceção e NCM/NBS vinculado. Cobre tanto os Anexos numerados (I-XVII, ex. nroAnexo=1) quanto os Anexos "compostos" referenciados por serviço (ex. nroAnexo=91271 = "Prestação de serviços de profissões intelectuais", o mesmo Anexo do cClassTrib 200052). É a fonte de texto legal citável que falta no endpoint classTrib (lcRedacao/lc214 lá são sempre null). Não cobre TODOS os valores de "anexo" vistos em classTrib — alguns (ex. 2, 3, 91721-91726) não têm correspondência neste catálogo; nesses casos, retorne "não encontrado" em vez de inventar. apenasVigentes (default true) filtra itens com dthFimVig no passado. Cache próprio, TTL igual ao principal (24h). Read-only, sem side effects. Requer ao menos um filtro (nroAnexo, codNcmNbs ou busca) — não retorna o catálogo completo (~4000 itens) de uma vez."""

    if nroAnexo is None and not codNcmNbs and not busca:
        return _erro("PARAMETRO_INVALIDO",
                    "Informe ao menos um filtro: nroAnexo, codNcmNbs ou busca",
                    False,
                    "O catálogo tem ~4000 itens; consultas sem filtro não são permitidas")

    if limite < 1 or limite > 200:
        return _erro("PARAMETRO_INVALIDO", "limite deve estar entre 1 e 200", False,
                    "Ajuste o parâmetro limite")

    erro_cache = _ensure_anexos_cache()
    if erro_cache:
        return erro_cache

    data_ref = date.today()
    resultado = []
    busca_lower = busca.lower() if busca else None

    for item in _anexos_cache:
        if nroAnexo is not None and item.get("nroAnexo") != nroAnexo:
            continue
        if codNcmNbs and item.get("codNcmNbs") != codNcmNbs:
            continue
        if busca_lower:
            campos_texto = " ".join(str(item.get(c) or "") for c in
                                     ("descrAnexo", "descrItemAnexo", "descrCondicao", "descrExcecao")).lower()
            if busca_lower not in campos_texto:
                continue

        dth_fim = item.get("dthFimVig")
        if apenasVigentes and dth_fim:
            try:
                fim = datetime.fromisoformat(dth_fim).date()
                if fim < data_ref:
                    continue
            except (ValueError, TypeError):
                pass

        resultado.append({
            "nroAnexo": item.get("nroAnexo"),
            "descrAnexo": item.get("descrAnexo"),
            "nroItemAnexoLei": item.get("nroItemAnexoLei"),
            "descrItemAnexo": item.get("descrItemAnexo"),
            "codNcmNbs": item.get("codNcmNbs"),
            "tipoNomenclatura": item.get("TipoNomenclatura"),
            "tipoPermissao": item.get("TipoPermissao"),
            "descrCondicao": item.get("descrCondicao"),
            "descrExcecao": item.get("descrExcecao"),
            "texObservacao": item.get("texObservacao"),
            "iniVigencia": item.get("dthIniVig"),
            "fimVigencia": item.get("dthFimVig"),
        })

    truncado = len(resultado) > limite
    return {
        "total": len(resultado),
        "truncado": truncado,
        "itens": resultado[:limite]
    }


@mcp.tool()
def status_cache() -> Dict[str, Any]:
    """Retorna diagnóstico do cache: total de registros, timestamp da última atualização, idade em horas, próxima atualização elegível e origem (rede/disco). Inclui também o cache secundário de anexos (consultar_anexo), se já foi carregado alguma vez nesta instância. Read-only, sem side effects."""
    
    erro_cache = _ensure_cache()
    if erro_cache:
        return erro_cache
    
    idade_horas = 0.0
    if _cache_updated_at:
        idade_horas = (datetime.utcnow() - _cache_updated_at).total_seconds() / 3600
    
    proxima_atualizacao = None
    if _cache_updated_at:
        proxima_atualizacao = (_cache_updated_at + timedelta(hours=CACHE_TTL_HORAS)).isoformat() + "Z"

    anexos_status = {"carregado": False}
    if _anexos_updated_at:
        idade_anexos = (datetime.utcnow() - _anexos_updated_at).total_seconds() / 3600
        anexos_status = {
            "carregado": True,
            "totalRegistros": len(_anexos_cache),
            "atualizadoEm": _anexos_updated_at.isoformat() + "Z",
            "idadeHoras": round(idade_anexos, 2)
        }
    
    return {
        "totalRegistros": len(_cache_data),
        "atualizadoEm": _cache_updated_at.isoformat() + "Z" if _cache_updated_at else None,
        "idadeHoras": round(idade_horas, 2),
        "proximaAtualizacaoElegivel": proxima_atualizacao,
        "origem": _cache_origin,
        "cacheAnexos": anexos_status
    }


@mcp.tool()
def atualizar_tabela(forcar: bool = False) -> Dict[str, Any]:
    """Força um refresh do snapshot da tabela cClassTrib da API, respeitando o guard de frequência (TTL). Recusa atualização se idade < TTL, salvo forcar=true. Side effect: busca upstream e atualiza cache. Retorna status do cache após tentativa."""
    
    global _cache_data, _cache_cst_groups, _cache_updated_at, _cache_origin
    
    if _cache_updated_at and not forcar:
        idade_horas = (datetime.utcnow() - _cache_updated_at).total_seconds() / 3600
        if idade_horas < CACHE_TTL_HORAS:
            return _erro("RATE_LIMIT_LOCAL",
                        f"Cache ainda válido (idade: {idade_horas:.1f}h, TTL: {CACHE_TTL_HORAS}h)",
                        False,
                        "Aguarde até a próxima janela de atualização ou use forcar=true")
    
    result = _fetch_upstream()
    if "erro" in result:
        return result
    
    return status_cache()


app = mcp.streamable_http_app()

if __name__ == "__main__":
    import os
    mcp.run(transport=os.environ.get("MCP_TRANSPORT", "streamable-http"))
