# HeadLabs CLI

Run AI agents on your AWS accounts. Short-lived session credentials are derived
locally per run; the analysis runs on HeadLabs AI against **your** account.

## Prerequisites

- Python 3.9+
- AWS CLI configured with at least one profile

## Installation

```bash
pip install headlabs
```

Or from source:

```bash
git clone https://github.com/headlabs-ai/headlabs-cli.git
cd headlabs-cli
pip install -e .
```

## Setup

```bash
headlabs config --key YOUR_API_KEY
```

This saves to `~/.headlabs/config.json`. You can also set optional fields there:
`api_url` (override the API endpoint) and `tenant` (a default tenant override
for `chat`).

## Commands

### Run an agent

```bash
headlabs run finops --profile my-aws-profile
headlabs run finops-advisor --profile production --days 60
headlabs run finops --profile staging --question "por que Lambda está caro?"
headlabs run threat-detector --profile prod --quiet
```

The agent name accepts a friendly alias (`finops`) or the platform agent id
(`finops-advisor`).

Options:
- `--profile` — AWS CLI profile (SSO or credentials)
- `--account-id` — target AWS account ID (defaults to the profile's account)
- `--days` — lookback period (default: 30)
- `--question` — specific question for the analyst
- `--quiet` — suppress live progress (prints only the report paths)
- `--verbose` — show every progress event

Each run prints **live progress** as the agent works — local phases, the
agent's reasoning (`● Thought for Ns`), each tool call with elapsed time, and a
final summary with the top findings and potential savings:

```
finops-advisor  ·  production  ·  30d
  ● Perfil AWS resolvido   123456789012
  ● Credenciais temporárias preparadas
  ● Agente invocado   exec ea428dd0
  ● Thought for 7s
      ╰ Maior gastador: EKS Extended Support em versão antiga do Kubernetes...
  - discover_dimension_values   +00:12
  - explore_costs   +00:14
  ● Concluído em 03:58 · 52 tool calls

  Resumo
    ...

  Principais achados (14)
  ● [CRITICAL] EKS Extended Support ativo   $360/mo
  ...
  Economia potencial: $1,322/mês
```

A report (HTML + JSON) is always saved to `./reports/`.

### Chat with an agent

Interactive Q&A against your account, with the same live progress:

```bash
headlabs chat finops-advisor --profile my-aws-profile
headlabs chat finops-advisor --profile cactus --tenant cactus-gaming
```

Options:
- `--profile` — AWS CLI profile
- `--tenant` — tenant override (normally resolved automatically from your API
  key; only needed in edge cases)

Type `/exit` or `/quit` (or Ctrl+C) to leave.

### List agents on the platform

```bash
headlabs agents
```

### Create a declarative agent

```bash
headlabs agents create \
  --id my-analyst \
  --name "My Financial Analyst" \
  --prompt "You are a financial analyst that..." \
  --tools web_search,table_get \
  --description "Custom agent for quarterly analysis"
```

Or from a prompt file:

```bash
headlabs agents create --id my-agent --prompt-file ./prompt.md --tools web_search
```

### List skills

```bash
headlabs skills
```

### Create/update a skill

```bash
headlabs skills create --id compliance-rules-v1 --file ./compliance.md
```

### List tools and MCPs

```bash
headlabs tools
```

### Open last report

```bash
headlabs report --last
```

## Output

Each run saves a report to `./reports/` (in the current directory):

- **HTML** — dark theme, opens in a browser; executive summary, cost-by-account
  table, and findings.
- **JSON** — structured `Result`: `status`, `summary`, `insights[]`
  (each with `severity`, `title`, `finding`, `action`, `saving_usd`),
  `total_saving_usd`, `account_id`, and `cost_summary`.

Open the most recent report with `headlabs report --last`.

## Security

The analysis always runs against **your** account, never HeadLabs' own. Per run, the CLI:

1. Authenticates locally with your AWS profile (SSO/credentials).
2. Derives **short-lived session credentials** from that profile — existing
   temporary credentials (SSO/assumed role) are forwarded as-is; static IAM
   keys are exchanged for a fresh STS session token.
3. Sends those ephemeral credentials plus your account ID to the HeadLabs
   agent, which runs the analysis against your account and **stores nothing**.
4. Receives the report and renders it locally.

**Long-lived access keys are never transmitted** — only short-lived session
credentials scoped to the run. Credentials are never logged or persisted by
the platform. If no credentials are available, the agent **fails closed** (no
analysis) rather than touching any other account.

## SDK Usage (Python)

```python
from headlabs import HeadLabsClient

client = HeadLabsClient()

# List what's available
agents = client.list_remote_agents()
tools = client.list_tools()

# Create an agent
client.create_agent(
    agent_id="my-agent",
    display_name="My Agent",
    prompt="You are a helpful assistant.",
    tools=["web_search"],
)

# Run analysis
result = client.run("finops-advisor", aws_profile="production", days=30)
print(result.summary)
print(f"Potential savings: ${result.total_saving_usd}/mo")
result.to_html("/tmp/report.html")
```
