# HeadLabs — Guia de Uso (Labs, Loops e Banca de Revisão)

Guia prático, passo a passo, para criar builds autônomos com o CLI `headlabs` e
usar a **banca de revisão sênior** (LLM-as-judge). Para a referência completa de
comandos/flags, veja [`labs-loops.md`](./labs-loops.md). Para a arquitetura da
banca, veja `headlabs-platform/docs/judge-panel-spec.md`.

---

## 1. Conceitos

- **Lab** — workspace de um projeto. Agrupa builds e acumula um repositório de arquivos.
- **Loop (build)** — um build autônomo. Pipeline de agentes:
  `orchestrator → researcher → architect → planner → executor → validator → deliverer`.
- **Research** — quando você **não** quer construir, e sim **investigar**. Um loop
  com `mode=research` que roda só **pesquisa amplificada** (web search + agente de
  pesquisa ampla e investigativa) e retorna **findings** (resumo, achados,
  caminhos/ideias, fontes). Acumula contexto no lab pra embasar builds futuros.
  Veja [a seção `research`](./labs-loops.md#research--investigar-sem-construir).
- **Gate** — ponto de pausa para revisão: `after_architect`, `after_planner`,
  `before_destructive`.
- **Banca** — seniors LLM-as-judge que avaliam o artefato no gate (arquitetura/plano).
  Cada senior assume um papel (architect, security, backend, data, frontend, devops,
  performance, accessibility, cost, …) e carrega **skills** dinamicamente para embasar
  o parecer. Você controla custo × rigor com `--judges`, `--judge-model` e `--gate-mode`.

---

## 2. Pré-requisitos

Autenticação (uma vez):
```bash
headlabs config --key <SUA_API_KEY>      # grava em ~/.headlabs/config.json
```

Invocação — o comando é `headlabs`. Se não estiver no PATH, use o binário do venv:
```bash
cd ~/Documents/headlabs-cli
.venv/bin/headlabs ...
# ou: source .venv/bin/activate && headlabs ...
```

Sanity check:
```bash
headlabs labs list
```

---

## 3. Seu primeiro build (passo a passo)

### Passo 1 — criar o lab + iniciar o build
```bash
headlabs labs create \
  -i "Uma API REST de lista de tarefas (to-do): criar, listar, concluir e apagar, com DynamoDB + Lambda + API Gateway" \
  --judges full --gate-mode judge+human --judge-model fast
```
Retorna um `loop_id` (ex.: `loop_f771865e7be1`). As flags `--judges/--gate-mode`
ligam a banca (a banca avalia automaticamente e **você** decide o gate).

> Build seguinte no mesmo lab: `headlabs loops create --lab <lab_id> -i "..."`.

### Passo 2 — acompanhar ao vivo
```bash
headlabs loops watch <loop_id>
```
Mostra fases, **raciocínio (thinking)** e cada **tool** em tempo real. Quando o build
pausar em `after_architect`, é a vez da banca.

### Passo 3 — ver o parecer da banca
```bash
headlabs loops panel <loop_id>
```
Exibe, por senior: veredito (`approve`/`revise`/`escalate`), score, as `skills`
carregadas e os problemas por severidade (critical/high/medium/low).

### Passo 4 — decidir (modo `judge+human`)
```bash
headlabs loops approve <loop_id> --note "ok, seguir"
# ou mandar refazer com feedback:
headlabs loops reject  <loop_id> --note "faltou expiração de token e tags de custo"
```
O build segue para o gate do plano e, por fim, executa e entrega.

### Passo 5 — status, logs e resultado
```bash
headlabs status                  # todos os builds ativos
headlabs loops status <loop_id>  # detalhe (inclui resumo da banca)
headlabs loops logs   <loop_id>  # trace dos agentes
```

## 3b. Pesquisar (sem construir)

Quando o objetivo é **entender um tema** antes (ou em vez) de construir. O tema é
posicional e os defaults já cobrem o caso comum (**deep** + todas as fontes):

```bash
# o caso comum — só o tema
headlabs research "estado da arte e concorrentes em rate limiting distribuído"

# acompanhando ao vivo
headlabs research "estado da arte e concorrentes em rate limiting distribuído" -w

# dentro de um lab que já existe (enriquece o contexto dele)
headlabs research "padrões de pricing de URL shorteners" --lab notes-api
```

- É read-only: **não** gera código nem pede gates — roda ponta a ponta.
- Flags (`--depth quick|standard|deep|exhaustive`, `--sources web,docs,repo`) são
  só para casos excepcionais; sem elas, é `deep` em todas as fontes.
- Ao concluir, imprime **findings**: resumo, principais achados, **caminhos/ideias**
  e fontes. O relatório fica no repositório do lab (`headlabs labs repo <lab> --tree`).
- Reaproveite: `headlabs loops create --lab <lab> -i "..."` usa esses findings como contexto.

Acompanhar / listar (investigações são loops — a superfície de loops é ciente do modo):
```bash
headlabs status <loop_id>             # pipeline + findings
headlabs loops watch <loop_id>        # ao vivo
headlabs loops list --mode research   # só investigações
```

---

## 4. Controlando a banca (custo × rigor)

| Cenário | Flags |
|---|---|
| Banca informa, humano decide (default recomendado) | `--judges full --gate-mode judge+human` |
| Banca **autônoma** (aprova/refaz sozinha) | `--judges full --gate-mode judge --max-revise 2` |
| Barato e rápido (1 senior do gate, Haiku) | `--judges gate --judge-model fast` |
| Sem banca e sem pausas (build direto) | `--auto-approve` |

Parâmetros:
- `--judges` — `off` (sem banca) · `gate` (só o senior do gate) · `full` (banca completa)
- `--judge-model` — `standard` (Sonnet, rigor) · `fast` (Haiku, barato/rápido)
- `--gate-mode` — `human` (sem banca) · `judge+human` (default) · `judge` (autônomo)
- `--max-revise` — nº de auto-revisões no modo `judge` antes de escalar (default 2)

No modo `judge` (autônomo): `approve` → avança · `revise` → refaz a fase anterior
com o feedback (até `max_revise`, depois escala pro humano) · `escalate`/`critical` → humano.

---

## 5. Convocar a banca manualmente

Em qualquer build parado num gate (mesmo criado sem `--judges`):
```bash
headlabs loops review <loop_id> --judges full --reviewers security,backend,cost
headlabs loops panel  <loop_id>
```
- `--reviewers` — escolhe os seniors de competência (vírgula). Sem isso, a banca
  infere por sinal: `security` (sempre) + `frontend/backend/data/devops/performance/accessibility`
  conforme a arquitetura. `product`, `cost` e `docs` entram via `--reviewers`.

---

## 6. Outras operações de build
```bash
headlabs loops list [--lab <id>] [--status <s>] [--active]
headlabs loops pause   <loop_id>
headlabs loops resume  <loop_id>
headlabs loops retry   <loop_id>      # re-tenta após falha
headlabs loops cancel  <loop_id>
headlabs loops iterate <loop_id> -i "adicione paginação na listagem"
```

---

## 7. Pela interface web

Mesmo fluxo, visual: app → **/labs/** → abra o lab → clique no build. O **painel da
banca** fica no topo do drawer do build — veredito por senior, score, skills usadas,
issues por severidade, e botão **Convocar/Reconvocar**. O gate é aprovado/rejeitado ali.

---

## 8. Dicas e troubleshooting

- **`-o json`** em `list/get` para scripts; **`-w`/`--watch`** acompanha ao vivo;
  **`--wait`** bloqueia até terminar (útil em CI — exit codes semânticos).
- `404` em `list_available_skills`/`list_tenant_mcps` no watch são os agentes sondando
  endpoints opcionais — inofensivo, o build segue.
- Erro de TLS "self-signed certificate / Fortiguard" ao chamar `api.headlabs.ai`:
  é um **firewall de rede** (FortiGuard) bloqueando o domínio — troque de rede/VPN.
- `headlabs loops watch` para num gate (`paused`); rode `panel` → `approve`/`reject`
  para destravar.
