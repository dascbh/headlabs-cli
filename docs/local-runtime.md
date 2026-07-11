# `headlabs local` — Runtime Local, Tools e Migração para EKS

Este documento cobre o modo `headlabs local` (agent runtime standalone, apontando
para um LLM self-hosted OpenAI-compatible), como validá-lo localmente via Docker
Compose, e o caminho de migração para o EKS existente da HeadLabs.

`headlabs local` é independente de `run` / `chat` / `agents` / `run --local` —
esses continuam falando com a plataforma HeadLabs (ou um agente Dockerizado da
plataforma) sem qualquer alteração. `headlabs local` roda seu próprio loop de
tool-calls (provider → tools → permissão → engine) inteiramente no processo do
usuário, contra qualquer endpoint que fale o protocolo `/v1/chat/completions`
(vLLM, Ollama, LM Studio, TGI, SGLang, ...).

## 1. Validação local (Docker Compose)

### Por quê

Antes de gastar tempo/dinheiro configurando infraestrutura no EKS, validamos
que o contrato entre o `headlabs-cli` e um servidor OpenAI-compatible real
funciona ponta-a-ponta: streaming SSE, tool-calling, execução de tools,
permissões. Isso isola problemas de *protocolo/integração* de problemas de
*infraestrutura*.

### Stack

- `docker-compose.local.yml` — um serviço `ollama` (imagem oficial
  `ollama/ollama`), expondo `11434:11434` (API OpenAI-compatible em
  `/v1/chat/completions`), com volume persistente para os modelos baixados.
- `Makefile.local` — bootstrap: sobe o compose, faz `ollama pull` do modelo,
  configura o `headlabs local` para apontar para o container.

### Uso

```bash
make -f Makefile.local up      # sobe Ollama, baixa o modelo, configura headlabs local
headlabs local run "liste os arquivos deste diretório"
make -f Makefile.local down    # para o stack
```

Nota de plataforma: em macOS com Apple Silicon, o Docker não expõe
aceleração de GPU (Metal/MPS) para containers Linux — o Ollama roda em CPU
dentro do container. Isso é aceitável para *validar o protocolo*; não é
representativo de throughput de produção. Em EC2/EKS com GPU real (`g5`/`g6`),
o mesmo container roda dramaticamente mais rápido sem qualquer mudança de
código.

### Experiência de chat interativo (`headlabs local chat`)

`headlabs local run "<prompt>"` é single-shot e usa saída em texto puro
(pensado para scripts/CI). `headlabs local chat` abre um REPL interativo com:

- **Markdown renderizado** (títulos, listas, negrito, blocos de código com
  syntax highlight) via `rich`, atualizado em tempo real conforme o modelo
  faz streaming da resposta (`src/headlabs/local/render.py`, classe
  `ChatRenderer`).
- **Status de tool call ao vivo**: spinner enquanto a tool executa, depois
  substituído por `●` verde (sucesso) ou vermelho (erro) com preview do
  resultado — sem poluir a tela com todo o output crú da tool.
- **Histórico de comandos** (`↑`/`↓`) persistido em
  `~/.headlabs/local_chat_history`, via `prompt_toolkit`. Se `prompt_toolkit`
  não estiver instalado, cai de volta para `input()` builtin (sem histórico,
  mas funcional).

`headlabs local run` continua usando o renderer de texto puro (`_render_event`
em `local_cli.py`) — importante para não quebrar uso em scripts que capturam
stdout linha a linha.

### Escolha do modelo — achado real, não suposição

**Testado e confirmado**: nem todo modelo com "suporte a tool calling" anunciado
funciona corretamente com o parser de tool-calling do Ollama.

| Modelo | Resultado (testado via `curl` direto no Ollama, sem o CLI no meio) |
|---|---|
| `qwen2.5-coder:7b` (Ollama v0.31.1) | **Falha** — retorna a tool call como texto JSON solto no campo `content`, não no campo `tool_calls` estruturado. Isolado com `curl` puro, confirmando que não é bug do `headlabs-cli`. Consistente com bugs de mismatch renderer/parser por família de modelo documentados em issues do Ollama (ex. [ollama/ollama#14493](https://github.com/ollama/ollama/issues/14493), que trata de um caso similar — porém não idêntico — com Qwen 3.5). |
| `llama3.1:8b` (Ollama v0.31.1) | **Funciona corretamente** — `tool_calls` no campo certo do protocolo OpenAI. Testado via `curl` isolado e via `headlabs local run` ponta-a-ponta. |

**Modelo default do stack: `llama3.1:8b`.**

Implicação prática para quando migrarmos para vLLM/EKS: o parser de
tool-calling é específico por família/versão de modelo, tanto no Ollama
quanto no vLLM (`--tool-call-parser <nome>`). **Sempre validar
empiricamente** com uma chamada `curl` direta antes de assumir que um modelo
suporta tool-calling corretamente através de um servidor específico — a
documentação do modelo por si só não é garantia suficiente.

### Teste ponta-a-ponta realizado (não mockado)

Comando executado:
```bash
headlabs local run "Use the bash tool to run 'ls' in the current directory, \
  then tell me in one sentence what kind of project this is based on the \
  file names you see." --yes
```

Resultado real observado:
1. Modelo chamou `bash` (`ls`) — executado, resultado real retornado.
2. Modelo tentou chamar `read_file`/`edit_file` em `/dev/stdout` — tentativa
   inválida do próprio modelo; nossa tool tratou o erro (`Permission denied`)
   sem crashar, e devolveu a mensagem de erro para o modelo continuar.
3. Modelo chamou `bash` novamente.
4. Resposta final correta: identificou o projeto como Python a partir de
   `pyproject.toml`, `src`, `tests`.

Isso comprova o loop completo (`provider.stream` → parsing de tool_calls →
`tool.execute` → `tool_result` de volta ao histórico → nova chamada ao
modelo → resposta final) funcionando contra um LLM real, não um mock.

## 2. Gerenciamento de secrets de terceiros (Brave Search)

A tool `web_search` segue a mesma convenção já usada em outras partes deste
repositório para chaves de API de terceiros (ver `mcps/mcp-cclasstrib/server.py`,
`agents/*/tools.py`): nunca hardcoded, sempre lida do AWS Secrets Manager em
runtime, usando as credenciais AWS já disponíveis no ambiente do chamador.

- Secret: `headlabs/brave-search-api-key`, região `us-east-1`, conta
  `688128002471` — mesma chave usada pelos runtimes declarativos da
  plataforma (`api/routers/agents.py`) e pelo CDK (`BRAVE_API_KEY` no deploy).
- Requer `secretsmanager:GetSecretValue` nesse secret — já concedido pelo
  mesmo profile/role usado para `headlabs run --profile ...`.
- O valor é buscado uma vez por processo (`functools.lru_cache`) e nunca
  logado/impresso — só o resultado da busca aparece na saída do agente.
- Falha graciosamente (mensagem clara, sem crash) se as credenciais AWS não
  estiverem configuradas — confirmado testando com `AWS_PROFILE` inválido.

## 3. Tools disponíveis

O `headlabs local` implementa um subconjunto das ~40 tools do Claude Code
(referência: `docs/tools.md` no leaked source em `~/Documents/claude-code`),
escolhido por viabilidade sem infraestrutura extra — sem servidor MCP, sem
sessões de equipe/agentes paralelos, sem scheduler.

### Implementadas

| Tool | Read-only | Observação |
|---|---|---|
| `read_file` | Sim | Lê arquivo, opcionalmente por intervalo de linhas |
| `edit_file` | Não | Busca/substituição exata (estilo SEARCH/REPLACE) |
| `bash` | Não | Executa comando shell |
| `execute_python` | Não | Executa código Python em subprocesso isolado (`sys.executable -c`, nunca `exec()` in-process) — captura stdout/stderr/traceback/exit code separadamente do `bash` |
| `web_search` | Sim | Busca via Brave Search API (chave em Secrets Manager — ver seção 2) |
| `glob` | Sim | Busca arquivos por padrão glob, ordenado por mtime (mais recente primeiro) |
| `grep` | Sim | Busca de conteúdo por regex, em Python puro (`re`) — sem depender de `ripgrep`, confirmado ausente no ambiente de dev |
| `web_fetch` | Sim | Busca conteúdo de uma URL específica (HTML→texto simplificado, sem parser externo) |
| `todo_write` | Não* | Escreve lista de tarefas estruturada, persistida em `.headlabs/local_todos.json` por projeto. *Não pede aprovação — é bookkeeping local, sem efeito no sistema. |
| `ask_user_question` | Não* | Pausa e pergunta ao usuário via stdin. Em modo `--yes`/não-interativo, falha graciosamente (EOFError) e sinaliza ao modelo para seguir com seu melhor julgamento. |
| `config` | Varia | `get` (sempre livre) / `set` (pede aprovação; allowlist restrita a `max_iterations`/`timeout_s` — `base_url`/`model`/`api_key` só via `headlabs local config` no shell) |
| `browser_devtools` | Não | Automação/inspeção de browser real (Chromium headless via Playwright): `navigate`, `screenshot`, `get_console_logs`, `get_network_requests`, `evaluate` (JS), `click`, `get_text`, `close`. Equivalente em capacidade ao servidor MCP popular `chrome-devtools-mcp`, mas implementado como tool nativa (sem falar o protocolo MCP — ver justificativa na seção "Fora de escopo" abaixo). Sessão de browser persiste entre chamadas na mesma execução do processo; requer `pip install playwright && playwright install chromium` (download de ~150MB do binário Chromium). |

### Fora de escopo (documentado, não implementado) — e por quê

| Categoria do Claude Code | Exemplos | Por que não agora |
|---|---|---|
| **MCP** | `MCPTool`, `ListMcpResourcesTool`, `ReadMcpResourceTool`, `McpAuthTool`, `ToolSearchTool` | Exige implementar o protocolo MCP (cliente completo: descoberta, handshake, transporte). Testamos no Open WebUI que até integrações "prontas" (Open Terminal) exigem ajuste fino de prompt para funcionar bem com modelos 8B — vale revisitar quando houver um caso de uso concreto que justifique o investimento. Exceção pontual: a capacidade do servidor MCP `chrome-devtools-mcp` (automação/inspeção de browser) foi coberta por uma tool nativa (`browser_devtools`, via Playwright) em vez de um cliente MCP genérico — resolve o caso de uso concreto sem pagar o custo de implementar o protocolo inteiro. |
| **Agentes/Times** | `AgentTool`, `TeamCreateTool`, `TeamDeleteTool`, `SendMessageTool` | Requer orquestração multi-processo/multi-sessão — o `headlabs local` hoje é deliberadamente single-agent, single-processo. Adicionar isso é redesenhar o `engine.py`, não uma tool isolada. |
| **Planejamento formal** | `EnterPlanModeTool`, `ExitPlanModeTool` | O padrão "modo plano" do Claude Code depende de um modo de permissão dedicado (`plan`) que ainda não existe no `PermissionManager` (hoje só `default`/`auto`). Pode ser adicionado depois como um terceiro modo. |
| **Worktree isolation** | `EnterWorktreeTool`, `ExitWorktreeTool` | Precisa de gestão de `git worktree` — não implementado, mas é factível como tool isolada futuramente; não é bloqueado por nada estrutural. |
| **Background tasks** | `TaskCreateTool`, `TaskUpdateTool`, `TaskGetTool`, `TaskListTool`, `TaskOutputTool`, `TaskStopTool` | O `engine.py` roda um loop síncrono único; tarefas em background exigiriam um scheduler/executor assíncrono separado. Vimos no Open Terminal (Open WebUI) que esse padrão assíncrono é justamente onde modelos 8B mais falham (confusão com `process_id`, polling incorreto) — não é prioridade replicar essa fragilidade. |
| **Agendamento** | `ScheduleCronTool`, `RemoteTriggerTool` | Sem infraestrutura de scheduler no `headlabs local` (diferente da plataforma HeadLabs, que já tem `headlabs schedule`). |
| **LSP** | `LSPTool` | Exigiria embutir/gerenciar language servers por linguagem — investimento significativo de infra para um ganho que `grep`/`glob` já cobrem parcialmente (busca textual, não semântica). |
| **Notebooks** | `NotebookEditTool` | Caso de uso específico (Jupyter) não faz parte do escopo atual do `headlabs local`. |
| **Skills** | `SkillTool` | Depende de um sistema de skills (arquivos de instrução reutilizáveis) que a plataforma HeadLabs já tem (`headlabs skills`) — replicar localmente teria que decidir se compartilha ou duplica esse conceito; não decidido ainda. |

### O que já existe fora dessa lista, adjacente

`AskUserQuestionTool` e `TodoWriteTool` do Claude Code foram replicados nesta
fase porque não exigem nenhuma infraestrutura nova — só interação com
stdin/stdout e um arquivo JSON local, mesmo padrão dos outros arquivos de
estado (`local_config.json`, `local_permissions.json`).

## 4. Migração para o EKS existente da HeadLabs

### O que não muda

O `OpenAICompatibleProvider` do `headlabs-cli` não depende de onde o servidor
roda — só do `base_url` configurado via `headlabs local config --base-url`.
Migrar de Docker Compose local para o EKS é uma mudança de **infraestrutura**,
não de código.

### O que muda

| Camada | Local (Compose) | EKS |
|---|---|---|
| Runtime do modelo | Ollama, CPU, container único | vLLM (ou Ollama) como `Deployment`, GPU (`g5`/`g6`), via AWS Deep Learning Containers |
| Exposição | `localhost:11434` | `Service` ClusterIP + `Ingress`/`port-forward`, dependendo de exposição desejada |
| Provisionamento de GPU | N/A (CPU) | Karpenter provisionando node groups `g5`/`g6` sob demanda |
| Modelo persistido | Docker volume | Volume persistente (EBS/FSx) para evitar re-download em cada cold start |
| Parser de tool-calling | Nativo do Ollama | `--tool-call-parser <family>` explícito no vLLM (não é automático — validar por modelo, ver seção acima) |

### Passos de transição (quando formos aplicar)

1. Escolher vLLM (throughput) ou Ollama (simplicidade) como runtime no cluster
   — ver rascunho de manifests em `k8s/`.
2. Validar o mesmo par modelo+parser via `curl` direto no Service do cluster
   antes de apontar o `headlabs local` para ele — repetir a mesma validação
   empírica feita localmente (seção 1), pois o comportamento de tool-calling
   é por combinação model+runtime+versão, não algo que se assume.
3. Apontar `headlabs local config --base-url http://<service>.<namespace>.svc.cluster.local:8000/v1`
   (ou o endpoint exposto, se acesso for de fora do cluster).
4. Nenhuma mudança de código no `headlabs-cli` é esperada nesta migração.

Os manifests em `k8s/` (ver `k8s/README.md`) são um rascunho para essa
transição futura — **não foram aplicados** em nenhum cluster.

## 5. Inspector de projeto local (`headlabs local inspect`)

Diferente do `headlabs labs inspect` (server-side, black-box, sobre recursos
**já implantados** na plataforma — ver `docs/local-inspector.md` para o
contraste completo), o `headlabs local inspect` é **client-side e white-box**:
dirige o mesmo `QueryEngine` do runtime local com um prompt de inspector e um
subconjunto **read-only** de tools (`read_file`/`glob`/`grep`/`web_fetch` +
`bash` gated), inspecionando um diretório qualquer em disco. Requer um endpoint
LLM configurado via `headlabs local config` (igual a `run`/`chat`).

```bash
headlabs local inspect .                       # inspeção QA do diretório atual
headlabs local inspect ./app --role backend -i "foco em auth"
headlabs local inspect ./app --role frontend --url http://localhost:5173
headlabs local inspect . --skill sec-checklist # injeta uma skill da plataforma
headlabs local inspect . --fix --yes           # aplica correções + loop de teste
headlabs local backlog                         # ver .headlabs/local_backlog.json
headlabs local fix                             # corrigir itens abertos do backlog
```

- **Roles** (`--role`): mesmos do `labs inspect`
  (`qa/ux/security/architect/performance/devops/data/frontend/backend`) + `usability`.
  A especialização é um prompt embarcado no CLI (`src/headlabs/local/inspector.py`),
  não um agente remoto — por isso funciona 100% self-hosted.
- **`--role usability` + `--provider platform`**: inspeção de **duas camadas**
  da URL **viva** (`--url`), desenhada para consistência:
  1. **Determinística** — o CLI chama o MCP `browser-devtools` diretamente
     (`a11y_audit` via axe-core + `inspect_page` mobile) e converte os sinais
     brutos em findings ancorados: WCAG (regra axe), responsivo (overflow, tap
     targets), performance (FCP) e erros de runtime. Sem LLM → **100%
     reproduzível** (mesma URL ⇒ mesmos findings objetivos, sempre; a chave de
     dedup é o id da regra/sinal, ex. `wcag:select-name`).
  2. **Heurística** — um **agente sintetizador** dedicado `usability-inspector`
     (Claude, sem MCP) recebe os *resultados* dessas mesmas checagens e adiciona
     só o que uma engine de regras não pega (clareza de conteúdo, carga do
     formulário, estados faltando), ancorado nos dados — sem repetir a camada
     objetiva nem alucinar.

  Assim o LLM sai do caminho dos findings objetivos (a fonte de variância) e fica
  só na camada de julgamento, que é aditiva. As chamadas ao browser reutilizam
  backoff adaptativo para absorver cold start do runtime. Requer `--url`.
  Ex.: `headlabs local inspect . --role usability --url https://meuapp.com --provider platform`.
- **Achados estruturados**: o modelo registra cada issue via a tool
  `report_finding` (schema pydantic validado pelo engine), persistida em
  `.headlabs/local_backlog.json` — mesmo formato de item do backlog do
  `labs inspect`. Há fallback tolerante que extrai findings do texto final se o
  modelo não usar a tool.
- **Front-end (`--url`)**: usa `browser_devtools` (navigate localhost →
  screenshot → console logs → network requests); erros de console e requests
  4xx/5xx viram findings. Requer Playwright/Chromium e aprovação (ou `--yes`).
- **`--fix`**: habilita `edit_file` e reusa o loop `autofix` (edit→test→fix);
  marca itens como `done` só se a suíte ficar verde.
- **Skills (`--skill ID`)**: busca o conteúdo da skill na plataforma
  (`GET /resources/skill/{id}`) e injeta no prompt — best-effort, funciona com
  qualquer backend; falha graciosamente offline.
- **`--provider platform`**: roda a inspeção na HeadLabs com um agente
  declarativo Claude-backed. Como o runtime da nuvem não lê seu disco, o CLI
  **empacota o código localmente** (`build_code_bundle`) e o envia via
  `invoke`+`poll` — o mesmo padrão de `agents`/`labs`, sem exigir `--profile`
  AWS. Na primeira execução provisiona (idempotente) o agente
  `local-code-inspector`. Não precisa de `headlabs local config`. Trade-off:
  qualidade muito maior (Claude Sonnet) ao custo de tokens da plataforma e de
  enviar o código para a nuvem — use `--provider self-hosted` (default) para
  ficar 100% local/offline.
