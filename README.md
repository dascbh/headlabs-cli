# HeadLabs CLI

Run AI agents on your AWS accounts. Credentials stay local, analysis runs on HeadLabs AI.

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

This saves to `~/.headlabs/config.json`.

## Commands

### Run an agent

```bash
headlabs run finops --profile my-aws-profile
headlabs run finops --profile production --days 60
headlabs run finops --profile staging --question "por que Lambda está caro?"
headlabs run finops --profile prod --output json --no-browser
```

Options:
- `--profile` — AWS CLI profile (SSO or credentials)
- `--days` — lookback period (default: 30)
- `--question` — specific question for the analyst
- `--output` — `html` (default), `json`, or `md`
- `--no-browser` — don't auto-open the report

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

Reports are saved to `~/.headlabs/reports/`:

- **HTML** — dark theme, opens in browser, includes cost-by-account table and findings
- **JSON** — structured data with `cost_summary.by_account[]` and `analysis.insights[]`
- **Markdown** — for terminal/Slack/email

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
