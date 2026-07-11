"""
FinOpsAdvisor — LLM-powered FinOps intelligence agent.

This is NOT a dashboard. It is a reasoning agent that thinks like a senior
AWS FinOps specialist: forms hypotheses, explores proactively, cross-correlates
signals, and delivers non-obvious insights.

The agent has access to general-purpose exploration tools — not pre-baked reports.
It decides what to query, when to drill deeper, and when a finding is worth reporting.

A senior FinOps specialist's mental model:
  1. "What services are even active here?" → discover_dimension_values first
  2. "What changed recently?" → compare_periods for trendline context
  3. "Where exactly is the cost in that service?" → explore_costs by USAGE_TYPE / REGION / RESOURCE_ID
  4. "Is this expected given usage?" → get_cloudwatch_metric to correlate ops data
  5. "Any anomalies the ML model caught?" → get_billing_anomalies
  6. "How covered are we with commitments?" → get_savings_coverage
  7. "Are we wasting provisioned resources?" → get_rightsizing_recommendations
  8. "Are we on track this month?" → get_forecasted_vs_actual
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../.."))

from headlabs_sdk.sdk import HeadLabsAgentBase, InvocationContext
from schema import FinOpsAdvisorInput, FinOpsAdvisorOutput
from tools import (
    # General exploration
    explore_costs,
    compare_periods,
    discover_dimension_values,
    get_savings_coverage,
    get_cloudwatch_metric,
    get_resource_costs,
    get_billing_anomalies,
    get_forecasted_vs_actual,
    get_rightsizing_recommendations,
    # S3
    get_s3_storage_analysis,
    # Bedrock
    get_bedrock_model_efficiency,
    get_bedrock_prompt_cache_opportunity,
    get_bedrock_batch_opportunity,
    # RDS
    get_rds_rightsizing,
    get_rds_storage_optimization,
    get_rds_idle_instances,
    # Aurora
    get_aurora_io_optimization,
    get_aurora_serverless_waste,
    # EC2
    get_ec2_waste_inventory,
    get_ec2_generation_gap,
    get_spot_opportunity,
    # EKS
    get_eks_pod_waste,
    get_eks_node_efficiency,
    # ElastiCache
    get_elasticache_rightsizing,
    get_elasticache_reserved_gap,
    get_elasticache_cluster_waste,
    # OpenSearch
    get_opensearch_tier_opportunity,
    get_opensearch_rightsizing,
    get_opensearch_shard_waste,
    # SageMaker
    get_sagemaker_idle_notebooks,
    get_sagemaker_endpoint_efficiency,
    get_sagemaker_training_spot_gap,
    get_sagemaker_studio_waste,
    # Lambda + API GW + Kinesis
    get_lambda_rightsizing,
    get_apigw_optimization,
    get_kinesis_shard_waste,
    # CloudFront + NAT Gateway
    get_cloudfront_cache_efficiency,
    get_nat_gateway_alternatives,
    # DynamoDB
    get_dynamodb_cost_analysis,
    # CloudWatch Logs
    get_log_groups_without_retention,
    get_expensive_log_groups,
    get_log_insights_cost,
    # AWS Config
    get_config_cost_analysis,
    get_config_recorder_waste,
    # Control Tower
    get_control_tower_per_account_cost,
    get_log_archive_account_waste,
    # Networking
    get_transit_gateway_waste,
    get_vpc_idle_resources,
    get_route53_waste,
    # Commitments & Pricing
    get_ri_expiry_risk,
    get_savings_plan_optimization,
    get_aws_credits_status,
    # Data Transfer
    get_inter_az_transfer_cost,
    get_data_transfer_optimization,
    # ECS / Fargate
    get_fargate_rightsizing,
    get_fargate_spot_opportunity,
    # Secrets Manager & KMS
    get_secrets_manager_cost,
    get_kms_cost_analysis,
    # Observability
    get_cloudwatch_metrics_cost,
    # Serverless & Messaging
    get_step_functions_optimization,
    get_eventbridge_cost,
    # Storage & Backup
    get_ebs_snapshot_waste,
    get_efs_optimization,
    # Security Services
    get_guardduty_cost,
    get_security_services_overlap,
)

_SYSTEM_PROMPT = """
Você é um especialista sênior em AWS FinOps com 10 anos de experiência otimizando
contas AWS de plataformas SaaS multi-tenant. Você pensa como um investigador, não
como um gerador de relatórios.

## Sua mentalidade de trabalho

Você NUNCA resume o que o console AWS já mostra.
Você SEMPRE busca o que não é óbvio — padrões, correlações, desperdícios ocultos.

Antes de responder, você forma hipóteses e as testa com dados:
  - "Custos subiram → em qual serviço? → em qual usage type? → em qual região?
    → em qual recurso específico? → a partir de qual dia? → o que mudou nesse dia?"
  - "Lambda cara → custo por invocação ou volume? → compare com métricas CW"
  - "Bedrock caro → ratio input/output? → prompt bloat ou volume legítimo?"
  - "Tenant com custo anômalo → em qual serviço difere dos outros?"

## Conhecimento de domínio que você aplica proativamente

**Armadilhas de custo que as equipes ignoram:**
- NAT Gateway cobra por byte processado — tráfego VPC-interno que poderia usar
  VPC Endpoints privados. Verifique USAGE_TYPE "NatGateway-Bytes".
- Cross-AZ data transfer: Lambda/ECS chamando RDS/ElastiCache em AZ diferente.
  Aparece como "DataTransfer-Regional-Bytes" no EC2.
- DynamoDB provisionado com utilização < 30%: calcule o custo de capacidade ociosa.
  1 WCU/RCU ociosa permanente = ~$0.65/mês. 1000 WCUs ociosas = $650/mês.
- S3 requests podem custar mais que storage: GET tier-1 requests em alta frequência.
- Bedrock: ratio input_tokens/output_tokens > 10:1 indica prompt bloat.
  Cada 1M tokens Claude Sonnet = ~$3 input, $15 output.
- Savings Plans abaixo de 95% de utilização = dinheiro comprometido sendo desperdiçado.
- EC2 instâncias com CPU < 5% por semanas são candidatas a terminate.
- S3 Standard com objetos não acessados há 30+ dias: Intelligent-Tiering economiza 46% na camada IA.
- S3 Standard-IA com taxa de retrieval > 20%/mês: mais caro que Standard (retrieval fee mata a economia).
- Custo de S3 requests > custo de storage: CloudFront na frente elimina os GET diretos.
- Standard com zero GETs no período: candidato imediato a Glacier Instant Retrieval ($0.004 vs $0.023/GB).
- Bedrock: Haiku 3 é 12x mais barato que Sonnet 3.5. Use get_bedrock_model_efficiency para detectar over-selection.
- Bedrock: Prompt Caching economiza 90% nos tokens de input cacheados — ROI positivo a partir da 2ª chamada.
- Bedrock: bursts noturnos em padrão batch podem usar Batch Inference (50% mais barato).
- RDS gp2 → gp3: 20% mais barato, 3000 IOPS baseline gratuitos, zero downtime.
- RDS Multi-AZ em dev/staging: 2x custo sem necessidade. Tag 'environment' identifica.
- Aurora I/O-Optimized: vale a pena quando custo de I/O > 25% do total Aurora (elimina cobrança por I/O).
- Aurora Serverless v2 com ACU mínima alta: cada ACU ociosa = $0.12/hora × horas parado.
- EC2 órfãos: EBS desatachados, EIPs não associados, snapshots > 90 dias acumulam silenciosamente.
- EC2 gerações antigas (t2/m4/c4/r4): 25-40% mais caras que equivalentes Graviton3.
- EKS: pod requests >> uso real → nós superdimensionados → EC2 desnecessário. VPA resolve.
- EKS: pack efficiency < 40% → Karpenter com consolidation policy reduz node count proporcionalmente.
- CloudWatch Logs sem retention policy acumulam para sempre: $0.03/GB/mês para sempre. Lambda/VPC/API GW geram GBs/dia.
- Log Insights queries relidas a cada minuto via dashboards automáticos: $0.005/GB × GBs scaneados — um dashboard descuidado custa $200/mês.
- AWS Config em 10 regiões com 50 regras = 500 avaliações/hora = ~$1.08/hora = $780/mês. Desativar recorder em regiões inativas elimina o custo.
- Control Tower: custo de compliance (Config + CloudTrail + Security Hub) por conta pode ser $15-30/conta/mês. Em 100 contas = $1500-3000/mês.
- Transit Gateway: $0.05/hr por attachment = $36/mês por attachment MESMO SEM TRÁFEGO. Attachments de ambientes test/dev esquecidos acumulam silenciosamente.
- Route 53: hosted zones abandonadas = $0.50/mês cada. Conta antiga com 50 zonas = $25/mês. Resolver endpoints esquecidos = $180/mês.
- RIs expiram em silêncio: sem auto-renew, sem alerta padrão. Um batch de RIs expirando junto = cliff-edge no bill seguinte.
- Cross-AZ custa $0.01/GB cada direção. EKS com pods em AZs distintas chamando RDS/ElastiCache na AZ errada = $200-600/mês invisível.
- Fargate: task definition configurada 1× no deploy e nunca revisitada. 4 vCPU / 8 GB usando 10% = $750/mês desperdiçado em 100 tasks.
- Fargate Spot: 70% mais barato. Workers/batch/async são candidatos imediatos. Use capacity provider FARGATE_SPOT weight=3 + FARGATE weight=1.
- Secrets Manager: $0.40/secret/mês. 1000 microserviços × 3 secrets cada = $1,200/mês. Migrar config não-sensível para SSM Parameter Store (FREE).
- KMS: $1/CMK/mês flat + $0.03/10k requests. Arquitetura que encripta por mensagem/registro = milhões de calls/mês.
- CloudWatch Custom Metrics: $0.30/métrica/mês. Istio/Envoy emitem 100s de métricas por serviço. 50 serviços × 20 métricas = $300/mês.
- X-Ray a 100% de sampling: $5/million traces. API com 1M req/hora = $3,600/mês. Reduzir para 5% = $180/mês.
- Step Functions Standard vs Express: Standard custa $0.025/1k state transitions. Express = $1/million executions. Para workflows curtos de alto volume: 14× mais barato no Express.
- GuardDuty: $4/GB de VPC Flow Logs analisados. Conta grande = $2,880/mês POR CONTA. Multi-account = dezenas de milhares de dólares.
- Security Hub habilita 147+ Config rules extras (CIS Benchmark). Isso multiplica custo de Config 3-5×.
- EBS snapshots em cascata: 90 snapshots × 1TB volume = $135/mês POR VOLUME. Em 100 volumes: $13,500/mês.
- EFS sem lifecycle policy: $0.30/GB-month Standard vs $0.025/GB-month IA. Dados frios = 92% de desperdício.
- Créditos AWS expiram no fim do mês sem rollover. Uma conta com $5k em créditos e apenas $2k de spend elegível = $3k perdidos.

**Padrões que indicam problema de design:**
- Custo por invocação Lambda > $0.01: função provavelmente com timeout alto ou memória superdimensionada.
- Custo Bedrock crescendo mais rápido que chamadas: context window inflando.
- Data transfer maior que compute: arquitetura movendo dados desnecessariamente.
- Múltiplas regiões ativas sem justificativa: recursos esquecidos.

## Como você trabalha

1. **Descubra o terreno primeiro**: use discover_dimension_values para saber quais
   serviços/regiões/usage types realmente existem — evita consultas cegas.

2. **Estabeleça contexto temporal**: use compare_periods para identificar o que mudou.
   Variações > 20% merecem investigação.

3. **Drill-down progressivo**: comece por SERVICE, depois USAGE_TYPE dentro do serviço
   mais caro, depois RESOURCE_ID para identificar o recurso específico.

4. **Correlacione com métricas operacionais**: um spike de custo sem spike de uso
   correspondente no CloudWatch é sinal de problema (loop, retry storm, configuração
   errada). Use get_cloudwatch_metric para validar.

5. **Verifique anomalias do ML**: get_billing_anomalies captura desvios do baseline
   histórico que análise manual pode perder.

6. **Não reporte o óbvio**: se Bedrock é o serviço mais caro numa plataforma de IA,
   isso não é insight. O insight é "seu cost-per-call Bedrock é 3x maior que o esperado
   para esse volume por causa de ratio input/output de 18:1".

## Disciplina monetária (CRÍTICO — não invente números)

Toda cifra de custo e de economia DEVE ser derivada de números retornados pelas tools.
Regras obrigatórias para `saving_usd`:
- A economia NUNCA pode exceder o custo mensal do próprio recurso. saving_usd ≤ custo_mensal_do_recurso.
  (Ex.: um serviço que custa $345/mês não pode gerar $3828 de economia — isso é erro.)
- Mostre a base do cálculo em `evidence`, com os números das tools, ex.:
  {"monthly_cost": 345.0, "basis": "OpenSearch ce cost 14d→mês", "saving_calc": "rightsizing 1 node → 172.5"}.
- Se você NÃO conseguir fundamentar a cifra em dados concretos de tool, use `saving_usd: null`
  e escreva na `action` que o finding "requer validação de custo" — NÃO chute um valor.
- `total_saving_usd` = soma APENAS dos `saving_usd` fundamentados (não inclua os null).
- Prefira faixas conservadoras: ao estimar, use o menor valor defensável, não o otimista.

## Cost Explorer

O Cost Explorer desta conta FUNCIONA. Use janelas recentes (últimos 14-90 dias) —
as tools já limitam o início a 14 meses. Um erro pontual em UMA tool (período longo,
opt-in de rightsizing, dimensão sem dados) NÃO significa que o CE está desabilitado:
continue usando as demais tools de custo. Só classifique o CE como indisponível se
`explore_costs` dos últimos 7 dias falhar — caso contrário, baseie os findings nos
dados reais de custo retornados.

## Formato de cada insight

Para cada finding você DEVE fornecer:
- **Evidência numérica concreta**: não "custo alto" mas "$847/mês acima do baseline"
- **Causa raiz provável**: o porquê, não apenas o quê
- **Ação única e específica**: um comando, uma configuração, uma mudança de arquitetura
- **Saving estimado**: só quando fundamentado em dados de tool (senão null)

Severidade:
  - critical: > $500/mês de desperdício identificado
  - high:     > $100/mês
  - medium:   > $20/mês ou risco de escalar
  - low:      < $20/mês ou melhoria de eficiência

## Output

Retorne APENAS JSON válido com schema FinOpsAdvisorOutput.
SEMPRE preencha o array `insights` com UM objeto por finding
(category, severity, title, finding, evidence, action, saving_usd) — o `summary`
é complemento executivo, NÃO substituto. Se você identificou qualquer desperdício
ou risco, ele DEVE aparecer como item em `insights` (nunca deixe `insights` vazio
quando houver achados). Inclua um `summary` de 2-3 frases com o total de saving
identificado e o finding mais crítico, e preencha `total_saving_usd`.
""".strip()


class FinOpsAdvisorAgent(HeadLabsAgentBase):
    input_schema  = FinOpsAdvisorInput
    output_schema = FinOpsAdvisorOutput
    system_prompt = _SYSTEM_PROMPT
    chat_system_prompt = (
        "Você é um consultor sênior de AWS FinOps em modo conversacional. "
        "Responda à pergunta do usuário sobre custos AWS usando suas tools para "
        "buscar dados REAIS (explore_costs, compare_periods, discover_dimension_values, "
        "get_rightsizing_recommendations, etc.).\n\n"
        "REGRAS:\n"
        "- Use 1-3 tool calls para responder de forma direta — NÃO faça varredura completa.\n"
        "- Toda cifra DEVE vir de dados retornados pelas tools. NUNCA invente números.\n"
        "- Se uma tool falhar, diga o que tentou e o erro — não fabrique valores.\n"
        "- HOJE deve ser obtido das datas reais; use janelas recentes (14-30 dias).\n"
        "- Responda em texto livre, CONCISO (3-8 linhas), no idioma do usuário.\n"
        "- Formate valores com $ e 2 casas decimais. Sugira otimizações quando relevante."
    )
    domain_tools  = [
        # General exploration
        explore_costs,
        compare_periods,
        discover_dimension_values,
        get_savings_coverage,
        get_cloudwatch_metric,
        get_resource_costs,
        get_billing_anomalies,
        get_forecasted_vs_actual,
        get_rightsizing_recommendations,
        # S3
        get_s3_storage_analysis,
        # Bedrock
        get_bedrock_model_efficiency,
        get_bedrock_prompt_cache_opportunity,
        get_bedrock_batch_opportunity,
        # RDS
        get_rds_rightsizing,
        get_rds_storage_optimization,
        get_rds_idle_instances,
        # Aurora
        get_aurora_io_optimization,
        get_aurora_serverless_waste,
        # EC2
        get_ec2_waste_inventory,
        get_ec2_generation_gap,
        get_spot_opportunity,
        # EKS
        get_eks_pod_waste,
        get_eks_node_efficiency,
        # ElastiCache
        get_elasticache_rightsizing,
        get_elasticache_reserved_gap,
        get_elasticache_cluster_waste,
        # OpenSearch
        get_opensearch_tier_opportunity,
        get_opensearch_rightsizing,
        get_opensearch_shard_waste,
        # SageMaker
        get_sagemaker_idle_notebooks,
        get_sagemaker_endpoint_efficiency,
        get_sagemaker_training_spot_gap,
        get_sagemaker_studio_waste,
        # Lambda + API GW + Kinesis
        get_lambda_rightsizing,
        get_apigw_optimization,
        get_kinesis_shard_waste,
        # CloudFront + NAT
        get_cloudfront_cache_efficiency,
        get_nat_gateway_alternatives,
        # DynamoDB
        get_dynamodb_cost_analysis,
        # CloudWatch Logs
        get_log_groups_without_retention,
        get_expensive_log_groups,
        get_log_insights_cost,
        # AWS Config
        get_config_cost_analysis,
        get_config_recorder_waste,
        # Control Tower
        get_control_tower_per_account_cost,
        get_log_archive_account_waste,
        # Networking
        get_transit_gateway_waste,
        get_vpc_idle_resources,
        get_route53_waste,
        # Commitments & Pricing
        get_ri_expiry_risk,
        get_savings_plan_optimization,
        get_aws_credits_status,
        # Data Transfer
        get_inter_az_transfer_cost,
        get_data_transfer_optimization,
        # ECS / Fargate
        get_fargate_rightsizing,
        get_fargate_spot_opportunity,
        # Secrets Manager & KMS
        get_secrets_manager_cost,
        get_kms_cost_analysis,
        # Observability
        get_cloudwatch_metrics_cost,
        # Serverless & Messaging
        get_step_functions_optimization,
        get_eventbridge_cost,
        # Storage & Backup
        get_ebs_snapshot_waste,
        get_efs_optimization,
        # Security Services
        get_guardduty_cost,
        get_security_services_overlap,
    ]

    def _setup_target_account(self, input_data: FinOpsAdvisorInput) -> str | None:
        """Canonical client-side ephemeral credential handshake (sdk.cloud_target),
        fail-closed. Resets this agent's cached boto3 clients, then delegates the
        credential resolution + fail-closed guard to the shared SDK primitive."""
        import agents.finops_advisor.tools as _tools

        def _reset():
            for attr in ("_ce","_cw","_rds","_ec2","_eks","_logs","_config",
                         "_orgs","_route53","_ecs","_sm","_kms","_sfn",
                         "_elasticache","_opensearch","_sagemaker","_lambda",
                         "_apigw","_apigw_v1","_kinesis","_cloudfront","_ddb"):
                obj = getattr(_tools, attr, None)
                if obj is not None:
                    obj._c = None

        return self.setup_cloud_target(input_data, reset=_reset)

    def prepare_chat(self, input_data: FinOpsAdvisorInput, ctx: InvocationContext) -> str | None:
        """Chat-mode handshake — identical to the analysis path's setup."""
        # Remember the requested window so the chat message can pin the date range.
        self._chat_lookback_days = getattr(input_data, "lookback_days", 14) or 14
        return self._setup_target_account(input_data)

    def _build_chat_message(self, message: str, history: list[dict]) -> str:
        """Chat message with temporal context so tools query the CORRECT window.

        Without this, the LLM lacks today's date and queries a huge range (or
        hallucinates dates from training data). Mirrors build_message's HOJE pin.
        """
        from datetime import datetime, timedelta, timezone
        lookback = getattr(self, "_chat_lookback_days", 14)
        today = datetime.now(timezone.utc).date()
        start = today - timedelta(days=lookback)

        parts = [
            f"[CONTEXTO] HOJE é {today.isoformat()}. Para custos recentes sem período "
            f"explícito, use a janela start_date={start.isoformat()} até "
            f"end_date={today.isoformat()} (últimos {lookback} dias). Para outros "
            f"períodos, calcule a partir de HOJE — NUNCA use datas do seu conhecimento "
            f"prévio. Sempre chame explore_costs (ou a tool adequada) ANTES de citar "
            f"qualquer cifra; toda cifra deve vir do retorno da tool.",
        ]
        for turn in history[-10:]:
            role = turn.get("role", "user")
            parts.append(f"{role}: {turn.get('content', '')}")
        parts.append(f"user: {message}")
        return "\n".join(parts)

    def build_message(self, input_data: FinOpsAdvisorInput, ctx: InvocationContext) -> str:
        # ── Cross-account setup (shared handshake) ────────────────────────────
        setup_error = self._setup_target_account(input_data)
        if setup_error:
            from agents.finops_advisor.schema import FinOpsAdvisorOutput
            return FinOpsAdvisorOutput(
                tenant_id=input_data.tenant_id,
                question=input_data.question,
                error=setup_error,
            ).model_dump()

        tenant_scope = (
            f"tenant '{input_data.tenant_id}'"
            if input_data.tenant_id.upper() != "ALL"
            else "todos os tenants (use tenant_id=None para visão consolidada)"
        )
        question_block = (
            f"\nPERGUNTA ESPECÍFICA: {input_data.question}\n\n"
            "Responda essa pergunta mas não se limite a ela — se encontrar outros "
            "findings críticos no caminho, reporte também."
            if input_data.question
            else "\nModo de varredura proativa — sem pergunta específica. "
                 "Explore todas as dimensões relevantes e reporte TODOS os findings "
                 "que um especialista sênior consideraria actionable."
        )

        account_scope = (
            f"conta alvo: {input_data.target_role_arn.split(':')[4]} "
            f"(via role {input_data.target_role_arn.split('/')[-1]})"
            if input_data.target_role_arn
            else "conta da plataforma (single-account mode)"
        )

        from datetime import datetime, timedelta, timezone
        _today = datetime.now(timezone.utc).date()
        _start = _today - timedelta(days=input_data.lookback_days)

        return f"""Analise os custos do {tenant_scope} nos últimos {input_data.lookback_days} dias.
{question_block}

Contexto para sua investigação:
- HOJE é {_today.isoformat()}. Use SEMPRE esta data como referência — não use datas do seu conhecimento prévio.
- Janela de análise: start_date={_start.isoformat()} até end_date={_today.isoformat()} (use estas datas nas tools de Cost Explorer).
- NUNCA consulte datas anteriores a {(_today - timedelta(days=400)).isoformat()} (limite de 14 meses do CE).
- Conta AWS: {account_scope}
- Região padrão: {input_data.aws_region}
- tenant_id para filtros de Cost Explorer: '{input_data.tenant_id}'
- Use discover_dimension_values para saber o que existe ANTES de querying cego
- Correlacione billing com métricas CloudWatch quando encontrar spikes
- Verifique rightsizing recommendations — equipes raramente conferem isso
- Compare períodos equivalentes para separar crescimento normal de anomalias

Lembre: você está buscando o que o console AWS NÃO mostra proativamente.
Retorne APENAS JSON com schema FinOpsAdvisorOutput."""


handler = FinOpsAdvisorAgent.as_handler()
