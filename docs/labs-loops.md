# HeadLabs CLI — Labs & Loops

Guia de uso da superfície de **labs** (workspaces de projeto) e **loops** (builds
autônomos), no estilo `kubectl`/`aws-cli`: comandos resource-verb, saída
`table|wide|json`, acompanhamento ao vivo e exit codes semânticos para CI.

## Conceitos

- **Lab** — um *workspace de projeto*. Agrupa loops, acumula um **repositório** de
  arquivos e pode dar **push pro GitHub**.
- **Loop (job)** — um **build**: o pipeline multi-agente
  `orchestrator → researcher → architect → planner → executor → validator → deliverer`,
  com **gates** (pontos de aprovação humana).
- **Research (modo do loop)** — um loop com `mode=research`: só **pesquisa
  investigativa amplificada** (web search + agente de pesquisa ampla) que
  retorna **findings**, sem construir. Acumula contexto no lab. Veja
  [`research`](#research--investigar-sem-construir).
- **Gate** — pausa para aprovação. Por padrão o build pausa em **arquitetura**,
  **plano** e antes de ações **destrutivas**. Você aprova/rejeita e o build segue.

> `labs create -i "..."` cria o workspace **e já dispara o primeiro build**,
> retornando um `job_id`. Builds seguintes no mesmo lab: `loops create --lab ...`.

## Quickstart

```bash
# cria o lab + inicia o build, e acompanha ao vivo
headlabs labs create -i "API REST de encurtador de URL com analytics" \
  --stack python,fastapi,dynamodb,cdk -w

# lista builds ativos
headlabs status

# aprova o gate pendente e continua acompanhando
headlabs loops approve loop_9c83cba1fa52 --note "arquitetura ok" -w
```

---

## `labs` — workspaces

### create — cria o lab + primeiro build
```bash
headlabs labs create -i "API REST simples de notas em FastAPI" \
  --name notes-api --stack python,fastapi \
  [--auto-approve | --gate architecture,plan] \
  [-w | --wait] [-o table|wide|json]
```
- `-i, --intent` (obrigatório): objetivo do build em linguagem natural.
- `--name`: nome do lab (default: slug do intent).
- `--stack`: stack separada por vírgula.
- `--auto-approve`: resolve todos os gates automaticamente (roda ponta a ponta).
- `--gate`: mantém apenas estes gates (`architecture`, `plan`, `destructive`).
- `-w/--watch`: acompanha ao vivo; `--wait`: bloqueia até terminar (CI).

Saída (`-o json`):
```json
{ "lab_id": "lab_280a551ebbf8", "job_id": "loop_9c83cba1fa52", "name": "notes-api" }
```

### list / get / describe
```bash
headlabs labs list                 # tabela
headlabs labs list -o json         # para scripts
headlabs labs get notes-api        # por nome ou lab_id
headlabs labs describe notes-api   # + lineage de loops + resumo do repositório
```
```
LAB_ID            NAME       STACK           LOOPS  STATUS  AGE
lab_280a551ebbf8  notes-api  python,fastapi  1      active  3m
```

### repo — repositório produzido
```bash
headlabs labs repo notes-api                 # lista arquivos (tamanho, linguagem, path)
headlabs labs repo notes-api --cat app/main.py   # imprime o conteúdo de um arquivo
```

### push — GitHub
```bash
export GITHUB_TOKEN=ghp_xxx
headlabs labs push notes-api --repo minhaorg/notes-api --branch main \
  --message "Initial build from HeadLabs"
```

### archive
```bash
headlabs labs archive notes-api
```

---

## `loops` — builds (jobs)

### create — novo build num lab existente
```bash
headlabs loops create --lab notes-api -i "adicionar rate limiting" \
  [--auto-approve | --gate plan] [-w | --wait]
```

### list — filtra por lab/status
```bash
headlabs loops list                          # todos
headlabs loops list --lab notes-api          # de um lab
headlabs loops list --status awaiting_approval
headlabs loops list --active                 # só não-terminais
headlabs loops list -o wide                  # + coluna ITER (iteração)
```
```
JOB_ID             LAB               INTENT                   STAGE      STATUS             AGE
loop_9c83cba1fa52  lab_280a551ebbf8  API REST simples de no…  architect  awaiting_approval  1m
```

### status / get / describe — detalhe + pipeline
```bash
headlabs loops status loop_9c83cba1fa52
```
```
Job:     loop_9c83cba1fa52   (lab: lab_280a551ebbf8)
Intent:  API REST simples de notas (criar e listar) em FastAPI
Status:  awaiting_approval    Iteração: 0/5    Início: 1m atrás
Pipeline:
  ✔ orchestrator
  ✔ researcher
  ▸ architect
  · planner
  · executor
  · validator
  · deliverer

⏸  Gate pendente: after_architect — aprove com: headlabs loops approve loop_9c83cba1fa52
```
`-o json` retorna o objeto completo (para `jq`); exit code **1** se o build falhou.

### watch — acompanhamento ao vivo
```bash
headlabs loops watch loop_9c83cba1fa52 [--timeout 600]
```
```
Build loop_9c83cba1f…
● Fase: researcher
● Fase: architect
● paused após 01:18
⏸  Gate pendente: after_architect — aprove: headlabs loops approve loop_9c83cba1fa52
```
Para automaticamente em estado terminal ou num gate pendente.

### gates — aprovar / rejeitar
```bash
headlabs loops approve loop_9c83cba1fa52 --note "arquitetura ok" [-w]
headlabs loops reject  loop_9c83cba1fa52 --note "refazer: usar Postgres, não DynamoDB"
```
- `approve`: resolve o gate e retoma o build (`-w` continua acompanhando).
- `reject`: devolve à fase anterior com o feedback (`--note` obrigatório).

### ciclo de vida
```bash
headlabs loops pause   loop_9c83cba1fa52
headlabs loops resume  loop_9c83cba1fa52
headlabs loops retry   loop_9c83cba1fa52      # re-executa um build que falhou
headlabs loops cancel  loop_9c83cba1fa52
headlabs loops iterate loop_9c83cba1fa52 -i "trocar para Redis no rate limit"
headlabs loops logs    loop_9c83cba1fa52 [--phase executor]
```

---

## `research` — investigar, sem construir

Nem todo trabalho é construir. O modo **research** roda **apenas pesquisa
investigativa amplificada** (web search + um agente de pesquisa ampla e
abrangente) e retorna **findings** — sem `architect`/`planner`/`executor`, sem
gerar código. A pesquisa muitas vezes revela caminhos e ideias que *podem* (ou
não) virar produtos; um levantamento acurado e rigoroso dá a **base de contexto**
para o sucesso dos builds seguintes.

Internamente é um **loop** com `mode=research` dentro de um lab — os findings
ficam acumulados no **repositório do lab** e servem de contexto para builds
futuros (`loops create --lab ...`).

Pipeline (sem gates — é read-only, roda ponta a ponta):
```
orchestrator → researcher → analyst → synthesizer → deliverer
```

### investigar — um comando só
```bash
# o caso comum: só o tema. Default já é deep + todas as fontes disponíveis.
headlabs research "estado da arte em rate limiting distribuído"

# acompanhando ao vivo
headlabs research "estado da arte em rate limiting distribuído" -w

# investiga dentro de um lab existente (enriquece o contexto dele)
headlabs research "concorrentes e padrões de pricing de URL shorteners" --lab notes-api
```
O **tema** é posicional — não precisa de `-i`. As flags abaixo são só para
casos excepcionais (o default já cobre o uso normal):
- `--depth`: profundidade — `quick | standard | deep | exhaustive` (default `deep`).
- `--sources`: restringe as fontes, separadas por vírgula (default: **todas** as disponíveis, ex.: `web,docs,repo`).
- `--lab`: acumula os findings num lab existente (id ou nome); sem ela, cria um lab novo.
- `--name` / `--stack`: nome/tags do lab quando um novo é criado.
- `-w/--watch`: acompanha ao vivo; `--wait`: bloqueia até terminar (CI).

Saída (`-o json`):
```json
{ "lab_id": "lab_280a551ebbf8", "job_id": "loop_9c83cba1fa52", "mode": "research", "created_lab": true }
```

Ao concluir (com `-w`/`--wait`, ou depois via `status`), os **findings** são
renderizados: resumo executivo, principais achados, **caminhos/ideias** e fontes.

### acompanhar / listar
Investigações são loops — use a superfície de loops, que é ciente do modo e
renderiza os findings:
```bash
headlabs status loop_9c83cba1fa52        # pipeline + findings (se concluído)
headlabs loops watch loop_9c83cba1fa52   # ao vivo; renderiza findings no fim
headlabs loops list --mode research      # só investigações (coluna MODE)
```

---

## `status` — atalho de topo
```bash
headlabs status                  # builds ativos (= loops list --active)
headlabs status loop_9c83cba1fa52   # detalhe (= loops status)
```

---

## Gates & aprovação (fluxo)

| Flag em `create`            | Comportamento                                              |
|-----------------------------|-----------------------------------------------------------|
| *(nenhuma)*                 | Pausa em **arquitetura**, **plano** e antes de destrutivo |
| `--gate architecture,plan`  | Pausa só nos gates escolhidos                              |
| `--auto-approve`            | Não pausa — roda ponta a ponta                            |

Fluxo típico (default): o build roda até o gate de arquitetura → você revisa com
`loops status` → `loops approve` (ou `reject --note`) → segue até o próximo gate /
conclusão.

---

## Saída & automação

- `-o table` (default): legível, **colorido no TTY**, plano em pipe.
- `-o wide`: colunas extras (ex.: `ITER`, `LAST_RUN`).
- `-o json`: machine-readable — combine com `jq`:
  ```bash
  headlabs loops list -o json | jq -r '.[] | select(.status=="failed") | .loop_id'
  ```
- `--quiet`: imprime só os ids (para `xargs`):
  ```bash
  headlabs loops list --active --quiet | xargs -I{} headlabs loops cancel {}
  ```

### Exit codes (CI)
| Código | Significado            |
|--------|------------------------|
| `0`    | sucesso / concluído    |
| `1`    | build falhou           |
| `2`    | erro de uso            |
| `4`    | gate rejeitado         |
| `8`    | timeout                |

Exemplo em CI (bloqueia até terminar e falha o job se o build falhar):
```bash
headlabs labs create -i "build do serviço X" --auto-approve --wait || exit $?
```

---

## Modelo mental (resumo)

```
lab (workspace)
 ├── loop (build/job)   ──> orchestrator → researcher → architect ⏸ → planner ⏸ → executor → validator → deliverer
 │                                                          gate          gate
 ├── loop (research)    ──> orchestrator → researcher → analyst → synthesizer → deliverer   (sem gates → findings)
 └── repositório (arquivos + findings)  ──>  push GitHub
```
