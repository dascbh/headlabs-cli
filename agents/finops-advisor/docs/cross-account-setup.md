# FinOps Advisor — Cross-Account Setup

## O que é necessário em cada conta cliente

O agente precisa de permissão para **ler** dados de billing e infraestrutura
na conta do cliente. Ele **nunca escreve nada**.

### 1. Criar a IAM Role no lado do cliente

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AllowHeadLabsFinOpsAdvisor",
      "Effect": "Allow",
      "Principal": {
        "AWS": "arn:aws:iam::688128002471:role/headlabs-agent-finops-advisor-production"
      },
      "Action": "sts:AssumeRole",
      "Condition": {
        "StringEquals": {
          "sts:ExternalId": "headlabs-finops-<customer-id>"
        }
      }
    }
  ]
}
```

**Nome obrigatório:** `HeadLabsFinOpsReadOnly`

### 2. Permissões da role

Attach a managed policy `ReadOnlyAccess` mais as billing actions abaixo
(não incluídas no ReadOnlyAccess por padrão):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "BillingAndCostExplorer",
      "Effect": "Allow",
      "Action": [
        "ce:*",
        "billing:GetBillingData",
        "budgets:ViewBudget",
        "aws-portal:ViewBilling",
        "cur:DescribeReportDefinitions",
        "organizations:ListAccounts",
        "organizations:DescribeOrganization"
      ],
      "Resource": "*"
    }
  ]
}
```

### 3. Invocar o agente com a role

```json
{
  "tenant_id": "acme-corp",
  "input": {
    "tenant_id":       "acme-corp",
    "target_role_arn": "arn:aws:iam::123456789012:role/HeadLabsFinOpsReadOnly",
    "aws_region":      "us-east-1",
    "lookback_days":   30
  }
}
```

## Fluxo interno

```
Lambda (conta HeadLabs 688128002471)
  │
  ├─ os.environ["TARGET_ROLE_ARN"] = "arn:aws:iam::CUSTOMER:role/HeadLabsFinOpsReadOnly"
  │
  ├─ sts:AssumeRole → credenciais temporárias (1h)
  │       └─ cached em _assumed_credentials (escopo do invocation)
  │
  └─ todos os boto3.client() usam as credenciais assumidas
        ├─ ce.get_cost_and_usage()       → billing da conta cliente
        ├─ ec2.describe_instances()      → infra da conta cliente
        ├─ cloudwatch.get_metric_stats() → métricas da conta cliente
        └─ ...
```

## Modo single-account (dev / staging)

Omitir `target_role_arn`. O agente usa a própria IAM Role do Lambda
e analisa a conta onde o Lambda roda (688128002471).

## Deploy com múltiplas contas clientes

```bash
# Registrar ARNs no CDK context
npx cdk deploy HeadlabsFinOpsAdvisorStack-production \
  --context finOpsTargetRoles="arn:aws:iam::111111111111:role/HeadLabsFinOpsReadOnly,arn:aws:iam::222222222222:role/HeadLabsFinOpsReadOnly"
```

O construct automaticamente adiciona `sts:AssumeRole` para cada ARN listado
na IAM Policy do Lambda.
