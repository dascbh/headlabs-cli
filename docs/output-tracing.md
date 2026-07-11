# HeadLabs CLI — Output Padronizado & Observabilidade (Traces)

Toda execução de agente — via `run`, `chat` ou `agents test` — produz um **output
padronizado** e captura uma **trace estruturada**. O objetivo é que você tenha,
de forma consistente em qualquer superfície:

- saída legível por humanos **ou** legível por máquina (`--output-format`);
- visibilidade do *tracing* da execução: quais tools foram chamadas, raciocínio,
  tokens, custo estimado, erros e duração;
- traces **persistidas** localmente, **comparáveis** entre execuções e
  **exportáveis** para qualquer backend OpenTelemetry.

O contrato de dados é **versionado** (`schema_version`, hoje `1.0`) e segue as
convenções do mercado (OpenTelemetry GenAI semantic conventions, modelo
trace/span do OpenAI Agents SDK, NDJSON streaming estilo Claude Code).

---

## 1. Formatos de saída (`--output-format`)

Disponível em `headlabs run`, `headlabs chat` e `headlabs agents test`.

| Formato | Uso | Comportamento |
|---------|-----|---------------|
| `human` (default) | Terminal interativo | Spinner, dots coloridos, tool lines com tempo, bloco de resumo final |
| `json` | Scripts / captura do resultado final | **Silencioso** durante a execução; ao final emite **um único objeto JSON** (a trace completa + resultado) |
| `stream-json` | Monitoramento ao vivo / pipe para outra ferramenta | **NDJSON**: um evento JSON por linha em tempo real, terminando com uma linha `{"type":"result", ...}` |

> Nos formatos de máquina (`json` / `stream-json`) o CLI **não imprime banners,
> prompts ou resumos humanos** — a saída é limpa e parseável. A regra de
> precedência é: o stream nunca é "contaminado" por texto não-JSON.

### Exemplos

```bash
# Humano (default) — UX ao vivo
headlabs run finops --profile prod

# JSON — pega o resultado final estruturado (a trace inteira)
headlabs run finops --profile prod --output-format json > run.json

# Stream NDJSON — cada evento em tempo real (ótimo pra orquestração)
headlabs run finops --profile prod --output-format stream-json | jq -c 'select(.type=="tool_use") | .tool'
```

```bash
# Chat headless: cada linha do stdin = um turno; emite um trace por turno
echo "por que Lambda está caro?" | headlabs chat finops-advisor --profile prod --output-format json
```

### `--quiet` / `--verbose`

- `--quiet` — suprime progresso (no `human` mode). Erros ainda aparecem.
- `--verbose` — mostra todos os eventos, inclusive os de baixo nível.

---

## 2. O modelo de trace

Cada execução vira uma **`AgentTrace`** com:

```jsonc
{
  "schema_version": "1.0",
  "trace_id": "trace_ab12…",
  "workflow": "run",          // run | chat | test | test-exec
  "agent_id": "finops-advisor",
  "account_id": "123456789012",
  "status": "succeeded",
  "started_at": 1750000000.0,
  "ended_at":   1750000238.0,
  "metrics": {
    "tool_calls": 52,
    "llm_calls": 8,
    "input_tokens": 41200,
    "output_tokens": 6100,
    "total_tokens": 47300,
    "cost_usd": 0.2143,        // reportado pela plataforma, ou estimado por modelo
    "errors": 0,
    "duration_s": 238.0,
    "tools_used": { "explore_costs": 14, "get_rds_rightsizing": 3, … }
  },
  "result": { "summary": "…", "insights": [ … ], "total_saving_usd": 1322 },
  "events": [ /* AgentEvent[] — ver abaixo */ ]
}
```

Cada **`AgentEvent`** é uma operação com timestamp e identidade de span:

```jsonc
{
  "type": "tool_use",         // agent_start | llm_call | tool_use | tool_result
                              // | thinking | handoff | approval_request
                              // | status | step | error | metric
  "seq": 12,
  "ts": 1750000014.2,
  "trace_id": "trace_ab12…",
  "span_id": "span_…",
  "parent_span_id": "span_…", // reconstrói a árvore de spans
  "agent_id": "finops-advisor",
  "label": "explore_costs",
  "level": "info",            // info | warn | error
  "tool": "explore_costs",
  "data": { "input_tokens": 120, "output_tokens": 60, "model": "sonnet" }
}
```

Traces são salvas em `~/.headlabs/traces/{trace_id}.json`, com um índice
compacto em `~/.headlabs/traces/index.jsonl` que sustenta o `trace list`.

---

## 3. `headlabs trace` — inspecionar, comparar, exportar

Estilo `kubectl`/`aws-cli`: resource-verb, `-o table|json`, exit codes
semânticos (`0` ok, `2` não encontrado/uso).

### `trace list` — execuções recentes

```bash
headlabs trace list
headlabs trace list --agent finops-advisor --limit 50
headlabs trace list --workflow test -o json
```

```
TRACE             AGENT                 WORKFLOW    STATUS        AGE     DUR      TOOLS   TOKENS     COST
trace_6486f23d    finops-advisor        run         succeeded     3m      03:58    52      47300      $0.2143
trace_395aef1a    finops-advisor        run         succeeded     2h      04:12    49      45980      $0.2089
```

### `trace show` — timeline completa de uma execução

```bash
headlabs trace show trace_6486f23d      # id completo
headlabs trace show 6486f23d            # prefixo único também resolve
headlabs trace show trace_6486f23d -o json
```

Mostra métricas, **mix de tools** e a **timeline** evento a evento (com tokens
por chamada quando disponíveis), além do resumo do resultado.

### `trace diff` — comparar duas execuções

A capacidade-chave para detectar **regressões** e validar mudanças:

```bash
headlabs trace diff trace_395aef1a trace_6486f23d
headlabs trace diff 395aef1a 6486f23d -o json
```

```
diff  trace_395aef1a  →  trace_6486f23d
finops-advisor  (succeeded → succeeded)

METRIC                      A            B   DELTA
------------------------------------------------------
duration                  252          238   ▼ -14
tool calls                 49           52   ▲ +3
tokens                  45980        47300   ▲ +1320
cost $                 0.2089       0.2143   ▲ +0.0054
errors                      0            0   = 0
findings                   12           14   ▲ +2
savings $                1100         1322   ▲ +222

tools added: get_eks_node_efficiency
```

### `trace export` — OpenTelemetry ou JSON cru

```bash
# OTLP/JSON (GenAI semantic conventions) para stdout
headlabs trace export trace_6486f23d --format otel > spans.json

# POST direto para um collector OTLP/HTTP (Grafana Tempo, Datadog, …)
headlabs trace export trace_6486f23d --format otel --endpoint http://localhost:4318

# JSON nativo da trace (idêntico ao --output-format json do run)
headlabs trace export trace_6486f23d --format raw
```

O export OTel mapeia cada evento para um span com atributos padronizados:
`gen_ai.operation.name`, `gen_ai.agent.id`, `gen_ai.tool.name`,
`gen_ai.request.model`, `gen_ai.usage.input_tokens` / `output_tokens`. Assim a
trace aparece nativamente em qualquer backend que entenda OTLP.

---

## 4. Testes closed-loop (`agents test`)

`headlabs agents test` invoca o agente, faz um **critic** avaliá-lo em
dimensões fixas, **persiste** o resultado como uma trace (`workflow="test"`) e
**compara com o baseline** (o teste anterior do mesmo agente).

Dimensões avaliadas (0-100 cada): `task_completion`, `reasoning_quality`,
`tool_correctness`, `step_efficiency`, `output_structure`, `accuracy`, `safety`.

```bash
# Avaliação + delta vs baseline (humano)
headlabs agents test finops-advisor --profile prod

# Relatório estruturado (CI / dashboards)
headlabs agents test finops-advisor --profile prod --output-format json
```

### O loop fechado com `--fix`

Antes, aplicar um fix não dizia se melhorou. Agora o loop é **verificável**:

```bash
headlabs agents test finops-advisor --profile prod --fix
```

1. avalia o agente (score + dimensões + `fix_instructions`);
2. aplica o fix via `agents update`;
3. **re-roda a MESMA avaliação**;
4. compara antes/depois e emite um veredito:

```
  CLOSED-LOOP RESULT
  ----------------------------------------
  verdict     IMPROVED
  score       68 → 84  (▲+16)
  tool_calls  61 → 47
  dimensions:
    step_efficiency        55 → 80   ▲+25
    tool_correctness       60 → 78   ▲+18
```

Veredito: `IMPROVED` / `REGRESSED` / `UNCHANGED` (banda de ruído de ±3 pontos),
ou `BASELINE` na primeira medição. **Em CI, uma regressão após o fix sai com
exit code `1`.**

O relatório JSON (`--output-format json`) traz `evaluation`, `baseline` e
`comparison` completos — pronto para versionar e acompanhar evolução.

---

## 5. Integração em CI / automação

Padrões recomendados (alinhados às boas práticas de CLIs para agentes):

```bash
# 1. Output como contrato estável — parseie, não dependa de texto humano
result=$(headlabs run finops --profile prod --output-format json)
savings=$(echo "$result" | jq '.result.total_saving_usd')

# 2. Regressão de qualidade quebra o build
headlabs agents test finops-advisor --profile prod --fix --output-format json || exit 1

# 3. Exporte a trace para observabilidade central
trace_id=$(echo "$result" | jq -r '.trace_id')
headlabs trace export "$trace_id" --format otel --endpoint "$OTEL_COLLECTOR"
```

- **Exit codes**: `run`/`chat` saem com `1` em timeout/failed; `agents test`
  sai com `1` numa regressão após `--fix`; `trace` usa `2` para não encontrado.
- **Stream estável**: o `schema_version` versiona o contrato. Mudanças aditivas
  (novos campos/tipos) são seguras; mudanças incompatíveis exigem major bump.

---

## 6. SDK (Python)

O modelo de trace está disponível programaticamente:

```python
from headlabs import trace_store
from headlabs.tracing import AgentTrace

# Últimas traces
for entry in trace_store.list_traces(agent_id="finops-advisor", limit=10):
    print(entry["trace_id"], entry["status"], entry["cost_usd"])

# Carregar uma trace e inspecionar
trace = trace_store.load_trace("trace_6486f23d")
print(trace.metrics.tool_calls, trace.metrics.total_tokens)
for ev in trace.events:
    if ev.type == "tool_use":
        print(ev.tool, ev.input_tokens(), "→", ev.output_tokens())

# Exportar para OTel
from headlabs import otel
otlp = otel.trace_to_otlp(trace, service_name="headlabs")
```
