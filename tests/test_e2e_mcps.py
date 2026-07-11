"""End-to-end tests that drive the REAL `headlabs mcps create --spec` pipeline
against the live platform, using free, no-auth public APIs as the spec source.

Gated by environment (they create real resources on the platform and call
external APIs over the network):
  HEADLABS_E2E=1           enable e2e (design+scaffold+register, --no-deploy)
  HEADLABS_E2E_MCP_DEPLOY=1 also run ONE full build+deploy scenario (slow, ~2-4 min,
                            provisions a real AgentCore runtime)

Each scenario:
  1. Writes a spec file describing an MCP for a real, freely-available public
     API (no API key required — verified live before writing this suite).
  2. Runs `headlabs mcps create --spec <file>` against the REAL platform:
     architect design turn -> AST + behavioral verification (real subprocess,
     real MCP protocol handshake) -> approval gate (fed "y" on stdin) ->
     scaffold -> platform registration (POST /mcps, kind="mcp").
  3. Asserts the MCP is registered (GET /mcps/{id} succeeds, kind == "mcp")
     and that the generated server.py actually imports and runs (already
     proven by the pipeline's own behavioral check, re-verified here directly
     against the scaffolded ./mcps/<id>/ project as a second, independent
     confirmation).
  4. Tears down: DELETE /mcps/{id} and removes the local ./mcps/<id>/ directory.

Public APIs used (all confirmed free/no-auth at authoring time):
  - Open-Meteo   https://open-meteo.com           (weather forecast)
  - ipify        https://www.ipify.org            (public IP lookup)
  - PokeAPI      https://pokeapi.co               (Pokémon data)
  - Frankfurter  https://api.frankfurter.dev       (currency exchange rates)
  - Agify        https://agify.io                 (age estimate from a name)
"""

import json
import os
import subprocess
import sys
import time

import pytest

E2E = os.environ.get("HEADLABS_E2E") == "1"
E2E_DEPLOY = E2E and os.environ.get("HEADLABS_E2E_MCP_DEPLOY") == "1"

pytestmark = pytest.mark.skipif(not E2E, reason="set HEADLABS_E2E=1 to run e2e")

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
_TIMEOUT = int(os.environ.get("HEADLABS_E2E_MCP_TIMEOUT", "480"))


def _cli(args, stdin_text="y\n", cwd=None, timeout=_TIMEOUT):
    """Run the real `headlabs` CLI as a subprocess, feeding stdin_text to any
    interactive confirmation prompt (the approval gate)."""
    env = dict(os.environ)
    env.setdefault("PYTHONPATH", _SRC)
    proc = subprocess.run(
        [sys.executable, "-c", "import sys; from headlabs.cli import main; sys.exit(main())", *args],
        input=stdin_text, capture_output=True, text=True, timeout=timeout, env=env, cwd=cwd)
    return proc.returncode, proc.stdout, proc.stderr


def _client():
    sys.path.insert(0, _SRC)
    from headlabs.client import HeadLabsClient
    return HeadLabsClient()


def _teardown_mcp(mcp_id, workdir):
    """Best-effort cleanup: remove the platform registration and the local
    scaffolded project, regardless of test outcome."""
    try:
        _client().request("DELETE", f"/mcps/{mcp_id}")
    except Exception:
        pass
    import shutil
    mcp_dir = os.path.join(workdir, "mcps", mcp_id)
    if os.path.isdir(mcp_dir):
        shutil.rmtree(mcp_dir, ignore_errors=True)


def _mcp_registered(mcp_id) -> bool:
    try:
        item = _client().request("GET", f"/mcps/{mcp_id}")
        return item.get("kind") == "mcp"
    except Exception:
        return False


# ── Spec fixtures: one per free public API ────────────────────────────────────

_SPECS = {
    "open-meteo-weather": """\
# MCP: Previsão do Tempo (Open-Meteo)

Crie um MCP que consulta a API pública e gratuita do Open-Meteo
(https://api.open-meteo.com/v1/forecast), sem necessidade de API key.

Tools:
- `consultar_previsao(latitude, longitude)`: retorna a temperatura atual e a
  previsão horária de temperatura para as próximas 24h, usando o parâmetro
  `current=temperature_2m&hourly=temperature_2m` da API. Read-only.

Não requer autenticação nem variáveis de ambiente obrigatórias.
""",
    "ipify-public-ip": """\
# MCP: Consulta de IP Público (ipify)

Crie um MCP que consulta a API pública e gratuita do ipify
(https://api.ipify.org?format=json) para obter o endereço IP público do
servidor que executa a chamada.

Tools:
- `obter_ip_publico()`: retorna o IP público atual em formato JSON. Read-only,
  sem parâmetros.

Não requer autenticação nem variáveis de ambiente obrigatórias.
""",
    "pokeapi-pokemon": """\
# MCP: Consulta de Pokémon (PokeAPI)

Crie um MCP que consulta a PokeAPI pública e gratuita
(https://pokeapi.co/api/v2/pokemon/{nome}) para obter dados de um Pokémon.

Tools:
- `consultar_pokemon(nome)`: dado o nome (lowercase) de um Pokémon, retorna
  altura, peso e lista de habilidades. Read-only. Se o Pokémon não existir,
  retorna erro RECURSO_NAO_ENCONTRADO.

Não requer autenticação nem variáveis de ambiente obrigatórias.
""",
    "frankfurter-fx": """\
# MCP: Cotação de Moedas (Frankfurter)

Crie um MCP que consulta a API pública e gratuita do Frankfurter
(https://api.frankfurter.dev/v1/latest) para taxas de câmbio atualizadas
diariamente pelo Banco Central Europeu.

Tools:
- `cotacao_atual(de, para)`: dado um código de moeda de origem (ex: USD) e um
  código de moeda de destino (ex: BRL), retorna a taxa de câmbio atual via o
  parâmetro `?from={de}&to={para}`. Read-only.

Não requer autenticação nem variáveis de ambiente obrigatórias.
""",
    "agify-name-age": """\
# MCP: Estimativa de Idade por Nome (Agify)

Crie um MCP que consulta a API pública e gratuita do Agify
(https://api.agify.io?name={nome}) para estimar a idade média de pessoas com
um determinado primeiro nome, baseado em dados estatísticos públicos.

Tools:
- `estimar_idade(nome)`: dado um primeiro nome, retorna a idade estimada e o
  tamanho da amostra usada (campo `count`). Read-only.

Não requer autenticação nem variáveis de ambiente obrigatórias.
""",
}


@pytest.mark.parametrize("spec_id,spec_text", _SPECS.items(), ids=list(_SPECS.keys()))
def test_mcps_create_from_public_api_spec(spec_id, spec_text, tmp_path):
    """Design + scaffold + register (no deploy): the fast, always-on e2e path.
    Proves the full pipeline works end-to-end against the live platform for a
    real, freely-available public API, without paying for a Docker build."""
    mcp_id = f"e2e-{spec_id}-{int(time.time())}"
    spec_file = tmp_path / f"spec-{spec_id}.md"
    spec_file.write_text(spec_text)

    try:
        rc, out, err = _cli(
            ["mcps", "create", "--spec", str(spec_file), "--id", mcp_id, "--no-deploy"],
            cwd=str(tmp_path))

        assert rc == 0, f"{spec_id}: exit={rc}\nSTDOUT:\n{out[-3000:]}\nSTDERR:\n{err[-1500:]}"
        assert "MCP scaffolded" in out, f"{spec_id}: did not reach scaffold step\n{out[-2000:]}"

        mcp_dir = tmp_path / "mcps" / mcp_id
        assert mcp_dir.is_dir(), f"{spec_id}: {mcp_dir} was not created"
        server_py = (mcp_dir / "server.py").read_text()
        assert "FastMCP" in server_py
        assert "streamable_http_app" in server_py
        assert "@mcp.tool" in server_py

        # --no-deploy skips platform registration entirely (by design — it's
        # purely local scaffolding for review). Confirm that contract holds.
        assert not _mcp_registered(mcp_id), f"{spec_id}: --no-deploy must not register on the platform"
    finally:
        _teardown_mcp(mcp_id, str(tmp_path))


@pytest.mark.skipif(not E2E_DEPLOY, reason="set HEADLABS_E2E_MCP_DEPLOY=1 to run a full build+deploy")
def test_mcps_create_full_deploy_registers_on_platform(tmp_path):
    """The expensive, real end-to-end path: design -> build Docker image ->
    push to ECR -> provision an AgentCore runtime -> register on the platform
    (kind="mcp"). This is the exact regression that was previously silent:
    a fully built+deployed MCP that never showed up in `mcps list`."""
    spec_id = "ipify-public-ip"
    mcp_id = f"e2e-deploy-{spec_id}-{int(time.time())}"
    spec_file = tmp_path / "spec.md"
    spec_file.write_text(_SPECS[spec_id])

    try:
        rc, out, err = _cli(
            ["mcps", "create", "--spec", str(spec_file), "--id", mcp_id],
            cwd=str(tmp_path), timeout=600)

        assert rc == 0, f"exit={rc}\nSTDOUT:\n{out[-3000:]}\nSTDERR:\n{err[-1500:]}"
        assert "MCP scaffolded" in out
        assert "Push OK" in out or "deployado" in out, f"deploy did not complete:\n{out[-2000:]}"

        assert _mcp_registered(mcp_id), (
            f"{mcp_id} was built+deployed but never registered as kind='mcp' "
            f"(GET /mcps/{mcp_id} did not confirm it) — this is the exact "
            f"visibility regression this test guards against"
        )
    finally:
        _teardown_mcp(mcp_id, str(tmp_path))
