# HeadLabs CLI

Run AI-powered cloud agents using your local AWS credentials.

## Installation

```bash
pip install headlabs
```

## Quick Start

```bash
# Configure your API key
headlabs config --key YOUR_API_KEY

# Run the FinOps advisor agent
headlabs run finops --profile my-aws-profile --days 30
```

## Usage

### Run an agent

```bash
headlabs run finops --profile production --days 14 --output html
headlabs run finops --profile production --question "Why did costs spike last week?"
```

### Chat with an agent

```bash
headlabs run finops --profile dev --question "What are my top 3 cost drivers?"
```

### List available agents

```bash
headlabs agents
```

### Open last report

```bash
headlabs report --last
```

## Output Formats

Reports are generated in `~/.headlabs/reports/`:

- **HTML** — Dark-themed interactive report (default, opens in browser)
- **JSON** — Structured data for automation
- **Markdown** — Terminal-friendly summary

## Security

### Your credentials never leave your machine

HeadLabs CLI collects cloud metadata locally using your AWS credentials via boto3.
Only aggregated, non-sensitive metrics (cost totals, service usage patterns) are sent
to the HeadLabs API for AI analysis. Your AWS access keys, secrets, and session tokens
are **never** transmitted to any external service.
