# Proposta: `headlabs local inspect` — inspector white-box para projetos locais

> **Status: implementado (completo).** Escopo entregue: inspeção estática +
> `--fix` + front-end (`--url`), superfície **`headlabs local inspect`**, backend
> **híbrido**: self-hosted (default) e **`--provider platform`** (agente Claude
> declarativo, via invoke+poll — ver §8) + ponte de skills (`--skill`). Uso
> resumido em `docs/local-runtime.md` §5; este doc mantém o racional de design.
>
> Código: `src/headlabs/local/inspector.py`, `local/backlog.py`,
> `local/tools/report_finding.py`, handlers em `local_cli.py`, subparsers em
> `cli.py`. Testes: `tests/test_local_{inspector,backlog,report_finding}.py`.
>
> **Comparação real medida** (repo `Vulnerable-Flask-App`, `--role security`):
> self-hosted `granite4.1:8b` achou 1–2 issues; `--provider platform`
> (Claude Sonnet 4.5) achou 23–27 (SQLi, SSTI, YAML RCE, JWT bypass, XXE, MD5,
> secrets hardcoded, Python 2.7 EOL) com números de linha exatos.

## 1. Contexto / problema

O inspector atual (`headlabs labs inspect`, em `src/headlabs/labsctl.py:794`) é
**100% server-side e black-box**: opera sobre um `lab_id` → `loop_id` → recursos
**já implantados na nuvem** (`site_urls` em `apps.headlabs.ai`, `function_endpoints`
em `api.headlabs.ai`), invocando o agente `loop-inspector` no servidor
(`POST /agents/loop-inspector/invoke`, `labsctl.py:935`). **Nada do disco do
usuário é lido** — o inspector bate nos deploys por HTTP. Logo, ele é
estruturalmente incapaz de inspecionar um diretório local (não há lab, loop, nem
recurso implantado).

Objetivo: um caminho **novo, client-side e white-box** que inspeciona um projeto
qualquer em disco (front/back/etc.), reaproveitando ao máximo o runtime
`headlabs local` (que já roda um loop agêntico contra o `cwd`).

**Conceitualmente complementar, não substituto:** o inspector da plataforma é
*dynamic/black-box* (deploy vivo); este é *static + runtime white-box* (código em
disco + opcionalmente o front local rodando).

## 2. Arquitetura: reuso do runtime `local`

O `QueryEngine` (`src/headlabs/local/engine.py:173`) já:
- roda um loop de tool-calls contra `cwd` (`engine.py:187,246`);
- aceita `system_prompt=` como kwarg (`engine.py:182`) — injeção de prompt sem
  tocar no engine;
- anexa `.headlabs/rules.md` ao prompt se existir (`engine.py:191-204`);
- emite `EngineEvent` via `on_event` e retorna o texto final (`engine.py:255`).

Mapa de reuso (nada disso é código novo):

| Necessidade | Reuso | Local |
|---|---|---|
| Loop sobre `cwd` | `QueryEngine` (sem mudança) | `engine.py:173` |
| Prompt do inspector | kwarg `system_prompt` + `.headlabs/rules.md` | `engine.py:182,191` |
| Explorar código read-only | `read_file`, `glob`, `grep` (`is_read_only=True`, sem prompt) | `tools/read_file.py`, `glob_tool.py`, `grep_tool.py` |
| Lint/testes/git | `bash` (gated) + `detect_test_command` | `bash.py`, `autofix.py:34` |
| Front rodando | `browser_devtools` (navigate localhost, screenshot, console, network) | `browser_devtools.py:204` |
| Achado estruturado e validado | tool com `input_schema` pydantic + `validate_input` | `tools/base.py:33`, `engine.py:227` |
| Modelo issue/severidade/fix | `TestEvaluation` + `parse_evaluation`/`normalize` | `testkit.py:45,73` |
| Persistir backlog por projeto | idiom `.headlabs/*.json` | `todo_write.py:19-65` |
| Aplicar correções (`--fix`) | loop edit→test→fix | `autofix.py` |
| Relatório HTML/MD | `Result.to_html/json/markdown` | `result.py:19` |
| Ponte de skills | `client.list_skills()` + `GET /resources/skill/{id}` → `content` | `cli.py:4026,4052` |

## 3. Interface (CLI)

```
headlabs local inspect [DIR] [--role ROLE] [-i CONTEXT]
                              [--url http://localhost:PORT] [--fix]
                              [--skill ID ...] [--provider self-hosted|platform]
                              [--html PATH] [-o human|json] [--yes]
headlabs local backlog [DIR]                 # lê .headlabs/local_backlog.json
headlabs local fix [DIR] [-i CONTEXT]        # remedia itens abertos do backlog local
```

- `DIR` default `.`. Roles: mesmos choices do `labs inspect`
  (`qa, ux, security, architect, performance, devops, data, frontend, backend`),
  default `qa`.
- `--fix` habilita `edit_file` + loop `autofix`. Sem `--fix`, o subset de tools é
  read-only (não muta nada).
- `--url` liga a inspeção de front-end (browser_devtools).
- `--skill ID` (repetível) injeta conteúdo de skills da plataforma no prompt.
- `--provider` default `self-hosted`; `platform` é fase 2 (ver §8).

## 4. Fluxo de execução (`_cmd_local_inspect`)

1. `cwd = DIR`. Carrega `LocalConfig`; valida configurado (ou erro claro, como
   `_build_engine` em `local_cli.py:92`).
2. Monta o prompt: `INSPECTOR_PROMPT[role]` (§5) + contexto `-i` + conteúdo de
   skills (`--skill`, §7) + `.headlabs/rules.md` (automático pelo engine).
3. Seleciona o subset de tools:
   - **inspeção (default):** `[ReadFileTool, GlobTool, GrepTool, WebFetchTool,
     BashTool]` (+ `BrowserDevtoolsTool` se `--url`). `bash`/`browser` são gated
     (pedem permissão salvo `--yes`); os read-only não pedem nada.
   - **`--fix`:** adiciona `EditFileTool`.
4. `engine = QueryEngine(provider, tools, pm, cwd=DIR, system_prompt=prompt,
   max_iterations=cfg.max_iterations)`.
5. `findings = []`. Roda `engine.run(task, on_event=...)`. O renderer (adaptado de
   `_render_event`, `local_cli.py:29`) mostra progresso; a tool `report_finding`
   (§6) acumula achados estruturados em `findings` conforme o modelo os emite.
6. Fallback: se o modelo não usou `report_finding` mas devolveu prosa+JSON,
   extrai via `parse_evaluation` (`testkit.py:73`).
7. **Front-end (se `--url`):** o prompt instrui a sequência `navigate → screenshot
   → get_console_logs → get_network_requests`; erros de console e requests
   falhados (4xx/5xx) viram findings automaticamente.
8. Renderiza igual ao `labs inspect` (`[severity] file → fix`, cf.
   `labsctl.py:1017-1039`) e persiste o backlog (§6). `--html` → `Result.to_html`.
9. **`--fix`:** após a inspeção, para cada finding com `fix`, roda o loop
   `autofix` existente (`detect_test_command` → aplica via `edit_file` →
   `run_test_command` → prova `IMPROVED/REGRESSED/UNCHANGED`). Reusa
   `autofix.py:34-74` integralmente.

## 5. `INSPECTOR_PROMPT` por role (novo — `src/headlabs/local/inspector.py`)

Constante base + variações por role, embarcadas no CLI (a "especialização" é
prompt versionado, não agente remoto). Base:

> Você é um inspector de QA/engenharia. Explore o projeto no diretório atual
> usando as ferramentas read-only. NÃO modifique arquivos. Para cada problema
> real que encontrar, chame `report_finding` com severidade, arquivo/linha,
> descrição e a correção sugerida. Não invente problemas; baseie-se em evidência
> lida. Ao terminar, dê um resumo de 1 frase.

Fragmentos por role (exemplos):
- `frontend`: a11y, erros de console, requests falhados, estado de loading/erro,
  render/SSR, bundle óbvio.
- `backend`: tratamento de erro, validação de input, segredos hardcoded, N+1,
  contratos de API.
- `security`: secrets no repo, injection, authz, deps vulneráveis (via `bash`
  read-only: `pip list`, `npm audit`).
- `devops`: Dockerfile, CI, healthcheck, config 12-factor.
- `data`: schema, migrations, integridade referencial.

## 6. Achados estruturados + backlog local

**Tool nova `report_finding`** (`src/headlabs/local/tools/report_finding.py`,
espelha `todo_write.py`), `input_schema` pydantic:

```python
class Finding(BaseModel):
    role: str
    severity: str        # critical | high | medium | low
    title: str
    file: str = ""
    line: int | None = None
    detail: str
    fix: str = ""
```

O engine valida via `validate_input` (`engine.py:227`). A tool acumula em memória
(callback) e faz append em `.headlabs/local_backlog.json`. Forma do item de
backlog **idêntica** à do `labs inspect` (`labsctl.py:1054`), para futura
convergência de UX:

```json
{"id": "...", "severity": "high", "resource": "src/app.py:42",
 "description": "...", "source": "inspector/backend (local)",
 "fix": "...", "status": "open"}
```

- `headlabs local backlog` → lê e renderiza abertos vs. done (clone de
  `_labs_backlog`, `labsctl.py:387`).
- `headlabs local fix` → pega itens abertos e roda o loop `autofix` sobre eles
  (equivalente local de `_labs_fix`/remediate, mas client-side).

Persistência: helpers no estilo `todo_write.py:32-65`
(`_backlog_path(cwd)`, load/save JSON sob `.headlabs/`).

## 7. Ponte de skills (viável hoje)

`--skill ID` (repetível): `HeadLabsClient().request("GET",
f"/resources/skill/{id}")` → `content` (cf. `cli.py:4052`), concatenado ao
`INSPECTOR_PROMPT` sob um header `# Skill: {id}`. Funciona com **qualquer**
backend (a busca é HTTP à plataforma; o loop continua onde estiver). Opcional:
mapa role→skill default (ex.: `security` puxa `security-checklist` se existir).

## 8. Backend LLM — o que é viável agora vs. fase 2

- **`--provider self-hosted` (default, viável hoje):** usa o endpoint
  OpenAI-compatible de `headlabs local config`. Para maior qualidade sem rodar um
  servidor próprio, o usuário pode apontar o `base_url` para qualquer endpoint
  OpenAI-compatible (vLLM hospedado, comercial, etc.).
- **`--provider platform` (implementado):** em vez de um gateway
  chat-completions (que a plataforma não expõe), o modo platform usa o padrão
  que o CLI já emprega em `agents`/`labs`: o CLI empacota o código localmente
  (`build_code_bundle`) e o envia a um agente declarativo Claude-backed via
  `client.invoke()` + `client.poll()`, depois faz parse das findings
  (`platform_findings_from_result`) para o mesmo backlog. Na 1ª execução
  provisiona o agente `local-code-inspector` (`ensure_platform_agent`,
  idempotente). Não usa `headlabs local config` nem `--profile` AWS. O código é
  enviado para a nuvem (considerar antes de usar em código sensível).

## 9. Mudanças de código (resumo)

Novos:
- `src/headlabs/local/inspector.py` — `INSPECTOR_PROMPT` base + roles + helper
  para montar o prompt (role + contexto + skills).
- `src/headlabs/local/tools/report_finding.py` — tool estruturada.
- `src/headlabs/local/backlog.py` — schema + persistência `.headlabs/local_backlog.json`.
- Testes: `tests/test_local_inspector.py`, `test_local_report_finding.py`,
  `test_local_backlog.py` (herméticos, fake provider como em `test_local_engine.py`).

Alterados:
- `src/headlabs/local_cli.py` — `_cmd_local_inspect`, `_cmd_local_backlog`,
  `_cmd_local_fix`; refatorar `_build_engine` para aceitar `tools`/`system_prompt`
  (ambos já são params do `QueryEngine`).
- `src/headlabs/cli.py` (~`4321-4339`) — subparsers `inspect`/`backlog`/`fix` sob
  `p_local_sub`; branches em `cmd_local` (`local_cli.py:53`).
- `src/headlabs/local/tools/__init__.py` — registrar `report_finding` (só no
  subset do inspector, não em `ALL_TOOLS` do run/chat geral).
- `docs/local-runtime.md` — seção do inspector.

## 10. Testes e verificação

- **Unit (pytest, herméticos):** engine dirigido por fake provider scriptado que
  emite tool-calls de `report_finding` → asserta backlog persistido; role prompt
  correto; subset read-only não inclui `edit_file` sem `--fix`; skill bridge com
  `client.request` mockado; parser de fallback (`parse_evaluation`).
- **E2E manual (não-mockado)**, no estilo do `docs/local-runtime.md`:
  1. `make -f Makefile.local up` (Ollama `llama3.1:8b`).
  2. `cd` num projeto de teste; `headlabs local inspect . --role backend --yes` →
     conferir findings reais e `.headlabs/local_backlog.json`.
  3. Front: servir um app (`python -m http.server`/dev server),
     `headlabs local inspect . --role frontend --url http://localhost:PORT --yes`
     → conferir que console/network errors viram findings.
  4. `headlabs local inspect . --fix --yes` → conferir edição + re-teste.
  5. `headlabs local backlog` e `headlabs local fix`.

## 11. Riscos / notas

- **Qualidade depende do modelo.** Um 8B self-hosted acha bem menos que o
  `loop-inspector` (Claude) da plataforma. Documentar como esperado; mitigar com
  role prompts fortes, ponte de skills e `--provider` apontável para modelo melhor.
- **browser_devtools exige Playwright/Chromium** (`pip install playwright &&
  playwright install chromium`, ~150MB) e **sempre pede permissão** — inspeção de
  front roda com `--yes` ou prompta.
- **`--fix` muta arquivos.** Rodar em repo git (undo via `undo.py` já existe).
  Sugerir confirmação/`--yes` explícito.
- **`--provider platform`** bloqueado por falta de gateway chat-completions no
  servidor (§8) — fica como dependência declarada, não entregável agora.
