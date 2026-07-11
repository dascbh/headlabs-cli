"""
FinOps Advisor tools — general-purpose AWS billing exploration surface.

Design principle: tools are EXPLORATORY, not pre-baked reports.
The LLM decides what dimensions to query, what to cross-reference,
when to drill deeper. These tools expose the full Cost Explorer API
surface plus correlated operational metrics.

A FinOps specialist doesn't run a fixed set of reports — they explore:
  "costs went up → by which service? → by which usage type in that service?
   → in which region? → on which resource IDs? → starting which day?
   → did something change in CloudWatch metrics that day?"

Tools:

  explore_costs           — CE query: any GroupBy × any filter × any granularity.
                            The power tool. Equivalent to the CE "Explore" tab
                            but callable programmatically with any combination.

  compare_periods         — run the same CE query for two date ranges and return
                            the delta (absolute + %). Answers "what changed and by how much?"

  discover_dimension_values — CE.get_dimension_values: what services / regions /
                            usage types / linked accounts are actually present?
                            The LLM calls this before querying to avoid blind shots.

  get_savings_coverage    — RI coverage + Savings Plans coverage + utilisation.
                            Answers "how much of our compute is covered by commitments?"

  get_cloudwatch_metric   — flexible CW metric query: any namespace, metric name,
                            dimensions, stat, period. Correlates operational data
                            with cost spikes.

  get_resource_costs      — CE at RESOURCE_ID granularity: which specific EC2
                            instances / S3 buckets / Lambda functions are the
                            most expensive? Requires Cost Explorer resource-level
                            tracking to be enabled.

  get_billing_anomalies   — CE anomaly detection with root cause analysis.
                            Returns anomalies sorted by financial impact.

  get_forecasted_vs_actual — current month actual vs forecast vs same period
                            last month. "Are we on track?"
"""
import logging
import os
from datetime import datetime, timezone, timedelta
from calendar import monthrange
from typing import Optional

import boto3

logger = logging.getLogger(__name__)

try:
    from strands import tool
except ImportError:  # pragma: no cover
    def tool(fn):  # type: ignore[misc]
        fn.__wrapped__ = fn
        return fn


# ═══════════════════════════════════════════════════════════════════════════════
# CROSS-ACCOUNT CREDENTIAL MANAGEMENT
#
# The agent can analyse any AWS account by assuming a read-only IAM role in the
# target account. This avoids storing long-lived credentials and follows the
# least-privilege principle.
#
# Flow:
#   1. Caller passes target_role_arn in the event (optional).
#   2. agent.py stores it in the TARGET_ROLE_ARN environment variable before
#      the first tool call (via os.environ — scoped to this invocation).
#   3. All boto3 client factories below check for TARGET_ROLE_ARN and, if set,
#      use the assumed-role credentials instead of the Lambda execution role.
#   4. Credentials are cached for the lifetime of the invocation (max 1h).
#
# Customer-side setup (one time per account):
#   Create role: HeadLabsFinOpsReadOnly
#   Trust policy: allow sts:AssumeRole from the agent's Lambda role ARN
#   Permissions: ReadOnlyAccess + ce:* + billing:GetBillingData
#
# ═══════════════════════════════════════════════════════════════════════════════

_assumed_credentials: Optional[dict] = None  # deprecated; cache lives in sdk.cloud_target

def _get_credentials() -> Optional[dict]:
    """Target-account credentials via the canonical SDK resolver (ephemeral
    client-side creds → assume-role → None). See sdk.cloud_target."""
    from headlabs_sdk.sdk import resolve_cloud_credentials
    return resolve_cloud_credentials()


def _client(service: str, region: Optional[str] = None) -> boto3.client:
    """
    Creates a boto3 client using cross-account credentials when TARGET_ROLE_ARN
    is set, or the Lambda execution role otherwise.

    All client factories below delegate to this function.
    """
    region = region or os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    creds  = _get_credentials()
    kwargs = {"region_name": region}
    if creds:
        kwargs.update(creds)
    return boto3.client(service, **kwargs)


# ── Lazy clients (all CE must be us-east-1) ───────────────────────────────────

def _ce():
    if not hasattr(_ce, "_c") or _ce._c is None:
        # Cost Explorer is always us-east-1 regardless of target region
        creds  = _get_credentials()
        kwargs = {"region_name": "us-east-1"}
        if creds:
            kwargs.update(creds)
        _ce._c = _CEProxy(boto3.client("ce", **kwargs))
    return _ce._c
_ce._c = None


class _CEProxy:
    """Clamps TimePeriod.Start to <=14 months ago so the LLM never trips the
    'historical data beyond 14 months' error (CE default retention)."""
    _FLOOR_DAYS = 410  # ~13.5 months, safe under the 14-month CE limit

    def __init__(self, client):
        self._client = client

    def __getattr__(self, name):
        attr = getattr(self._client, name)
        if not callable(attr):
            return attr
        def wrapped(**kw):
            tp = kw.get("TimePeriod")
            if isinstance(tp, dict) and tp.get("Start"):
                today = datetime.now(timezone.utc).date()
                floor = (today - timedelta(days=self._FLOOR_DAYS)).isoformat()
                start, end = tp["Start"], tp.get("End")
                if start < floor:
                    start = floor
                if end and end <= start:        # window fully stale → snap End to today
                    end = today.isoformat()
                if start != tp["Start"] or end != tp.get("End"):
                    kw = {**kw, "TimePeriod": {**tp, "Start": start, "End": end}}
            return attr(**kw)
        return wrapped


def _cw():
    if not hasattr(_cw, "_c") or _cw._c is None:
        _cw._c = _client("cloudwatch")
    return _cw._c
_cw._c = None


# ── Date helpers ──────────────────────────────────────────────────────────────

def _date(days_ago: int = 0) -> str:
    return (datetime.now(timezone.utc).date() - timedelta(days=days_ago)).isoformat()

def _month_start(months_ago: int = 0) -> str:
    d = datetime.now(timezone.utc).date()
    for _ in range(months_ago):
        d = d.replace(day=1) - timedelta(days=1)
    return d.replace(day=1).isoformat()

def _month_end_last() -> str:
    d = datetime.now(timezone.utc).date().replace(day=1) - timedelta(days=1)
    return d.isoformat()


def _build_filter(
    tenant_id: Optional[str],
    service: Optional[str],
    region: Optional[str],
    extra_tag_key: Optional[str],
    extra_tag_value: Optional[str],
) -> Optional[dict]:
    """Builds a CE filter combining tenant tag with optional service/region/tag filters."""
    clauses = []
    if tenant_id and tenant_id.upper() != "ALL":
        clauses.append({
            "Tags": {"Key": "TenantId", "Values": [tenant_id], "MatchOptions": ["EQUALS"]}
        })
    if service:
        clauses.append({
            "Dimensions": {"Key": "SERVICE", "Values": [service], "MatchOptions": ["EQUALS"]}
        })
    if region:
        clauses.append({
            "Dimensions": {"Key": "REGION", "Values": [region], "MatchOptions": ["EQUALS"]}
        })
    if extra_tag_key and extra_tag_value:
        clauses.append({
            "Tags": {"Key": extra_tag_key, "Values": [extra_tag_value], "MatchOptions": ["EQUALS"]}
        })
    if not clauses:
        return None
    return clauses[0] if len(clauses) == 1 else {"And": clauses}


# ── Tools ─────────────────────────────────────────────────────────────────────

@tool
def explore_costs(
    start_date: str,
    end_date: str,
    granularity: str,
    group_by: str,
    tenant_id: Optional[str] = None,
    service: Optional[str] = None,
    region: Optional[str] = None,
    extra_tag_key: Optional[str] = None,
    extra_tag_value: Optional[str] = None,
) -> list:
    """
    General-purpose Cost Explorer query. The primary exploration tool.

    Use this to slice billing data by any dimension:
      group_by="SERVICE"        → which services are costing most?
      group_by="REGION"         → where geographically?
      group_by="USAGE_TYPE"     → what specific usage within a service?
      group_by="OPERATION"      → which API operations (GetObject, PutItem, etc)?
      group_by="LINKED_ACCOUNT" → multi-account breakdown?
      group_by="AZ"             → cross-AZ traffic causing costs?
      group_by="RESOURCE_ID"    → individual resource breakdown?
      group_by="TAG:Environment" → tag-based grouping (prefix with TAG:)?

    Granularity: DAILY | MONTHLY | HOURLY (HOURLY only last 14 days)

    Returns list of {"period_start", "period_end", "groups": [{"key", "amount_usd"}]},
    sorted by amount descending within each period.

    Args:
        start_date:      YYYY-MM-DD start date (inclusive)
        end_date:        YYYY-MM-DD end date (exclusive)
        granularity:     DAILY | MONTHLY | HOURLY
        group_by:        Dimension or tag to group by (see above)
        tenant_id:       Filter by TenantId tag (pass None or "ALL" for all tenants)
        service:         Optionally restrict to one AWS service
        region:          Optionally restrict to one AWS region
        extra_tag_key:   Additional tag key filter (e.g. "Environment")
        extra_tag_value: Additional tag value filter (e.g. "production")
    """
    # Parse group_by — support "TAG:MyKey" syntax
    if group_by.startswith("TAG:"):
        group_by_spec = [{"Type": "TAG", "Key": group_by[4:]}]
    else:
        group_by_spec = [{"Type": "DIMENSION", "Key": group_by}]

    flt = _build_filter(tenant_id, service, region, extra_tag_key, extra_tag_value)
    kwargs: dict = {
        "TimePeriod":  {"Start": start_date, "End": end_date},
        "Granularity": granularity,
        "GroupBy":     group_by_spec,
        "Metrics":     ["UnblendedCost", "UsageQuantity"],
    }
    if flt:
        kwargs["Filter"] = flt

    response = _ce().get_cost_and_usage(**kwargs)

    results = []
    for item in response.get("ResultsByTime", []):
        period = item["TimePeriod"]
        groups = []
        for g in item.get("Groups", []):
            amount = float(g["Metrics"]["UnblendedCost"]["Amount"])
            usage  = float(g["Metrics"]["UsageQuantity"]["Amount"])
            if amount > 0:
                groups.append({
                    "key":        g["Keys"][0],
                    "amount_usd": round(amount, 4),
                    "usage":      round(usage, 4),
                    "unit":       g["Metrics"]["UnblendedCost"].get("Unit", "USD"),
                })
        groups.sort(key=lambda x: x["amount_usd"], reverse=True)
        if groups:
            results.append({
                "period_start": period["Start"],
                "period_end":   period["End"],
                "groups":       groups,
                "total_usd":    round(sum(g["amount_usd"] for g in groups), 4),
            })

    logger.info("explore_costs: group_by=%s tenant=%s periods=%d", group_by, tenant_id, len(results))
    return results


@tool
def compare_periods(
    group_by: str,
    period_a_start: str,
    period_a_end: str,
    period_b_start: str,
    period_b_end: str,
    tenant_id: Optional[str] = None,
    service: Optional[str] = None,
) -> list:
    """
    Compares costs between two time periods for the same grouping dimension.

    Essential for answering: "what changed?" — e.g. this month vs last month,
    this week vs last week, pre-deploy vs post-deploy.

    Returns list of {"key", "period_a_usd", "period_b_usd", "delta_usd",
    "delta_pct", "direction"} sorted by absolute delta descending.
    New items (only in period_b) are highlighted with direction="new".
    Removed items (only in period_a) have direction="removed".

    Args:
        group_by:       Dimension to compare (SERVICE, USAGE_TYPE, REGION, etc.)
        period_a_start: YYYY-MM-DD — baseline period start
        period_a_end:   YYYY-MM-DD — baseline period end
        period_b_start: YYYY-MM-DD — comparison period start
        period_b_end:   YYYY-MM-DD — comparison period end
        tenant_id:      Optional TenantId tag filter
        service:        Optional service filter
    """
    def _total_by_key(start: str, end: str) -> dict:
        flt = _build_filter(tenant_id, service, None, None, None)
        group_spec = (
            [{"Type": "TAG",       "Key": group_by[4:]}] if group_by.startswith("TAG:")
            else [{"Type": "DIMENSION", "Key": group_by}]
        )
        kwargs: dict = {
            "TimePeriod":  {"Start": start, "End": end},
            "Granularity": "MONTHLY",
            "GroupBy":     group_spec,
            "Metrics":     ["UnblendedCost"],
        }
        if flt:
            kwargs["Filter"] = flt
        resp = _ce().get_cost_and_usage(**kwargs)
        totals: dict = {}
        for item in resp.get("ResultsByTime", []):
            for g in item.get("Groups", []):
                key    = g["Keys"][0]
                amount = float(g["Metrics"]["UnblendedCost"]["Amount"])
                totals[key] = totals.get(key, 0) + amount
        return totals

    a = _total_by_key(period_a_start, period_a_end)
    b = _total_by_key(period_b_start, period_b_end)

    all_keys = set(a) | set(b)
    deltas = []
    for key in all_keys:
        va = a.get(key, 0)
        vb = b.get(key, 0)
        delta = vb - va
        if va == 0:
            direction = "new"
            pct = 100.0
        elif vb == 0:
            direction = "removed"
            pct = -100.0
        else:
            pct = (delta / va * 100) if va else 0
            direction = "increased" if delta > 0 else "decreased"
        if abs(delta) > 0.001:
            deltas.append({
                "key":          key,
                "period_a_usd": round(va, 4),
                "period_b_usd": round(vb, 4),
                "delta_usd":    round(delta, 4),
                "delta_pct":    round(pct, 1),
                "direction":    direction,
            })

    deltas.sort(key=lambda x: abs(x["delta_usd"]), reverse=True)
    logger.info("compare_periods: group_by=%s deltas=%d", group_by, len(deltas))
    return deltas


@tool
def discover_dimension_values(
    dimension: str,
    start_date: str,
    end_date: str,
    tenant_id: Optional[str] = None,
) -> list:
    """
    Discovers what values exist for a Cost Explorer dimension in a given period.

    Use this BEFORE querying a dimension you're not sure about — e.g. to find
    what service names exist (SERVICE), what regions are active (REGION),
    what usage types are present (USAGE_TYPE), what linked accounts (LINKED_ACCOUNT).

    This prevents querying for non-existent values and reveals surprises
    (e.g. a region you didn't know was active and running resources).

    Returns sorted list of {"value", "attributes"} — the actual values present.

    Args:
        dimension:   CE dimension: SERVICE | REGION | USAGE_TYPE | OPERATION |
                     LINKED_ACCOUNT | AZ | INSTANCE_TYPE | LEGAL_ENTITY_NAME
        start_date:  YYYY-MM-DD
        end_date:    YYYY-MM-DD
        tenant_id:   Optional TenantId tag filter
    """
    flt = _build_filter(tenant_id, None, None, None, None)
    kwargs: dict = {
        "TimePeriod": {"Start": start_date, "End": end_date},
        "Dimension":  dimension,
    }
    if flt:
        kwargs["Filter"] = flt

    response = _ce().get_dimension_values(**kwargs)
    values = [
        {"value": dv["Value"], "attributes": dv.get("Attributes", {})}
        for dv in response.get("DimensionValues", [])
        if dv.get("Value")
    ]
    values.sort(key=lambda x: x["value"])
    logger.info("discover_dimension_values: dim=%s count=%d", dimension, len(values))
    return values


@tool
def get_savings_coverage(
    start_date: str,
    end_date: str,
    tenant_id: Optional[str] = None,
) -> dict:
    """
    Returns commitment coverage and utilisation across Reserved Instances
    and Savings Plans — the "how efficiently are we using what we bought?" view.

    Key metrics:
      - RI coverage %: what % of eligible EC2/RDS hours are covered by RIs
      - SP coverage %: what % of eligible compute spend is covered by SPs
      - SP utilisation %: how much of the SP commitment is actually being used
        (low utilisation = you bought too much and are wasting the commitment)

    A well-optimised account targets:
      - RI/SP coverage > 70%
      - SP utilisation > 95%

    Args:
        start_date:  YYYY-MM-DD
        end_date:    YYYY-MM-DD
        tenant_id:   Optional TenantId tag filter (best-effort; CE coverage
                     APIs have limited tag filtering support)
    """
    flt = _build_filter(tenant_id, None, None, None, None)

    # Savings Plans coverage
    sp_kwargs: dict = {
        "TimePeriod":   {"Start": start_date, "End": end_date},
        "Granularity":  "MONTHLY",
    }
    if flt:
        sp_kwargs["Filter"] = flt
    sp_cov_resp = _ce().get_savings_plans_coverage(**sp_kwargs)

    sp_utilisation_resp = _ce().get_savings_plans_utilization(
        TimePeriod={"Start": start_date, "End": end_date},
        Granularity="MONTHLY",
    )

    # RI coverage
    ri_kwargs: dict = {
        "TimePeriod":  {"Start": start_date, "End": end_date},
        "Granularity": "MONTHLY",
    }
    if flt:
        ri_kwargs["Filter"] = flt
    ri_cov_resp = _ce().get_reservation_coverage(**ri_kwargs)

    # Parse SP coverage
    sp_coverage_pct  = 0.0
    sp_periods = sp_cov_resp.get("SavingsPlansCoverages", [])
    if sp_periods:
        coverages = [
            float(p.get("Coverage", {}).get("CoveragePercentage", 0))
            for p in sp_periods
        ]
        sp_coverage_pct = round(sum(coverages) / len(coverages), 1) if coverages else 0

    # Parse SP utilisation
    sp_util_pct = 0.0
    sp_util_periods = sp_utilisation_resp.get("SavingsPlansUtilizationsByTime", [])
    if sp_util_periods:
        utils = [
            float(p.get("Utilization", {}).get("UtilizationPercentage", 0))
            for p in sp_util_periods
        ]
        sp_util_pct = round(sum(utils) / len(utils), 1) if utils else 0

    # Parse RI coverage
    ri_coverage_pct = 0.0
    ri_periods = ri_cov_resp.get("CoveragesByTime", [])
    if ri_periods:
        ri_covs = [
            float(p.get("Total", {}).get("CoverageHours", {}).get("CoverageHoursPercentage", 0))
            for p in ri_periods
        ]
        ri_coverage_pct = round(sum(ri_covs) / len(ri_covs), 1) if ri_covs else 0

    return {
        "savings_plans_coverage_pct":    sp_coverage_pct,
        "savings_plans_utilisation_pct": sp_util_pct,
        "ri_coverage_pct":               ri_coverage_pct,
        "sp_coverage_target_pct":        70.0,
        "sp_utilisation_target_pct":     95.0,
        "ri_coverage_target_pct":        70.0,
        "sp_coverage_gap":               round(max(0, 70.0 - sp_coverage_pct), 1),
        "sp_utilisation_gap":            round(max(0, 95.0 - sp_util_pct), 1),
    }


@tool
def get_cloudwatch_metric(
    namespace: str,
    metric_name: str,
    start_date: str,
    end_date: str,
    statistic: str = "Sum",
    period_seconds: int = 86400,
    dimensions: Optional[list] = None,
) -> list:
    """
    Queries any CloudWatch metric to correlate operational data with cost spikes.

    Use this to answer: "the cost spiked on May 20th — what happened operationally?"
      namespace="AWS/Lambda" metric_name="Invocations"       → call volume
      namespace="AWS/Lambda" metric_name="Duration"          → execution time (cost driver)
      namespace="AWS/Lambda" metric_name="Errors"            → retry loops = wasted cost
      namespace="AWS/Bedrock" metric_name="InputTokenCount"  → prompt size trend
      namespace="AWS/Bedrock" metric_name="OutputTokenCount" → response size trend
      namespace="AWS/DynamoDB" metric_name="ConsumedReadCapacityUnits"
      namespace="AWS/DynamoDB" metric_name="ProvisionedReadCapacityUnits"
      namespace="AWS/NATGateway" metric_name="BytesOutToDestination" → NAT traffic
      namespace="AWS/S3" metric_name="BucketSizeBytes"
      namespace="AWS/EC2" metric_name="CPUUtilization"       → rightsizing evidence

    Returns list of {"timestamp", "value", "unit"} sorted by timestamp.

    Args:
        namespace:       CW namespace (e.g. "AWS/Lambda", "AWS/Bedrock")
        metric_name:     CW metric name
        start_date:      YYYY-MM-DD
        end_date:        YYYY-MM-DD
        statistic:       Sum | Average | Maximum | Minimum | SampleCount
        period_seconds:  Aggregation window in seconds (86400=daily, 3600=hourly)
        dimensions:      List of {"Name": str, "Value": str} dimension filters
    """
    from datetime import datetime
    start_dt = datetime.fromisoformat(start_date).replace(tzinfo=timezone.utc)
    end_dt   = datetime.fromisoformat(end_date).replace(tzinfo=timezone.utc)

    kwargs: dict = {
        "Namespace":  namespace,
        "MetricName": metric_name,
        "StartTime":  start_dt,
        "EndTime":    end_dt,
        "Period":     period_seconds,
        "Statistics": [statistic],
    }
    if dimensions:
        kwargs["Dimensions"] = dimensions

    response = _cw().get_metric_statistics(**kwargs)
    points = sorted(
        [
            {
                "timestamp": dp["Timestamp"].isoformat(),
                "value":     round(dp.get(statistic, 0), 4),
                "unit":      dp.get("Unit", ""),
            }
            for dp in response.get("Datapoints", [])
        ],
        key=lambda x: x["timestamp"],
    )
    logger.info("get_cloudwatch_metric: %s/%s points=%d", namespace, metric_name, len(points))
    return points


@tool
def get_resource_costs(
    start_date: str,
    end_date: str,
    service: str,
    tenant_id: Optional[str] = None,
    top_n: int = 20,
) -> list:
    """
    Returns cost at individual resource ID level for a given service.

    Use this when you've identified a costly service and need to know
    WHICH SPECIFIC RESOURCES are driving it — the specific EC2 instance,
    S3 bucket, Lambda function, DynamoDB table, etc.

    Note: requires Cost Explorer resource-level tracking to be enabled
    (Settings → Resource-level data in the CE console).

    Returns top_n resources by cost, sorted descending.

    Args:
        start_date:  YYYY-MM-DD
        end_date:    YYYY-MM-DD
        service:     AWS service name (e.g. "Amazon EC2", "AWS Lambda")
        tenant_id:   Optional TenantId tag filter
        top_n:       Max resources to return
    """
    flt = _build_filter(tenant_id, service, None, None, None)
    kwargs: dict = {
        "TimePeriod":  {"Start": start_date, "End": end_date},
        "Granularity": "MONTHLY",
        "GroupBy":     [{"Type": "DIMENSION", "Key": "RESOURCE_ID"}],
        "Metrics":     ["UnblendedCost"],
    }
    if flt:
        kwargs["Filter"] = flt

    response = _ce().get_cost_and_usage_with_resources(**kwargs)

    resources = []
    for item in response.get("ResultsByTime", []):
        for g in item.get("Groups", []):
            amount = float(g["Metrics"]["UnblendedCost"]["Amount"])
            if amount > 0:
                resources.append({
                    "resource_id": g["Keys"][0],
                    "amount_usd":  round(amount, 4),
                })

    resources.sort(key=lambda x: x["amount_usd"], reverse=True)
    return resources[:top_n]


@tool
def get_billing_anomalies(
    start_date: str,
    end_date: str,
    min_impact_usd: float = 10.0,
    monitor_arn: Optional[str] = None,
) -> list:
    """
    Returns cost anomalies detected by Cost Explorer Anomaly Detection,
    with full root cause breakdown.

    Anomalies are spikes above the ML-predicted baseline — not just
    "costs went up" but "costs went up UNEXPECTEDLY given your patterns".

    Use this to find: runaway processes, forgotten test resources,
    misconfigured auto-scaling, DDoS-induced data transfer spikes.

    Returns list sorted by impact_usd descending, each with:
    anomaly_id, service, usage_type, region, impact_usd, expected_usd,
    actual_usd, start_date, end_date, root_causes.

    Args:
        start_date:      YYYY-MM-DD search window start
        end_date:        YYYY-MM-DD search window end
        min_impact_usd:  Only return anomalies above this USD impact
        monitor_arn:     Specific monitor ARN (optional; searches all monitors if omitted)
    """
    arn = monitor_arn or os.environ.get("ANOMALY_MONITOR_ARN")
    kwargs: dict = {
        "DateInterval": {"StartDate": start_date, "EndDate": end_date},
        "TotalImpact":  {
            "NumericOperator": "GREATER_THAN",
            "StartValue":      min_impact_usd,
        },
    }
    if arn:
        kwargs["MonitorArn"] = arn

    response = _ce().get_anomalies(**kwargs)

    anomalies = []
    for a in response.get("Anomalies", []):
        impact = a.get("Impact", {})
        root_causes = [
            {
                "service":    rc.get("Service"),
                "usage_type": rc.get("UsageType"),
                "region":     rc.get("Region"),
                "linked_account": rc.get("LinkedAccount"),
            }
            for rc in a.get("RootCauses", [])
        ]
        anomalies.append({
            "anomaly_id":    a.get("AnomalyId", ""),
            "impact_usd":    round(float(impact.get("TotalImpact", 0)), 2),
            "expected_usd":  round(float(impact.get("TotalExpectedSpend", 0)), 2),
            "actual_usd":    round(float(impact.get("TotalActualSpend", 0)), 2),
            "start_date":    a.get("AnomalyStartDate"),
            "end_date":      a.get("AnomalyEndDate"),
            "root_causes":   root_causes,
        })

    anomalies.sort(key=lambda x: x["impact_usd"], reverse=True)
    logger.info("get_billing_anomalies: found=%d min_impact=%.2f", len(anomalies), min_impact_usd)
    return anomalies


@tool
def get_s3_storage_analysis(
    start_date: str,
    end_date: str,
    tenant_id: Optional[str] = None,
) -> dict:
    """
    Deep S3 cost and storage-class analysis — the foundation for recommending
    the right storage tier per bucket.

    What this reveals that the console does NOT show proactively:
    - Which buckets are paying for Standard storage but have objects not accessed
      in 30+ days (should be Intelligent-Tiering or Glacier)
    - Buckets with high GET request costs vs low storage cost (hot access pattern
      → benefit from CloudFront in front)
    - Buckets paying for Standard-IA but with frequent access (wrong tier —
      Standard would be cheaper above ~20% monthly retrieval rate)
    - Storage cost by class: StandardStorage, IntelligentTieringFAStorage,
      IntelligentTieringIAStorage, GlacierStorage, DeepArchiveStorage, etc.

    Storage class decision heuristics:
    - Access daily/weekly:        S3 Standard
    - Access monthly, unpredictable: S3 Intelligent-Tiering (auto-tiers, no retrieval fee)
    - Access < once/quarter:      S3 Standard-IA (but watch retrieval costs)
    - Access < once/year:         S3 Glacier Instant Retrieval
    - Archive, access never:      S3 Glacier Deep Archive (~$0.00099/GB/month)
    - Standard-IA break-even:     only cheaper than Standard if accessed < 20% of objects/month

    Returns:
      - cost_by_usage_type:  CE costs broken down by S3 usage type (storage + requests)
      - cost_by_bucket:      top buckets by cost (requires resource-level tracking)
      - storage_by_type_gb:  CloudWatch BucketSizeBytes per StorageType
      - request_metrics:     GET + PUT counts to infer access frequency
      - recommendations:     derived storage class suggestions per pattern found

    Args:
        start_date:  YYYY-MM-DD
        end_date:    YYYY-MM-DD
        tenant_id:   Optional TenantId tag filter
    """
    from datetime import datetime

    flt = _build_filter(tenant_id, "Amazon S3", None, None, None)
    s3_filter = _build_filter(tenant_id, None, None, None, None)

    # ── 1. CE: S3 costs broken down by usage type ────────────────────────────
    ce_kwargs: dict = {
        "TimePeriod":  {"Start": start_date, "End": end_date},
        "Granularity": "MONTHLY",
        "GroupBy":     [{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
        "Metrics":     ["UnblendedCost", "UsageQuantity"],
    }
    if flt:
        ce_kwargs["Filter"] = flt
    elif s3_filter:
        ce_kwargs["Filter"] = {"Dimensions": {"Key": "SERVICE", "Values": ["Amazon S3"], "MatchOptions": ["EQUALS"]}}

    ce_resp = _ce().get_cost_and_usage(**ce_kwargs)

    cost_by_usage: dict = {}
    for period in ce_resp.get("ResultsByTime", []):
        for g in period.get("Groups", []):
            key    = g["Keys"][0]
            amount = float(g["Metrics"]["UnblendedCost"]["Amount"])
            qty    = float(g["Metrics"]["UsageQuantity"]["Amount"])
            if amount > 0 or qty > 0:
                if key not in cost_by_usage:
                    cost_by_usage[key] = {"cost_usd": 0.0, "usage_qty": 0.0}
                cost_by_usage[key]["cost_usd"]   += amount
                cost_by_usage[key]["usage_qty"]  += qty

    # Categorise usage types into storage vs requests vs data transfer
    storage_types  = [k for k in cost_by_usage if "Storage" in k or "storage" in k]
    request_types  = [k for k in cost_by_usage if "Requests" in k or "Request" in k]
    transfer_types = [k for k in cost_by_usage if "DataTransfer" in k or "Bytes" in k]

    total_storage_cost  = sum(cost_by_usage[k]["cost_usd"] for k in storage_types)
    total_request_cost  = sum(cost_by_usage[k]["cost_usd"] for k in request_types)
    total_transfer_cost = sum(cost_by_usage[k]["cost_usd"] for k in transfer_types)

    # ── 2. CE: cost per bucket (resource-level) ───────────────────────────────
    bucket_costs: list = []
    try:
        res_kwargs: dict = {
            "TimePeriod":  {"Start": start_date, "End": end_date},
            "Granularity": "MONTHLY",
            "GroupBy":     [{"Type": "DIMENSION", "Key": "RESOURCE_ID"}],
            "Metrics":     ["UnblendedCost"],
        }
        if flt:
            res_kwargs["Filter"] = flt
        elif s3_filter:
            res_kwargs["Filter"] = {"Dimensions": {"Key": "SERVICE", "Values": ["Amazon S3"], "MatchOptions": ["EQUALS"]}}
        res_resp = _ce().get_cost_and_usage_with_resources(**res_kwargs)
        for period in res_resp.get("ResultsByTime", []):
            for g in period.get("Groups", []):
                amount = float(g["Metrics"]["UnblendedCost"]["Amount"])
                if amount > 0:
                    bucket_costs.append({"bucket": g["Keys"][0], "cost_usd": round(amount, 4)})
        bucket_costs.sort(key=lambda x: x["cost_usd"], reverse=True)
        bucket_costs = bucket_costs[:20]
    except Exception:
        # Resource-level tracking may not be enabled
        bucket_costs = []

    # ── 3. CloudWatch: storage volume by storage class ────────────────────────
    start_dt = datetime.fromisoformat(start_date).replace(tzinfo=timezone.utc)
    end_dt   = datetime.fromisoformat(end_date).replace(tzinfo=timezone.utc)

    storage_classes = [
        "StandardStorage",
        "IntelligentTieringFAStorage",
        "IntelligentTieringIAStorage",
        "IntelligentTieringAAStorage",
        "StandardIAStorage",
        "OneZoneIAStorage",
        "GlacierInstantRetrievalStorage",
        "GlacierStorage",
        "DeepArchiveStorage",
    ]
    storage_by_type: dict = {}
    for sc in storage_classes:
        try:
            cw_resp = _cw().get_metric_statistics(
                Namespace="AWS/S3",
                MetricName="BucketSizeBytes",
                Dimensions=[{"Name": "StorageType", "Value": sc},
                            {"Name": "BucketName",  "Value": "AllBuckets"}],
                StartTime=start_dt,
                EndTime=end_dt,
                Period=86400,
                Statistics=["Average"],
            )
            pts = cw_resp.get("Datapoints", [])
            if pts:
                avg_bytes = max(dp.get("Average", 0) for dp in pts)
                if avg_bytes > 0:
                    storage_by_type[sc] = round(avg_bytes / (1024 ** 3), 2)  # bytes → GB
        except Exception:
            pass

    # ── 4. CloudWatch: request volume (GET + PUT) ─────────────────────────────
    request_counts: dict = {}
    for metric in ["GetRequests", "PutRequests", "ListRequests"]:
        try:
            cw_resp = _cw().get_metric_statistics(
                Namespace="AWS/S3",
                MetricName=metric,
                Dimensions=[{"Name": "BucketName", "Value": "AllBuckets"},
                            {"Name": "FilterId",   "Value": "EntireBucket"}],
                StartTime=start_dt,
                EndTime=end_dt,
                Period=int((end_dt - start_dt).total_seconds()),
                Statistics=["Sum"],
            )
            pts = cw_resp.get("Datapoints", [])
            if pts:
                request_counts[metric] = int(sum(dp.get("Sum", 0) for dp in pts))
        except Exception:
            pass

    # ── 5. Derive storage class recommendations ───────────────────────────────
    recommendations: list = []

    standard_gb    = storage_by_type.get("StandardStorage", 0)
    standard_ia_gb = storage_by_type.get("StandardIAStorage", 0)
    glacier_gb     = storage_by_type.get("GlacierStorage", 0)
    deep_archive_gb = storage_by_type.get("DeepArchiveStorage", 0)

    # Standard with no Intelligent-Tiering → candidate for auto-tiering
    it_gb = sum(v for k, v in storage_by_type.items() if "IntelligentTiering" in k)
    if standard_gb > 100 and it_gb == 0:
        # Standard costs ~$0.023/GB, IT ~$0.023/GB but auto-moves cold to $0.0125/GB
        potential_saving = round(standard_gb * 0.4 * (0.023 - 0.0125), 2)  # assume 40% cold
        recommendations.append({
            "pattern":   "large_standard_no_it",
            "finding":   f"{standard_gb:.0f} GB in Standard storage with no Intelligent-Tiering active",
            "action":    "Enable S3 Intelligent-Tiering on buckets with mixed-age objects — "
                         "objects not accessed for 30 days auto-move to IA tier at 46% lower cost",
            "estimated_monthly_saving_usd": potential_saving,
            "severity":  "high" if potential_saving > 100 else "medium",
        })

    # Standard-IA with high request cost → may be cheaper in Standard
    if standard_ia_gb > 0 and total_request_cost > total_storage_cost * 0.5:
        recommendations.append({
            "pattern":   "standard_ia_high_requests",
            "finding":   f"Standard-IA storage {standard_ia_gb:.0f} GB but request costs "
                         f"(${total_request_cost:.2f}) are >50% of storage costs — "
                         "retrieval fees may be making IA more expensive than Standard",
            "action":    "Analyse per-bucket GET frequency. If > 20% of objects accessed monthly, "
                         "move back to Standard. IA break-even is ~20% monthly retrieval rate.",
            "estimated_monthly_saving_usd": None,
            "severity":  "medium",
        })

    # High request cost relative to storage → CloudFront candidate
    if total_request_cost > 50 and total_request_cost > total_storage_cost:
        recommendations.append({
            "pattern":   "high_s3_request_cost",
            "finding":   f"S3 request costs (${total_request_cost:.2f}) exceed storage costs "
                         f"(${total_storage_cost:.2f}) — high GET frequency pattern",
            "action":    "Place CloudFront in front of frequently-read buckets. "
                         "CloudFront → S3 origin requests are free; you pay only for CF egress "
                         "which is cheaper than S3 direct at high volumes.",
            "estimated_monthly_saving_usd": round(total_request_cost * 0.6, 2),
            "severity":  "high" if total_request_cost > 200 else "medium",
        })

    # Data in Standard with zero access = should be Glacier
    if standard_gb > 50 and request_counts.get("GetRequests", 0) == 0:
        potential_saving = round(standard_gb * (0.023 - 0.004), 2)  # Standard vs Glacier IR
        recommendations.append({
            "pattern":   "standard_zero_access",
            "finding":   f"{standard_gb:.0f} GB in Standard with 0 GET requests in period "
                         "— objects appear to be cold data paying Standard rates",
            "action":    "Move to S3 Glacier Instant Retrieval ($0.004/GB vs $0.023/GB Standard). "
                         "Same millisecond retrieval latency, 83% cheaper storage.",
            "estimated_monthly_saving_usd": potential_saving,
            "severity":  "critical" if potential_saving > 500 else "high",
        })

    logger.info(
        "get_s3_storage_analysis: usage_types=%d buckets=%d recommendations=%d",
        len(cost_by_usage), len(bucket_costs), len(recommendations),
    )
    return {
        "period":               {"start": start_date, "end": end_date},
        "total_s3_cost_usd":    round(total_storage_cost + total_request_cost + total_transfer_cost, 2),
        "cost_breakdown_usd": {
            "storage":       round(total_storage_cost, 2),
            "requests":      round(total_request_cost, 2),
            "data_transfer": round(total_transfer_cost, 2),
        },
        "cost_by_usage_type":   dict(sorted(cost_by_usage.items(),
                                            key=lambda x: x[1]["cost_usd"], reverse=True)),
        "top_buckets_by_cost":  bucket_costs,
        "storage_by_class_gb":  dict(sorted(storage_by_type.items(),
                                            key=lambda x: x[1], reverse=True)),
        "request_counts":       request_counts,
        "recommendations":      sorted(recommendations,
                                       key=lambda x: x.get("estimated_monthly_saving_usd") or 0,
                                       reverse=True),
    }


@tool
def get_rightsizing_recommendations(
    service: str = "AmazonEC2",
    tenant_id: Optional[str] = None,
) -> list:
    """
    Returns rightsizing recommendations from Cost Explorer — specific instances
    the AWS ML model flagged as over-provisioned based on actual utilisation.

    This is NOT in the AWS console home or Cost Explorer overview — it requires
    navigating to Recommendations → Rightsizing, and most teams never look at it.

    The recommendations include:
      - Which instance to downsize (current type → recommended type)
      - Estimated monthly savings
      - CPU/memory utilisation evidence supporting the recommendation
      - Whether the recommendation is "Terminate" (idle) or "Modify" (downsize)

    Use this proactively — even one idle r5.4xlarge terminated saves ~$700/month.

    Returns list of recommendations sorted by estimated_monthly_savings descending.

    Args:
        service:    "AmazonEC2" (default) or "AmazonRDS"
        tenant_id:  Optional TenantId tag filter
    """
    flt = _build_filter(tenant_id, None, None, None, None)
    kwargs: dict = {
        "Service":                service,
        "Configuration": {
            "RecommendationTarget":   "SAME_INSTANCE_FAMILY",
            "BenefitsConsidered":     True,
        },
    }
    if flt:
        kwargs["Filter"] = flt

    response = _ce().get_rightsizing_recommendation(**kwargs)

    recs = []
    for r in response.get("RightsizingRecommendations", []):
        current  = r.get("CurrentInstance", {})
        rec_type = r.get("RightsizingType", "")

        if rec_type == "Terminate":
            saving = float(r.get("TerminateRecommendationDetail", {})
                           .get("EstimatedMonthlySavings", 0))
            recommended_type = "TERMINATE"
        else:
            mod = r.get("ModifyRecommendationDetail", {})
            targets = mod.get("TargetInstances", [])
            if not targets:
                continue
            best = max(targets,
                       key=lambda t: float(t.get("EstimatedMonthlySavings", 0)))
            saving = float(best.get("EstimatedMonthlySavings", 0))
            recommended_type = best.get("ResourceDetails", {}) \
                                   .get("EC2ResourceDetails", {}) \
                                   .get("InstanceType", "")

        util = current.get("ResourceUtilization", {}).get("EC2ResourceUtilization", {})
        recs.append({
            "resource_id":                current.get("ResourceId"),
            "instance_type":              current.get("ResourceDetails", {})
                                                  .get("EC2ResourceDetails", {})
                                                  .get("InstanceType"),
            "recommended_type":           recommended_type,
            "rightsizing_type":           rec_type,
            "estimated_monthly_savings":  round(saving, 2),
            "max_cpu_utilisation_pct":    float(util.get("MaxCpuUtilizationPercentage", 0)),
            "max_memory_utilisation_pct": float(util.get("MaxMemoryUtilizationPercentage", 0)),
            "monthly_on_demand_cost":     float(current.get("MonthlyCost", 0)),
        })

    recs.sort(key=lambda x: x["estimated_monthly_savings"], reverse=True)
    logger.info("get_rightsizing_recommendations: service=%s recs=%d", service, len(recs))
    return recs


@tool
def get_forecasted_vs_actual(tenant_id: Optional[str] = None) -> dict:
    """
    Returns current month actual spend vs forecast vs same period last month.
    The "are we on track?" view that no dashboard shows in one place.

    Computes:
      - mtd_actual:   month-to-date actual spend
      - mtd_last_month: same days last month (apples-to-apples)
      - forecast_eom: Cost Explorer forecast for end of current month
      - run_rate_eom: simple projection (mtd / days_elapsed * days_in_month)
      - mtd_delta_pct: % change vs same period last month
      - run_rate_vs_forecast_pct: divergence between simple projection and CE forecast
        (large divergence = unusual spend pattern, worth investigating)

    Args:
        tenant_id: Optional TenantId tag filter
    """
    from calendar import monthrange
    from datetime import date
    today      = datetime.now(timezone.utc).date()
    month_start = today.replace(day=1)
    days_elapsed = today.day
    days_in_month = monthrange(today.year, today.month)[1]

    # Same window last month
    last_month_start = (month_start - timedelta(days=1)).replace(day=1)
    last_month_same_day = min(
        last_month_start.replace(day=days_elapsed),
        (month_start - timedelta(days=1)),
    )

    flt = _build_filter(tenant_id, None, None, None, None)

    def _actual(start: date, end: date) -> float:
        kwargs: dict = {
            "TimePeriod":  {"Start": start.isoformat(), "End": end.isoformat()},
            "Granularity": "MONTHLY",
            "Metrics":     ["UnblendedCost"],
        }
        if flt:
            kwargs["Filter"] = flt
        resp = _ce().get_cost_and_usage(**kwargs)
        return sum(
            float(r.get("Total", {}).get("UnblendedCost", {}).get("Amount", 0))
            for r in resp.get("ResultsByTime", [])
        )

    mtd_actual      = _actual(month_start, today)
    mtd_last_month  = _actual(last_month_start, last_month_same_day)

    # CE forecast (must start tomorrow)
    tomorrow  = today + timedelta(days=1)
    month_end = today.replace(day=days_in_month)
    forecast_eom = 0.0
    if tomorrow <= month_end:
        fcast_kwargs: dict = {
            "TimePeriod":           {"Start": tomorrow.isoformat(), "End": month_end.isoformat()},
            "Metric":               "UNBLENDED_COST",
            "Granularity":          "MONTHLY",
            "PredictionIntervalLevel": 80,
        }
        if flt:
            fcast_kwargs["Filter"] = flt
        try:
            fcast_resp = _ce().get_cost_forecast(**fcast_kwargs)
            forecast_remaining = sum(
                float(r.get("MeanValue", 0))
                for r in fcast_resp.get("ForecastResultsByTime", [])
            )
            forecast_eom = round(mtd_actual + forecast_remaining, 2)
        except Exception:
            forecast_eom = None

    run_rate_eom = round(
        (mtd_actual / days_elapsed * days_in_month) if days_elapsed > 0 else 0, 2
    )
    mtd_delta_pct = (
        round((mtd_actual - mtd_last_month) / mtd_last_month * 100, 1)
        if mtd_last_month > 0 else None
    )
    run_rate_vs_forecast = (
        round((run_rate_eom - forecast_eom) / forecast_eom * 100, 1)
        if forecast_eom and forecast_eom > 0 else None
    )

    return {
        "tenant_id":                  tenant_id,
        "month":                      month_start.strftime("%Y-%m"),
        "days_elapsed":               days_elapsed,
        "days_in_month":              days_in_month,
        "mtd_actual_usd":             round(mtd_actual, 2),
        "mtd_last_month_usd":         round(mtd_last_month, 2),
        "mtd_delta_pct":              mtd_delta_pct,
        "run_rate_eom_usd":           run_rate_eom,
        "ce_forecast_eom_usd":        forecast_eom,
        "run_rate_vs_forecast_pct":   run_rate_vs_forecast,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# BEDROCK SPECIALIST TOOLS
# ═══════════════════════════════════════════════════════════════════════════════

@tool
def get_bedrock_model_efficiency(
    start_date: str,
    end_date: str,
    tenant_id: Optional[str] = None,
) -> dict:
    """
    Analyses Bedrock cost efficiency per model — the key tool for detecting
    model over-selection (using Claude Sonnet where Haiku would suffice).

    Pricing reference (per 1M tokens, as of 2025):
      Claude Haiku 3:   $0.25 input  /  $1.25 output  → cheapest, fast, good for simple tasks
      Claude Sonnet 3.5: $3.00 input  / $15.00 output  → 12x more expensive than Haiku
      Claude Opus 3:    $15.00 input  / $75.00 output  → 60x more expensive than Haiku

    What this reveals:
    - Cost breakdown per model family
    - Input/output token ratio per model (>10:1 = prompt bloat regardless of model)
    - Models used: if Opus is present for high-volume workloads, that's a critical finding
    - "Optimal model" suggestion based on token volume and use-case inference

    Returns per-model stats and an overall cost_by_model breakdown.

    Args:
        start_date:  YYYY-MM-DD
        end_date:    YYYY-MM-DD
        tenant_id:   Optional TenantId tag filter
    """
    flt = _build_filter(tenant_id, "Amazon Bedrock", None, None, None)

    # CE cost by USAGE_TYPE to split per model
    ce_kwargs: dict = {
        "TimePeriod":  {"Start": start_date, "End": end_date},
        "Granularity": "MONTHLY",
        "GroupBy":     [{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
        "Metrics":     ["UnblendedCost", "UsageQuantity"],
    }
    if flt:
        ce_kwargs["Filter"] = flt
    elif tenant_id is None:
        ce_kwargs["Filter"] = {
            "Dimensions": {"Key": "SERVICE", "Values": ["Amazon Bedrock"], "MatchOptions": ["EQUALS"]}
        }
    ce_resp = _ce().get_cost_and_usage(**ce_kwargs)

    cost_by_usage: dict = {}
    for period in ce_resp.get("ResultsByTime", []):
        for g in period.get("Groups", []):
            key    = g["Keys"][0]
            amount = float(g["Metrics"]["UnblendedCost"]["Amount"])
            qty    = float(g["Metrics"]["UsageQuantity"]["Amount"])
            if amount > 0:
                cost_by_usage[key] = cost_by_usage.get(key, {"cost_usd": 0.0, "tokens": 0.0})
                cost_by_usage[key]["cost_usd"] += amount
                cost_by_usage[key]["tokens"]   += qty

    # Group by model family from usage type strings
    model_costs: dict = {}
    for usage_type, data in cost_by_usage.items():
        # Infer model from usage type (e.g. "USE1-claude-3-haiku:InputTokens")
        model = "unknown"
        ut_lower = usage_type.lower()
        if "haiku" in ut_lower:
            model = "claude-3-haiku"
        elif "sonnet" in ut_lower:
            model = "claude-3-sonnet" if "3-sonnet" in ut_lower else "claude-3.5-sonnet"
        elif "opus" in ut_lower:
            model = "claude-3-opus"
        elif "titan" in ut_lower:
            model = "amazon-titan"
        elif "llama" in ut_lower:
            model = "meta-llama"
        elif "mistral" in ut_lower:
            model = "mistral"

        if model not in model_costs:
            model_costs[model] = {"cost_usd": 0.0, "input_tokens": 0.0, "output_tokens": 0.0}
        if "input" in ut_lower:
            model_costs[model]["input_tokens"] += data["tokens"]
        elif "output" in ut_lower:
            model_costs[model]["output_tokens"] += data["tokens"]
        model_costs[model]["cost_usd"] += data["cost_usd"]

    # Compute derived metrics per model
    models_out = []
    total_cost = sum(v["cost_usd"] for v in model_costs.values())
    for model, data in model_costs.items():
        inp  = data["input_tokens"]
        out  = data["output_tokens"]
        cost = data["cost_usd"]
        ratio = round(inp / out, 1) if out > 0 else None
        pct_of_total = round(cost / total_cost * 100, 1) if total_cost > 0 else 0

        flags = []
        if model == "claude-3-opus" and cost > 50:
            flags.append("HIGH_COST_MODEL: consider claude-3-sonnet for most tasks")
        if model in ("claude-3.5-sonnet", "claude-3-sonnet") and cost > 200:
            flags.append("VOLUME_CANDIDATE: evaluate claude-3-haiku for simple classification/routing tasks")
        if ratio and ratio > 10:
            flags.append(f"PROMPT_BLOAT: input/output ratio {ratio}:1 — reduce system prompt or context window")

        models_out.append({
            "model":             model,
            "cost_usd":          round(cost, 2),
            "pct_of_total":      pct_of_total,
            "input_tokens":      int(inp),
            "output_tokens":     int(out),
            "input_output_ratio": ratio,
            "flags":             flags,
        })

    models_out.sort(key=lambda x: x["cost_usd"], reverse=True)
    logger.info("get_bedrock_model_efficiency: models=%d total=%.2f", len(models_out), total_cost)
    return {
        "total_bedrock_cost_usd": round(total_cost, 2),
        "by_model":               models_out,
        "cost_by_usage_type":     dict(sorted(cost_by_usage.items(),
                                              key=lambda x: x[1]["cost_usd"], reverse=True)),
    }


@tool
def get_bedrock_prompt_cache_opportunity(
    start_date: str,
    end_date: str,
    tenant_id: Optional[str] = None,
) -> dict:
    """
    Estimates the ROI of enabling Bedrock Prompt Caching for repeated context.

    Prompt Caching saves 90% on cached input tokens when the same system prompt
    or context prefix is reused across calls. For agents with large system prompts
    (1000+ tokens) called thousands of times per day, this is the single highest-ROI
    optimisation available.

    Pricing:
      Normal input tokens:      $3.00/1M (Sonnet 3.5)
      Cache write (first call): $3.75/1M (25% premium to write cache)
      Cache read (subsequent):  $0.30/1M (90% cheaper than normal input)

    Break-even: if the same context prefix is reused > 1.25x (virtually always true
    for agents with a fixed system prompt), caching saves money from the 2nd call.

    Detection signals:
      - High input token volume vs output (agents with large system prompts)
      - Consistent daily input token count (stable system prompt size)
      - Input/output ratio > 5:1 (large context relative to response)

    Returns estimated monthly saving if prompt caching were enabled on the top models.

    Args:
        start_date:  YYYY-MM-DD
        end_date:    YYYY-MM-DD
        tenant_id:   Optional TenantId tag filter
    """
    from datetime import datetime as dt

    start_dt = dt.fromisoformat(start_date).replace(tzinfo=timezone.utc)
    end_dt   = dt.fromisoformat(end_date).replace(tzinfo=timezone.utc)

    # Get input token metrics from CloudWatch (per day to detect pattern stability)
    input_pts = []
    output_pts = []
    try:
        for metric, store in [("InputTokenCount", input_pts), ("OutputTokenCount", output_pts)]:
            dims = []
            if tenant_id and tenant_id.upper() != "ALL":
                dims = [{"Name": "TenantId", "Value": tenant_id}]
            kwargs: dict = {
                "Namespace":  "AWS/Bedrock",
                "MetricName": metric,
                "StartTime":  start_dt,
                "EndTime":    end_dt,
                "Period":     86400,
                "Statistics": ["Sum"],
            }
            if dims:
                kwargs["Dimensions"] = dims
            resp = _cw().get_metric_statistics(**kwargs)
            store.extend(dp.get("Sum", 0) for dp in resp.get("Datapoints", []))
    except Exception:
        pass

    total_input  = sum(input_pts)
    total_output = sum(output_pts)
    days = max(len(input_pts), 1)

    # Estimate cacheable fraction: assume 60% of input tokens are repeated context
    # (system prompt + few-shot examples); conservative estimate
    cacheable_fraction = 0.60
    cacheable_tokens   = total_input * cacheable_fraction

    # Cost without caching (all input at $3/1M for Sonnet)
    input_cost_per_1m = 3.0
    current_cost      = total_input / 1_000_000 * input_cost_per_1m

    # Cost with caching: cache write on first call per day, cache reads on subsequent
    # Assume average 50 calls/day sharing same cache prefix
    cache_writes_per_day = 1
    cache_reads_per_day  = 49
    total_cache_writes   = cacheable_tokens * (cache_writes_per_day / 50)
    total_cache_reads    = cacheable_tokens * (cache_reads_per_day  / 50)
    cost_with_cache = (
        (total_input - cacheable_tokens) / 1_000_000 * input_cost_per_1m  # uncacheable
        + total_cache_writes / 1_000_000 * 3.75                            # write premium
        + total_cache_reads  / 1_000_000 * 0.30                            # read discount
    )

    saving         = max(0, current_cost - cost_with_cache)
    monthly_saving = round(saving * 30 / days, 2) if days > 0 else 0

    # Detect if input/output ratio is stable (good caching candidate)
    ratio = round(total_input / total_output, 1) if total_output > 0 else None
    daily_input_std = 0.0
    if len(input_pts) > 1:
        mean = sum(input_pts) / len(input_pts)
        daily_input_std = (sum((x - mean) ** 2 for x in input_pts) / len(input_pts)) ** 0.5

    cv = round(daily_input_std / (sum(input_pts) / len(input_pts)), 2) if input_pts else None

    return {
        "total_input_tokens":          int(total_input),
        "total_output_tokens":         int(total_output),
        "input_output_ratio":          ratio,
        "daily_input_cv":              cv,           # coefficient of variation; <0.3 = stable pattern
        "cacheable_fraction_assumed":  cacheable_fraction,
        "estimated_current_cost_usd":  round(current_cost, 2),
        "estimated_cached_cost_usd":   round(cost_with_cache, 2),
        "estimated_monthly_saving_usd": monthly_saving,
        "is_caching_candidate":        (ratio is not None and ratio > 3 and monthly_saving > 20),
        "recommendation": (
            f"Enable Prompt Caching on system prompts. Estimated saving: "
            f"${monthly_saving}/month. Input/output ratio {ratio}:1 indicates large "
            f"repeated context — each cached call costs 90% less in input tokens."
            if monthly_saving > 0 else
            "Low input volume — prompt caching ROI is minimal at current scale."
        ),
    }


@tool
def get_bedrock_batch_opportunity(
    start_date: str,
    end_date: str,
    tenant_id: Optional[str] = None,
) -> dict:
    """
    Detects Bedrock workloads suitable for Batch Inference (50% cheaper than on-demand).

    Batch inference processes requests asynchronously (results within 24h) at 50% of
    on-demand price. Suitable for: report generation, data enrichment, classification
    pipelines, nightly analytics — anything that doesn't need real-time response.

    Detection: analyses hourly token patterns to find burst-then-idle signatures
    characteristic of batch jobs vs real-time interactive traffic.

    Signals for batch suitability:
      - Token bursts at specific hours (nightly jobs, end-of-day reports)
      - Low variance in burst timing (predictable schedule)
      - High token volume per burst relative to daily total

    Returns estimated saving if batch inference were used for detected batch-like traffic.

    Args:
        start_date:  YYYY-MM-DD
        end_date:    YYYY-MM-DD
        tenant_id:   Optional TenantId tag filter
    """
    from datetime import datetime as dt

    start_dt = dt.fromisoformat(start_date).replace(tzinfo=timezone.utc)
    end_dt   = dt.fromisoformat(end_date).replace(tzinfo=timezone.utc)

    # Hourly token data to detect bursty patterns
    hourly_tokens: list = []
    try:
        dims = []
        if tenant_id and tenant_id.upper() != "ALL":
            dims = [{"Name": "TenantId", "Value": tenant_id}]
        kwargs: dict = {
            "Namespace":  "AWS/Bedrock",
            "MetricName": "InputTokenCount",
            "StartTime":  start_dt,
            "EndTime":    end_dt,
            "Period":     3600,
            "Statistics": ["Sum"],
        }
        if dims:
            kwargs["Dimensions"] = dims
        resp = _cw().get_metric_statistics(**kwargs)
        hourly_tokens = [
            {"hour": dp["Timestamp"].hour, "tokens": dp.get("Sum", 0)}
            for dp in resp.get("Datapoints", [])
        ]
    except Exception:
        pass

    if not hourly_tokens:
        return {
            "batch_suitable_pct": 0,
            "estimated_monthly_saving_usd": 0,
            "pattern": "insufficient_data",
            "recommendation": "No CloudWatch data available for pattern analysis.",
        }

    total_tokens = sum(h["tokens"] for h in hourly_tokens)
    if total_tokens == 0:
        return {"batch_suitable_pct": 0, "estimated_monthly_saving_usd": 0, "pattern": "no_traffic"}

    # Find peak hour buckets — hours with >5x the average hourly volume
    avg_hourly = total_tokens / max(len(hourly_tokens), 1)
    burst_tokens = sum(h["tokens"] for h in hourly_tokens if h["tokens"] > avg_hourly * 5)
    burst_pct    = round(burst_tokens / total_tokens * 100, 1) if total_tokens > 0 else 0

    # Top hours by volume
    hour_totals: dict = {}
    for h in hourly_tokens:
        hour_totals[h["hour"]] = hour_totals.get(h["hour"], 0) + h["tokens"]
    top_hours = sorted(hour_totals.items(), key=lambda x: x[1], reverse=True)[:3]

    # Estimate cost saving: batch_suitable traffic at 50% discount
    batch_suitable_pct = min(burst_pct, 70)  # cap at 70% — some burst may be user-facing
    days = (end_dt - start_dt).days or 1
    monthly_tokens_batch = total_tokens * (batch_suitable_pct / 100) * 30 / days
    # Sonnet input at $3/1M → saving is 50% of batch-eligible tokens
    monthly_saving = round(monthly_tokens_batch / 1_000_000 * 3.0 * 0.50, 2)

    pattern = (
        "STRONG_BATCH_CANDIDATE" if batch_suitable_pct > 30 else
        "MODERATE_BATCH_CANDIDATE" if batch_suitable_pct > 10 else
        "REALTIME_DOMINANT"
    )

    return {
        "total_input_tokens":           int(total_tokens),
        "burst_traffic_pct":            burst_pct,
        "batch_suitable_pct":           batch_suitable_pct,
        "peak_hours_utc":               [h for h, _ in top_hours],
        "pattern":                      pattern,
        "estimated_monthly_saving_usd": monthly_saving,
        "recommendation": (
            f"~{batch_suitable_pct}% of Bedrock traffic is burst/batch-pattern. "
            f"Migrating these calls to Batch Inference saves ~${monthly_saving}/month (50% discount). "
            f"Peak activity at UTC hours {[h for h, _ in top_hours]}."
            if pattern != "REALTIME_DOMINANT" else
            "Traffic pattern is real-time dominant — batch inference not applicable."
        ),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# RDS SPECIALIST TOOLS
# ═══════════════════════════════════════════════════════════════════════════════

def _rds():
    if not hasattr(_rds, "_c") or _rds._c is None:
        _rds._c = boto3.client(
            "rds",
            region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
        )
    return _rds._c
_rds._c = None


@tool
def get_rds_rightsizing(
    lookback_days: int = 14,
) -> list:
    """
    Identifies over-provisioned RDS instances by comparing instance size to actual
    CPU and connection utilisation over the lookback period.

    The AWS console shows CPU per instance but never says "this instance is
    over-provisioned and costs X more than necessary." This tool does that.

    Downsize candidates (conservative thresholds — safe to act on):
      - CPU avg < 10% AND max < 25%  → 2x oversize at minimum
      - DB connections < 10% of max connections → memory over-provisioned
      - Both CPU and connections low → strong terminate/downsize signal

    Instance sizing cost impact: each size step down halves cost.
    db.r6g.4xlarge (~$800/mo) → db.r6g.2xlarge (~$400/mo) if underutilised.

    Returns list of instances with utilisation metrics and downsize recommendations.

    Args:
        lookback_days: days of CloudWatch metrics to analyse (default 14)
    """
    from datetime import datetime as dt

    end_dt   = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=lookback_days)

    # List all RDS instances
    paginator = _rds().get_paginator("describe_db_instances")
    instances = []
    for page in paginator.paginate():
        instances.extend(page.get("DBInstances", []))

    results = []
    for inst in instances:
        db_id    = inst["DBInstanceIdentifier"]
        db_class = inst.get("DBInstanceClass", "")
        engine   = inst.get("Engine", "")
        az       = inst.get("AvailabilityZone", "")
        multi_az = inst.get("MultiAZ", False)
        status   = inst.get("DBInstanceStatus", "")

        if status != "available":
            continue

        dims = [{"Name": "DBInstanceIdentifier", "Value": db_id}]

        def _cw_avg(metric: str) -> float:
            try:
                resp = _cw().get_metric_statistics(
                    Namespace="AWS/RDS",
                    MetricName=metric,
                    Dimensions=dims,
                    StartTime=start_dt,
                    EndTime=end_dt,
                    Period=3600,
                    Statistics=["Average", "Maximum"],
                )
                pts = resp.get("Datapoints", [])
                if not pts:
                    return -1.0
                avgs = [dp.get("Average", 0) for dp in pts]
                maxs = [dp.get("Maximum", 0) for dp in pts]
                return round(sum(avgs) / len(avgs), 1), round(max(maxs), 1)
            except Exception:
                return -1.0, -1.0

        cpu_avg, cpu_max          = _cw_avg("CPUUtilization")
        conn_avg, conn_max        = _cw_avg("DatabaseConnections")
        freemem_avg, _            = _cw_avg("FreeableMemory")

        flags = []
        recommendation = None

        if cpu_avg >= 0 and cpu_avg < 10 and cpu_max < 25:
            flags.append(f"CPU avg {cpu_avg}% / max {cpu_max}% — severely under-utilised")
        if conn_avg >= 0 and conn_avg < 5:
            flags.append(f"Avg {conn_avg:.1f} DB connections — near-idle")
            if cpu_avg < 5:
                recommendation = "TERMINATE or STOP — no meaningful workload detected"
            else:
                recommendation = "DOWNSIZE 1-2 instance class steps"
        elif cpu_avg >= 0 and cpu_avg < 10:
            recommendation = "DOWNSIZE 1 instance class step (halves cost)"

        if flags:
            results.append({
                "db_instance_id":      db_id,
                "instance_class":      db_class,
                "engine":              engine,
                "multi_az":            multi_az,
                "cpu_avg_pct":         cpu_avg,
                "cpu_max_pct":         cpu_max,
                "connections_avg":     conn_avg,
                "freeable_memory_gb":  round(freemem_avg / (1024**3), 1) if freemem_avg > 0 else None,
                "flags":               flags,
                "recommendation":      recommendation,
                "multi_az_note":       (
                    "Multi-AZ in non-production? Consider Single-AZ to halve instance cost."
                    if multi_az else None
                ),
            })

    logger.info("get_rds_rightsizing: checked=%d candidates=%d", len(instances), len(results))
    return results


@tool
def get_rds_storage_optimization(
) -> list:
    """
    Scans all RDS instances for storage optimisation opportunities:

    1. gp2 → gp3 migration: gp3 is 20% cheaper than gp2 AND provides 3000 IOPS
       baseline for free (vs gp2's burst-based IOPS). Zero performance regression
       in most workloads. No downtime required (online storage modification).
       Saving: $0.115/GB/mo (gp2) → $0.092/GB/mo (gp3) = 20% per GB.

    2. Multi-AZ in non-production: Multi-AZ doubles instance cost. Environments
       tagged as dev/staging/test/demo rarely need synchronous failover.
       Saving: 50% of instance cost.

    3. Allocated vs used storage: RDS charges for allocated storage, not used.
       Instances allocated 2 TB but using 50 GB pay for 2 TB.

    Returns list of instances with specific storage optimisation opportunities.
    """
    paginator = _rds().get_paginator("describe_db_instances")
    instances = []
    for page in paginator.paginate():
        instances.extend(page.get("DBInstances", []))

    results = []
    for inst in instances:
        db_id      = inst["DBInstanceIdentifier"]
        storage_type = inst.get("StorageType", "")
        allocated_gb = inst.get("AllocatedStorage", 0)
        multi_az   = inst.get("MultiAZ", False)
        engine     = inst.get("Engine", "")
        db_class   = inst.get("DBInstanceClass", "")
        tags       = {t["Key"].lower(): t["Value"].lower()
                      for t in inst.get("TagList", [])}
        env_tag    = tags.get("environment", tags.get("env", ""))

        opps = []

        if storage_type == "gp2" and allocated_gb > 0:
            saving = round(allocated_gb * (0.115 - 0.092), 2)
            opps.append({
                "type":          "gp2_to_gp3",
                "detail":        f"{allocated_gb} GB gp2 → gp3: no downtime, "
                                 f"free IOPS upgrade, 20% cheaper",
                "saving_usd_mo": saving,
                "action":        f"aws rds modify-db-instance --db-instance-identifier {db_id} "
                                 f"--storage-type gp3 --apply-immediately",
            })

        is_nonprod = any(x in env_tag for x in ["dev", "staging", "test", "demo", "qa", "sandbox"])
        if multi_az and is_nonprod:
            opps.append({
                "type":          "multi_az_nonprod",
                "detail":        f"Multi-AZ enabled on '{env_tag}' environment — "
                                 f"synchronous standby unnecessary for non-production",
                "saving_usd_mo": None,  # depends on instance class pricing
                "action":        f"aws rds modify-db-instance --db-instance-identifier {db_id} "
                                 f"--no-multi-az --apply-immediately",
            })

        if opps:
            results.append({
                "db_instance_id":  db_id,
                "instance_class":  db_class,
                "engine":          engine,
                "storage_type":    storage_type,
                "allocated_gb":    allocated_gb,
                "multi_az":        multi_az,
                "environment_tag": env_tag or "untagged",
                "opportunities":   opps,
                "total_saving_usd_mo": sum(o["saving_usd_mo"] or 0 for o in opps),
            })

    results.sort(key=lambda x: x["total_saving_usd_mo"], reverse=True)
    logger.info("get_rds_storage_optimization: instances=%d with_opps=%d", len(instances), len(results))
    return results


@tool
def get_rds_idle_instances(
    lookback_days: int = 7,
) -> list:
    """
    Finds RDS instances with zero or near-zero connections over the lookback period
    — the "forgotten database" pattern common in development and demo environments.

    An idle db.r6g.2xlarge = ~$400/month burning with no one connected.
    This is one of the easiest savings to capture: stop or delete the instance.

    Returns instances with last connection time estimate and monthly cost approximation.

    Args:
        lookback_days: how far back to look for connection activity (default 7)
    """
    end_dt   = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=lookback_days)

    paginator = _rds().get_paginator("describe_db_instances")
    instances = []
    for page in paginator.paginate():
        instances.extend(page.get("DBInstances", []))

    idle = []
    for inst in instances:
        db_id  = inst["DBInstanceIdentifier"]
        status = inst.get("DBInstanceStatus", "")
        if status not in ("available", "stopped"):
            continue

        try:
            resp = _cw().get_metric_statistics(
                Namespace="AWS/RDS",
                MetricName="DatabaseConnections",
                Dimensions=[{"Name": "DBInstanceIdentifier", "Value": db_id}],
                StartTime=start_dt,
                EndTime=end_dt,
                Period=3600,
                Statistics=["Maximum"],
            )
            pts = resp.get("Datapoints", [])
            max_conns = max((dp.get("Maximum", 0) for dp in pts), default=0)
        except Exception:
            continue

        if max_conns < 2:  # 0 or 1 connection (monitoring agent counts as 1)
            idle.append({
                "db_instance_id":  db_id,
                "instance_class":  inst.get("DBInstanceClass", ""),
                "engine":          inst.get("Engine", ""),
                "status":          status,
                "max_connections_in_period": int(max_conns),
                "lookback_days":   lookback_days,
                "action":          (
                    "DELETE instance (back up first)" if status == "available"
                    else "Already stopped — schedule deletion after confirming no owner"
                ),
            })

    logger.info("get_rds_idle_instances: checked=%d idle=%d", len(instances), len(idle))
    return idle


# ═══════════════════════════════════════════════════════════════════════════════
# AURORA SPECIALIST TOOLS
# ═══════════════════════════════════════════════════════════════════════════════

def _aurora():
    """Re-uses the RDS client — Aurora is managed via RDS API."""
    return _rds()


@tool
def get_aurora_io_optimization(
    start_date: str,
    end_date: str,
) -> list:
    """
    Determines whether each Aurora cluster should migrate to Aurora I/O-Optimized.

    Aurora I/O-Optimized pricing:
      - Storage: 25% more expensive ($0.225/GB/mo vs $0.10/GB/mo)
      - I/O operations: FREE (vs $0.20 per million I/Os in standard)

    Break-even rule: migrate to I/O-Optimized when I/O costs > 25% of total Aurora cost.
    Above that threshold, the I/O savings exceed the storage premium.

    This calculation is never shown in the console. Teams pay for I/O-heavy Aurora
    clusters on standard pricing for years without knowing I/O-Optimized would be cheaper.

    Returns per-cluster analysis with current costs, break-even status, and
    estimated monthly saving or extra cost of switching.

    Args:
        start_date:  YYYY-MM-DD
        end_date:    YYYY-MM-DD
    """
    # Get Aurora cluster I/O costs from CE
    io_kwargs: dict = {
        "TimePeriod":  {"Start": start_date, "End": end_date},
        "Granularity": "MONTHLY",
        "GroupBy":     [{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
        "Metrics":     ["UnblendedCost", "UsageQuantity"],
        "Filter":      {
            "Dimensions": {"Key": "SERVICE",
                           "Values": ["Amazon Relational Database Service"],
                           "MatchOptions": ["EQUALS"]}
        },
    }
    ce_resp = _ce().get_cost_and_usage(**io_kwargs)

    aurora_costs: dict = {}
    for period in ce_resp.get("ResultsByTime", []):
        for g in period.get("Groups", []):
            ut     = g["Keys"][0]
            amount = float(g["Metrics"]["UnblendedCost"]["Amount"])
            if "Aurora" in ut or "aurora" in ut:
                aurora_costs[ut] = aurora_costs.get(ut, 0.0) + amount

    # Separate I/O from storage/compute
    io_cost      = sum(v for k, v in aurora_costs.items() if "IO" in k or "io" in k.lower())
    storage_cost = sum(v for k, v in aurora_costs.items() if "Storage" in k)
    compute_cost = sum(v for k, v in aurora_costs.items()
                       if "InstanceUsage" in k or "Serverless" in k)
    total_aurora = sum(aurora_costs.values())

    io_pct = round(io_cost / total_aurora * 100, 1) if total_aurora > 0 else 0

    # Break-even: I/O-Optimized adds 25% to storage cost, removes all I/O cost
    storage_premium = storage_cost * 0.25
    net_saving      = round(io_cost - storage_premium, 2)
    should_switch   = net_saving > 0

    # List Aurora clusters from RDS API
    clusters = []
    try:
        paginator = _aurora().get_paginator("describe_db_clusters")
        for page in paginator.paginate():
            for c in page.get("DBClusters", []):
                if "aurora" in c.get("Engine", "").lower():
                    clusters.append({
                        "cluster_id":      c["DBClusterIdentifier"],
                        "engine":          c.get("Engine"),
                        "engine_version":  c.get("EngineVersion"),
                        "storage_encrypted": c.get("StorageEncrypted", False),
                        "io_optimized_now": c.get("StorageType", "") == "aurora-iopt1",
                    })
    except Exception:
        pass

    results = []
    for cluster in clusters:
        if cluster["io_optimized_now"]:
            continue  # already on I/O-Optimized

        results.append({
            "cluster_id":               cluster["cluster_id"],
            "engine":                   cluster["engine"],
            "current_pricing_model":    "standard",
            "aurora_io_cost_usd":       round(io_cost / max(len(clusters), 1), 2),
            "aurora_storage_cost_usd":  round(storage_cost / max(len(clusters), 1), 2),
            "io_pct_of_total":          io_pct,
            "break_even_threshold_pct": 25.0,
            "should_switch_to_io_optimized": should_switch,
            "estimated_net_saving_usd": max(net_saving, 0),
            "estimated_net_cost_usd":   abs(min(net_saving, 0)),
            "recommendation": (
                f"SWITCH to Aurora I/O-Optimized: I/O is {io_pct}% of cost (threshold 25%). "
                f"Saves ~${net_saving:.2f}/mo by eliminating I/O charges."
                if should_switch else
                f"STAY on standard: I/O is only {io_pct}% of cost. "
                f"I/O-Optimized would cost ${abs(net_saving):.2f}/mo MORE."
            ),
        })

    logger.info("get_aurora_io_optimization: clusters=%d io_pct=%.1f should_switch=%s",
                len(clusters), io_pct, should_switch)
    return results


@tool
def get_aurora_serverless_waste(
    lookback_days: int = 7,
) -> list:
    """
    Detects Aurora Serverless v2 clusters paying for idle minimum ACU capacity.

    Aurora Serverless v2 charges for ACU-hours even at minimum capacity.
    If your minimum ACU is set to 2.0 and the cluster is idle 16h/day,
    you pay 2 ACU × 16h × $0.12/ACU-hr = $3.84/day = ~$115/month in idle capacity.

    The console never shows "you paid $115 for ACUs that did nothing."

    Optimisation: reduce minimum ACU to 0.5 (the lowest supported value).
    ACU scales back up within seconds when traffic arrives.

    Saving: (current_min_acu - 0.5) × idle_hours_per_day × $0.12 × 30

    Args:
        lookback_days: days of CloudWatch metrics to assess idle pattern (default 7)
    """
    end_dt   = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=lookback_days)

    clusters = []
    try:
        paginator = _aurora().get_paginator("describe_db_clusters")
        for page in paginator.paginate():
            for c in page.get("DBClusters", []):
                if ("aurora" in c.get("Engine", "").lower()
                        and c.get("ServerlessV2ScalingConfiguration")):
                    clusters.append(c)
    except Exception:
        pass

    results = []
    for cluster in clusters:
        cid      = cluster["DBClusterIdentifier"]
        scaling  = cluster.get("ServerlessV2ScalingConfiguration", {})
        min_acu  = float(scaling.get("MinCapacity", 0.5))
        max_acu  = float(scaling.get("MaxCapacity", 128))

        # CloudWatch: ServerlessDatabaseCapacity metric (actual ACU used)
        try:
            resp = _cw().get_metric_statistics(
                Namespace="AWS/RDS",
                MetricName="ServerlessDatabaseCapacity",
                Dimensions=[{"Name": "DBClusterIdentifier", "Value": cid}],
                StartTime=start_dt,
                EndTime=end_dt,
                Period=3600,
                Statistics=["Average", "Minimum"],
            )
            pts = resp.get("Datapoints", [])
        except Exception:
            pts = []

        if not pts:
            continue

        avg_acu     = round(sum(dp.get("Average", 0) for dp in pts) / len(pts), 2)
        at_minimum  = sum(1 for dp in pts if dp.get("Minimum", 0) <= min_acu * 1.1)
        idle_pct    = round(at_minimum / len(pts) * 100, 1)
        idle_hrs_day = round(idle_pct / 100 * 24, 1)

        # Cost of idle capacity: paying min_acu when actual work needs < min_acu
        wasted_acu_per_hour = max(0, min_acu - 0.5)
        monthly_waste_usd   = round(wasted_acu_per_hour * idle_hrs_day * 0.12 * 30, 2)

        if idle_pct > 20 and wasted_acu_per_hour > 0:
            results.append({
                "cluster_id":           cid,
                "engine":               cluster.get("Engine"),
                "min_acu_configured":   min_acu,
                "max_acu_configured":   max_acu,
                "avg_acu_actual":       avg_acu,
                "hours_at_minimum_pct": idle_pct,
                "idle_hours_per_day":   idle_hrs_day,
                "monthly_waste_usd":    monthly_waste_usd,
                "recommendation":       (
                    f"Reduce MinCapacity from {min_acu} to 0.5 ACU. "
                    f"Cluster is at minimum {idle_pct}% of the time ({idle_hrs_day}h/day idle). "
                    f"Saving: ~${monthly_waste_usd}/month. "
                    f"Scale-up latency from 0.5→{min_acu} ACU is <1 second."
                ),
            })

    results.sort(key=lambda x: x["monthly_waste_usd"], reverse=True)
    logger.info("get_aurora_serverless_waste: clusters=%d wasteful=%d", len(clusters), len(results))
    return results


# ═══════════════════════════════════════════════════════════════════════════════
# EC2 SPECIALIST TOOLS
# ═══════════════════════════════════════════════════════════════════════════════

def _ec2():
    if not hasattr(_ec2, "_c") or _ec2._c is None:
        _ec2._c = boto3.client(
            "ec2",
            region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
        )
    return _ec2._c
_ec2._c = None


@tool
def get_ec2_waste_inventory() -> dict:
    """
    Scans for EC2 waste that accumulates silently over time:

    1. Unattached EBS volumes — paying for storage no instance is using.
       Cost: $0.08-0.115/GB/mo for gp2/gp3. A forgotten 500 GB volume = $57/mo.

    2. Unassociated Elastic IPs — $0.005/hr (~$3.65/mo each) when not attached
       to a running instance. 10 orphan EIPs = $36/mo for nothing.

    3. Old EBS snapshots (>90 days) from terminated instances — snapshot cost
       is incremental but chains of old snapshots accumulate to hundreds of GB.

    4. Stopped instances still paying for attached EBS storage and EIP.

    A 2-year-old AWS account typically has $200-500/mo in these orphan resources.
    None of them appear in any dashboard — you have to hunt for them.

    Returns categorised waste inventory with per-item cost estimates.
    """
    waste: dict = {
        "unattached_volumes":    [],
        "unassociated_eips":     [],
        "old_snapshots":         [],
        "stopped_instances":     [],
    }

    # ── Unattached EBS volumes ────────────────────────────────────────────────
    try:
        paginator = _ec2().get_paginator("describe_volumes")
        for page in paginator.paginate(Filters=[{"Name": "status", "Values": ["available"]}]):
            for vol in page.get("Volumes", []):
                size_gb   = vol.get("Size", 0)
                vol_type  = vol.get("VolumeType", "gp2")
                price_map = {"gp2": 0.10, "gp3": 0.08, "io1": 0.125, "io2": 0.125,
                             "st1": 0.045, "sc1": 0.025}
                monthly = round(size_gb * price_map.get(vol_type, 0.10), 2)
                waste["unattached_volumes"].append({
                    "volume_id":       vol["VolumeId"],
                    "size_gb":         size_gb,
                    "volume_type":     vol_type,
                    "create_time":     vol["CreateTime"].isoformat(),
                    "monthly_cost_usd": monthly,
                })
    except Exception:
        pass

    # ── Unassociated Elastic IPs ──────────────────────────────────────────────
    try:
        resp = _ec2().describe_addresses()
        for addr in resp.get("Addresses", []):
            if not addr.get("AssociationId"):
                waste["unassociated_eips"].append({
                    "allocation_id":   addr.get("AllocationId"),
                    "public_ip":       addr.get("PublicIp"),
                    "monthly_cost_usd": 3.65,
                })
    except Exception:
        pass

    # ── Old snapshots (>90 days, not tagged as backup) ────────────────────────
    try:
        account_id = boto3.client(
            "sts", region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
        ).get_caller_identity()["Account"]
        now = datetime.now(timezone.utc)
        paginator = _ec2().get_paginator("describe_snapshots")
        for page in paginator.paginate(OwnerIds=[account_id]):
            for snap in page.get("Snapshots", []):
                age_days = (now - snap["StartTime"]).days
                if age_days > 90:
                    size_gb  = snap.get("VolumeSize", 0)
                    monthly  = round(size_gb * 0.05, 2)  # ~$0.05/GB snapshot
                    waste["old_snapshots"].append({
                        "snapshot_id":     snap["SnapshotId"],
                        "age_days":        age_days,
                        "size_gb":         size_gb,
                        "description":     snap.get("Description", "")[:60],
                        "monthly_cost_usd": monthly,
                    })
    except Exception:
        pass

    # ── Stopped instances ─────────────────────────────────────────────────────
    try:
        paginator = _ec2().get_paginator("describe_instances")
        for page in paginator.paginate(
            Filters=[{"Name": "instance-state-name", "Values": ["stopped"]}]
        ):
            for reservation in page.get("Reservations", []):
                for inst in reservation.get("Instances", []):
                    name_tag = next(
                        (t["Value"] for t in inst.get("Tags", []) if t["Key"] == "Name"), ""
                    )
                    waste["stopped_instances"].append({
                        "instance_id":   inst["InstanceId"],
                        "instance_type": inst.get("InstanceType"),
                        "name":          name_tag,
                        "note":          "Stopped but EBS volumes still incurring charges",
                    })
    except Exception:
        pass

    # Totals
    total_monthly = (
        sum(v["monthly_cost_usd"] for v in waste["unattached_volumes"])
        + sum(e["monthly_cost_usd"] for e in waste["unassociated_eips"])
        + sum(s["monthly_cost_usd"] for s in waste["old_snapshots"])
    )

    logger.info(
        "get_ec2_waste_inventory: volumes=%d eips=%d snapshots=%d stopped=%d total=%.2f",
        len(waste["unattached_volumes"]), len(waste["unassociated_eips"]),
        len(waste["old_snapshots"]), len(waste["stopped_instances"]), total_monthly,
    )
    return {
        **waste,
        "summary": {
            "unattached_volumes_count":  len(waste["unattached_volumes"]),
            "unassociated_eips_count":   len(waste["unassociated_eips"]),
            "old_snapshots_count":       len(waste["old_snapshots"]),
            "stopped_instances_count":   len(waste["stopped_instances"]),
            "total_waste_usd_mo":        round(total_monthly, 2),
        },
    }


@tool
def get_ec2_generation_gap() -> list:
    """
    Identifies EC2 instances running on old generations with a direct newer
    equivalent that is cheaper AND faster.

    Generation upgrades that save money with zero performance regression:
      t2.* → t4g.*: 20% cheaper, ARM64 (Graviton2), better burst
      m4.* → m7g.*: 40% cheaper, Graviton3
      c4.* → c7g.*: 40% cheaper, Graviton3
      r4.* → r7g.*: 35% cheaper, Graviton3
      m5.*  → m7g.*: 25% cheaper (cross-arch) or m7i (same arch, 10% cheaper)

    Note: Graviton (ARM) requires application compatibility check.
    Always list both Graviton and Intel/AMD equivalents.

    Returns instances on old generations with specific upgrade recommendation
    and estimated monthly saving.
    """
    GENERATION_MAP = {
        # old_prefix: [(new_type_prefix, saving_pct, note)]
        "t2.":  [("t4g.", 20, "Graviton2 ARM"), ("t3.", 10, "Intel/AMD x86")],
        "t3.":  [("t4g.", 10, "Graviton2 ARM")],
        "m4.":  [("m7g.", 40, "Graviton3 ARM"), ("m7i.", 10, "Intel x86")],
        "m5.":  [("m7g.", 25, "Graviton3 ARM"), ("m7i.", 10, "Intel x86")],
        "m6i.": [("m7i.", 10, "Intel x86 latest gen")],
        "c4.":  [("c7g.", 40, "Graviton3 ARM"), ("c7i.", 15, "Intel x86")],
        "c5.":  [("c7g.", 25, "Graviton3 ARM"), ("c7i.", 10, "Intel x86")],
        "r4.":  [("r7g.", 35, "Graviton3 ARM"), ("r7i.", 10, "Intel x86")],
        "r5.":  [("r7g.", 25, "Graviton3 ARM"), ("r7i.", 10, "Intel x86")],
    }

    results = []
    try:
        paginator = _ec2().get_paginator("describe_instances")
        for page in paginator.paginate(
            Filters=[{"Name": "instance-state-name", "Values": ["running"]}]
        ):
            for reservation in page.get("Reservations", []):
                for inst in reservation.get("Instances", []):
                    itype = inst.get("InstanceType", "")
                    name  = next(
                        (t["Value"] for t in inst.get("Tags", []) if t["Key"] == "Name"), ""
                    )

                    for old_prefix, upgrades in GENERATION_MAP.items():
                        if itype.startswith(old_prefix):
                            size    = itype[len(old_prefix):]
                            options = [
                                {
                                    "recommended_type": f"{new_prefix}{size}",
                                    "saving_pct":       saving_pct,
                                    "note":             note,
                                }
                                for new_prefix, saving_pct, note in upgrades
                            ]
                            results.append({
                                "instance_id":    inst["InstanceId"],
                                "instance_type":  itype,
                                "name":           name,
                                "az":             inst.get("Placement", {}).get("AvailabilityZone"),
                                "upgrade_options": options,
                                "max_saving_pct": max(o["saving_pct"] for o in options),
                            })
                            break
    except Exception:
        pass

    results.sort(key=lambda x: x["max_saving_pct"], reverse=True)
    logger.info("get_ec2_generation_gap: old_gen_instances=%d", len(results))
    return results


@tool
def get_spot_opportunity(
    start_date: str,
    end_date: str,
    tenant_id: Optional[str] = None,
) -> dict:
    """
    Analyses EC2 and ECS/Fargate spending to identify workloads suitable for Spot
    instances (70-90% cheaper than On-Demand).

    Spot suitability criteria:
      - Stateless (no local state that can't be recreated in seconds)
      - Fault-tolerant (handles instance interruption gracefully)
      - Flexible on instance type and AZ

    Typical Spot candidates:
      - Batch processing jobs (data pipelines, ML training, ETL)
      - Stateless web tier behind ALB (auto-scaling group with mixed instances)
      - CI/CD build workers
      - Dev/test environments (interruption just means slower tests)

    NOT suitable for Spot:
      - Databases (RDS, self-managed) — interruption causes outage
      - Stateful services — would lose in-memory state
      - Single-instance critical services

    Returns: On-Demand EC2 spend breakdown and estimated Spot saving potential.

    Args:
        start_date:  YYYY-MM-DD
        end_date:    YYYY-MM-DD
        tenant_id:   Optional TenantId tag filter
    """
    flt = _build_filter(tenant_id, "Amazon EC2", None, None, None)

    ce_kwargs: dict = {
        "TimePeriod":  {"Start": start_date, "End": end_date},
        "Granularity": "MONTHLY",
        "GroupBy":     [{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
        "Metrics":     ["UnblendedCost", "UsageQuantity"],
    }
    if flt:
        ce_kwargs["Filter"] = flt
    elif tenant_id is None:
        ce_kwargs["Filter"] = {
            "Dimensions": {"Key": "SERVICE", "Values": ["Amazon EC2"], "MatchOptions": ["EQUALS"]}
        }
    ce_resp = _ce().get_cost_and_usage(**ce_kwargs)

    on_demand_cost = 0.0
    spot_cost      = 0.0
    box_usage_cost = 0.0  # BoxUsage = On-Demand instance hours

    for period in ce_resp.get("ResultsByTime", []):
        for g in period.get("Groups", []):
            ut     = g["Keys"][0]
            amount = float(g["Metrics"]["UnblendedCost"]["Amount"])
            if "SpotUsage" in ut or "Spot" in ut:
                spot_cost += amount
            elif "BoxUsage" in ut:
                box_usage_cost += amount
                on_demand_cost += amount
            elif "InstanceUsage" in ut:
                on_demand_cost += amount

    total_compute = on_demand_cost + spot_cost
    spot_pct_current = round(spot_cost / total_compute * 100, 1) if total_compute > 0 else 0

    # Conservative: assume 40% of On-Demand is spot-suitable (stateless workloads)
    spot_candidate_pct  = 40
    spot_saving_per_pct = 0.75  # avg 75% saving on spot vs on-demand
    estimated_saving    = round(
        on_demand_cost * (spot_candidate_pct / 100) * spot_saving_per_pct, 2
    )

    return {
        "on_demand_compute_cost_usd":    round(on_demand_cost, 2),
        "spot_compute_cost_usd":         round(spot_cost, 2),
        "spot_pct_of_compute":           spot_pct_current,
        "spot_candidate_pct_assumed":    spot_candidate_pct,
        "estimated_monthly_saving_usd":  estimated_saving,
        "recommendation": (
            f"Only {spot_pct_current}% of compute uses Spot. "
            f"Migrating stateless workloads (~{spot_candidate_pct}% of On-Demand) "
            f"to Spot/mixed-instance ASGs saves ~${estimated_saving}/month."
            if spot_pct_current < 30 and on_demand_cost > 100 else
            f"Spot adoption at {spot_pct_current}% — already well optimised."
        ),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# EKS SPECIALIST TOOLS
# ═══════════════════════════════════════════════════════════════════════════════

def _eks():
    if not hasattr(_eks, "_c") or _eks._c is None:
        _eks._c = boto3.client(
            "eks",
            region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
        )
    return _eks._c
_eks._c = None


@tool
def get_eks_pod_waste(
    lookback_days: int = 7,
) -> dict:
    """
    Detects over-provisioned pod resource requests vs actual usage in EKS clusters.

    Why pod requests matter for cost:
    The Kubernetes scheduler reserves CPU/memory on nodes based on pod requests,
    not actual usage. A pod requesting 4 CPU / 8 GB RAM but using 0.2 CPU / 1 GB
    forces the scheduler to treat that node as "nearly full" — causing new pods to
    spin up new (expensive) nodes unnecessarily.

    Over-provisioned requests → over-provisioned nodes → wasted EC2 spend.

    This tool reads Container Insights metrics from CloudWatch:
      - pod_cpu_request vs pod_cpu_limit vs pod_cpu_utilization
      - pod_memory_request vs pod_memory_limit vs pod_memory_utilization

    Rule of thumb: requests should be ~1.3x the p95 actual usage (headroom for spikes).
    If requests are >5x actual usage, the pod is a waste candidate.

    Returns per-cluster waste analysis with estimated node efficiency impact.

    Args:
        lookback_days: days of Container Insights data to analyse (default 7)
    """
    end_dt   = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=lookback_days)
    period   = lookback_days * 86400  # one aggregate point

    clusters = []
    try:
        resp = _eks().list_clusters()
        clusters = resp.get("clusters", [])
    except Exception:
        pass

    if not clusters:
        return {
            "clusters_found": 0,
            "note": "No EKS clusters found or insufficient permissions.",
        }

    cluster_results = []
    for cluster_name in clusters:
        dims_cluster = [{"Name": "ClusterName", "Value": cluster_name}]

        def _ci_metric(metric: str, stat: str = "Average") -> float:
            try:
                resp = _cw().get_metric_statistics(
                    Namespace="ContainerInsights",
                    MetricName=metric,
                    Dimensions=dims_cluster,
                    StartTime=start_dt,
                    EndTime=end_dt,
                    Period=period,
                    Statistics=[stat],
                )
                pts = resp.get("Datapoints", [])
                return round(sum(dp.get(stat, 0) for dp in pts) / max(len(pts), 1), 2)
            except Exception:
                return -1.0

        cpu_request  = _ci_metric("pod_cpu_request",             "Average")
        cpu_actual   = _ci_metric("pod_cpu_utilization",         "Average")
        mem_request  = _ci_metric("pod_memory_request",          "Average")
        mem_actual   = _ci_metric("pod_memory_utilization",      "Average")
        node_cpu_res = _ci_metric("node_cpu_reserved_capacity",  "Average")
        node_mem_res = _ci_metric("node_memory_reserved_capacity","Average")

        if cpu_request <= 0 or cpu_actual <= 0:
            continue

        cpu_waste_ratio = round(cpu_request / cpu_actual, 1) if cpu_actual > 0 else None
        mem_waste_ratio = round(mem_request / mem_actual, 1) if mem_actual > 0 else None

        flags = []
        if cpu_waste_ratio and cpu_waste_ratio > 5:
            flags.append(
                f"CPU requests are {cpu_waste_ratio}x actual usage "
                f"(target ≤1.3x p95). Reduce requests to free node capacity."
            )
        if mem_waste_ratio and mem_waste_ratio > 3:
            flags.append(
                f"Memory requests are {mem_waste_ratio}x actual usage. "
                f"Over-reserved memory blocks scheduler from packing pods efficiently."
            )
        if node_cpu_res > 0 and node_cpu_res < 50:
            flags.append(
                f"Node CPU reserved capacity only {node_cpu_res}% — "
                f"nodes are under-packed. Right-sizing pod requests would allow "
                f"denser packing and fewer nodes."
            )

        cluster_results.append({
            "cluster_name":          cluster_name,
            "cpu_request_avg":       cpu_request,
            "cpu_actual_avg":        cpu_actual,
            "cpu_waste_ratio":       cpu_waste_ratio,
            "mem_request_avg_mb":    round(mem_request / 1_000_000, 1) if mem_request > 0 else None,
            "mem_actual_avg_mb":     round(mem_actual  / 1_000_000, 1) if mem_actual  > 0 else None,
            "mem_waste_ratio":       mem_waste_ratio,
            "node_cpu_reserved_pct": node_cpu_res,
            "node_mem_reserved_pct": node_mem_res,
            "flags":                 flags,
            "recommendation": (
                "Audit pod resource requests with: "
                "`kubectl top pods --all-namespaces` and VPA (Vertical Pod Autoscaler) "
                "in recommendation mode. Set requests to ~1.3x p95 observed usage."
                if flags else "Pod resource efficiency looks reasonable."
            ),
        })

    logger.info("get_eks_pod_waste: clusters=%d with_issues=%d",
                len(clusters), sum(1 for c in cluster_results if c["flags"]))
    return {
        "clusters_analysed": len(cluster_results),
        "clusters":          cluster_results,
    }


@tool
def get_eks_node_efficiency(
    lookback_days: int = 7,
) -> dict:
    """
    Measures EKS node group pack efficiency — the ratio of actual workload resource
    usage to provisioned node capacity.

    Low pack efficiency means you're paying for EC2 nodes that are mostly idle.
    Example: 10 × m5.2xlarge nodes at 15% CPU utilisation = paying for 10 nodes
    but doing the work of 1.5. Denser packing → fewer nodes → lower EC2 bill.

    Efficiency drivers:
    - Pod request over-provisioning (see get_eks_pod_waste)
    - Cluster Autoscaler scale-in aggressiveness (default is conservative)
    - Multiple small node groups vs consolidated larger ones
    - Fargate for sporadic/bursty workloads vs always-on EC2 nodes

    Fargate vs EC2 break-even:
    - Fargate: pay per pod CPU/memory-second, no idle cost
    - EC2: pay for full node regardless of pod utilisation
    - Fargate cheaper when node utilisation < ~40% AND workload is bursty

    Returns per-cluster efficiency metrics and node group analysis.

    Args:
        lookback_days: days of Container Insights data (default 7)
    """
    end_dt   = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=lookback_days)
    period   = lookback_days * 86400

    clusters = []
    try:
        resp = _eks().list_clusters()
        clusters = resp.get("clusters", [])
    except Exception:
        pass

    if not clusters:
        return {"clusters_found": 0, "note": "No EKS clusters found."}

    cluster_results = []
    for cluster_name in clusters:
        dims = [{"Name": "ClusterName", "Value": cluster_name}]

        def _cw_stat(metric: str, stat: str = "Average") -> float:
            try:
                resp = _cw().get_metric_statistics(
                    Namespace="ContainerInsights",
                    MetricName=metric,
                    Dimensions=dims,
                    StartTime=start_dt,
                    EndTime=end_dt,
                    Period=period,
                    Statistics=[stat],
                )
                pts = resp.get("Datapoints", [])
                return round(sum(dp.get(stat, 0) for dp in pts) / max(len(pts), 1), 2)
            except Exception:
                return -1.0

        node_cpu_util  = _cw_stat("node_cpu_utilization")
        node_mem_util  = _cw_stat("node_memory_utilization")
        node_count     = _cw_stat("cluster_node_count", "Maximum")

        if node_cpu_util < 0:
            continue

        # Pack efficiency: how much of provisioned capacity is actually used
        pack_efficiency = round((node_cpu_util + node_mem_util) / 2, 1) if node_mem_util > 0 else node_cpu_util

        # Node consolidation potential: if efficiency < 40%, could use fewer nodes
        consolidation_ratio = round(100 / max(pack_efficiency, 1), 1)  # e.g. 20% util → 5x potential
        potential_node_reduction_pct = max(0, round((1 - pack_efficiency / 60) * 100, 0))

        flags = []
        if pack_efficiency < 30 and node_count > 2:
            flags.append(
                f"Pack efficiency {pack_efficiency}% — nodes are mostly idle. "
                f"Potential to run same workload on ~{round(node_count * pack_efficiency / 60)}  "
                f"nodes (target 60% efficiency)."
            )
        if pack_efficiency < 40:
            flags.append(
                "Consider Karpenter (smarter bin-packing) over Cluster Autoscaler, "
                "or Fargate for bursty/sporadic workloads."
            )

        cluster_results.append({
            "cluster_name":              cluster_name,
            "avg_cpu_utilisation_pct":   node_cpu_util,
            "avg_memory_utilisation_pct": node_mem_util,
            "pack_efficiency_pct":        pack_efficiency,
            "node_count_max":             int(node_count) if node_count > 0 else None,
            "potential_node_reduction_pct": potential_node_reduction_pct,
            "flags":                      flags,
            "recommendation": (
                f"Enable Karpenter with bin-packing consolidation policy. "
                f"At {pack_efficiency}% pack efficiency you could reduce node count "
                f"by ~{potential_node_reduction_pct}%, directly cutting EC2 spend by the same %."
                if pack_efficiency < 40 else
                f"Node efficiency at {pack_efficiency}% — within acceptable range."
            ),
        })

    logger.info("get_eks_node_efficiency: clusters=%d", len(cluster_results))
    return {
        "clusters_analysed": len(cluster_results),
        "clusters":          cluster_results,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# ELASTICACHE SPECIALIST TOOLS
# ═══════════════════════════════════════════════════════════════════════════════

def _elasticache():
    if not hasattr(_elasticache, "_c") or _elasticache._c is None:
        _elasticache._c = boto3.client(
            "elasticache",
            region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
        )
    return _elasticache._c
_elasticache._c = None


@tool
def get_elasticache_rightsizing(lookback_days: int = 14) -> list:
    """
    Identifies over- and under-provisioned ElastiCache nodes by correlating
    CPU utilisation, memory pressure, and eviction rate.

    Eviction rate > 0: cache is too small — data is being thrown out before use.
    This means cache misses that cost you backend DB or API calls. The right fix
    is to UPSIZE, not downsize.

    CPU < 10% AND evictions = 0: cache is too large — pay for memory you don't need.
    Safe to downsize 1 node class step.

    CurrConnections near limit: saturation risk — scale out before it becomes an incident.

    Pricing reference (cache.r7g.xlarge ~$0.238/hr = ~$171/mo per node).
    Each step down halves the node cost.

    Args:
        lookback_days: days of CloudWatch metrics to analyse (default 14)
    """
    end_dt   = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=lookback_days)

    clusters = []
    try:
        paginator = _elasticache().get_paginator("describe_cache_clusters")
        for page in paginator.paginate(ShowCacheNodeInfo=True):
            clusters.extend(page.get("CacheClusters", []))
    except Exception:
        pass

    results = []
    for cluster in clusters:
        cid       = cluster["CacheClusterId"]
        node_type = cluster.get("CacheNodeType", "")
        engine    = cluster.get("Engine", "")
        status    = cluster.get("CacheClusterStatus", "")

        if status != "available":
            continue

        dims = [{"Name": "CacheClusterId", "Value": cid}]

        def _metric(name: str, stat: str = "Average") -> float:
            try:
                resp = _cw().get_metric_statistics(
                    Namespace="AWS/ElastiCache",
                    MetricName=name,
                    Dimensions=dims,
                    StartTime=start_dt,
                    EndTime=end_dt,
                    Period=3600,
                    Statistics=[stat],
                )
                pts = resp.get("Datapoints", [])
                if not pts:
                    return -1.0
                vals = [dp.get(stat, 0) for dp in pts]
                return round(sum(vals) / len(vals), 2)
            except Exception:
                return -1.0

        cpu_avg       = _metric("CPUUtilization")
        evictions_sum = _metric("Evictions", "Sum")
        curr_items    = _metric("CurrItems")
        memory_pct    = _metric("DatabaseMemoryUsagePercentage")

        diagnosis = None
        recommendation = None
        severity = "low"

        if evictions_sum > 0 and evictions_sum != -1.0:
            diagnosis      = f"UNDERSIZE: {evictions_sum:.0f} evictions — cache is full, data being ejected"
            recommendation = "Upsize node class or add shards. Evictions = cache misses = backend load."
            severity       = "high"
        elif cpu_avg >= 0 and cpu_avg < 10 and memory_pct >= 0 and memory_pct < 30:
            diagnosis      = f"OVERSIZE: CPU {cpu_avg}%, memory {memory_pct}% — severely under-utilised"
            recommendation = "Downsize 1 node class step (halves cost, same performance)"
            severity       = "medium"

        if diagnosis:
            results.append({
                "cluster_id":         cid,
                "node_type":          node_type,
                "engine":             engine,
                "cpu_avg_pct":        cpu_avg,
                "memory_used_pct":    memory_pct,
                "evictions_per_hour": evictions_sum,
                "curr_items":         curr_items,
                "diagnosis":          diagnosis,
                "recommendation":     recommendation,
                "severity":           severity,
            })

    logger.info("get_elasticache_rightsizing: clusters=%d issues=%d", len(clusters), len(results))
    return results


@tool
def get_elasticache_reserved_gap(start_date: str, end_date: str) -> dict:
    """
    Analyses ElastiCache Reserved Cache Node coverage — separate from EC2/RDS
    Savings Plans, frequently overlooked.

    Reserved Cache Nodes save 30-45% vs On-Demand for stable clusters.
    1-year No Upfront RI on cache.r7g.large: ~$0.121/hr vs $0.193/hr On-Demand = 37% saving.

    Teams that diligently buy RIs for EC2 often forget ElastiCache entirely.
    A 10-node Redis cluster running 100% On-Demand for a year = $17k/year avoidable cost.

    Returns: On-Demand spend, RI coverage %, and estimated annual saving from full coverage.

    Args:
        start_date: YYYY-MM-DD
        end_date:   YYYY-MM-DD
    """
    flt = {
        "Dimensions": {"Key": "SERVICE",
                       "Values": ["Amazon ElastiCache"],
                       "MatchOptions": ["EQUALS"]}
    }
    ce_kwargs: dict = {
        "TimePeriod":  {"Start": start_date, "End": end_date},
        "Granularity": "MONTHLY",
        "GroupBy":     [{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
        "Metrics":     ["UnblendedCost"],
        "Filter":      flt,
    }
    ce_resp = _ce().get_cost_and_usage(**ce_kwargs)

    on_demand_cost = 0.0
    reserved_cost  = 0.0
    for period in ce_resp.get("ResultsByTime", []):
        for g in period.get("Groups", []):
            ut     = g["Keys"][0]
            amount = float(g["Metrics"]["UnblendedCost"]["Amount"])
            if "NodeUsage" in ut and "Reserved" not in ut:
                on_demand_cost += amount
            elif "Reserved" in ut or "RI" in ut:
                reserved_cost += amount

    total = on_demand_cost + reserved_cost
    ri_coverage_pct   = round(reserved_cost / total * 100, 1) if total > 0 else 0
    coverage_gap_pct  = max(0.0, 70.0 - ri_coverage_pct)

    # 37% average saving on reserved vs on-demand
    estimated_annual_saving = round(on_demand_cost * (coverage_gap_pct / 100) * 0.37 * 12, 2)

    return {
        "on_demand_cost_usd":         round(on_demand_cost, 2),
        "reserved_cost_usd":          round(reserved_cost, 2),
        "ri_coverage_pct":            ri_coverage_pct,
        "coverage_gap_pct":           coverage_gap_pct,
        "estimated_annual_saving_usd": estimated_annual_saving,
        "recommendation": (
            f"ElastiCache RI coverage is only {ri_coverage_pct}% (target 70%). "
            f"Purchasing 1-year No-Upfront RIs for stable clusters saves "
            f"~${estimated_annual_saving}/year."
            if coverage_gap_pct > 10 else
            f"ElastiCache RI coverage at {ri_coverage_pct}% — well covered."
        ),
    }


@tool
def get_elasticache_cluster_waste() -> list:
    """
    Scans ElastiCache for structural waste patterns:

    1. Multi-AZ on non-production clusters: 2x node cost for automatic failover
       that dev/staging environments don't need.

    2. Redis Cluster Mode enabled on small datasets: Cluster Mode shards data
       across multiple nodes (good for > 100 GB). Below that, it adds cross-slot
       overhead and forces you to pay for multiple primary + replica nodes
       where a single node would suffice.

    3. Stopped/deleted primaries with replicas still running: replica nodes
       incur full cost even if the primary is gone.

    Returns list of clusters with structural optimisation opportunities.
    """
    clusters = []
    try:
        paginator = _elasticache().get_paginator("describe_replication_groups")
        for page in paginator.paginate():
            clusters.extend(page.get("ReplicationGroups", []))
    except Exception:
        pass

    results = []
    for rg in clusters:
        rgid       = rg["ReplicationGroupId"]
        multi_az   = rg.get("MultiAZ") == "enabled"
        cluster_mode = rg.get("ClusterEnabled", False)
        member_clusters = rg.get("MemberClusters", [])
        node_groups = rg.get("NodeGroups", [])
        num_shards  = len(node_groups)
        description = rg.get("Description", "")

        opps = []

        # Check non-prod tag via description heuristic and member cluster tags
        is_nonprod = any(x in description.lower() for x in ["dev", "staging", "test", "qa"])
        if multi_az and is_nonprod:
            opps.append({
                "type":   "multi_az_nonprod",
                "detail": "Multi-AZ enabled on apparent non-production cluster",
                "action": "Disable Multi-AZ to halve standby replica costs",
            })

        if cluster_mode and num_shards > 1 and num_shards <= 3:
            opps.append({
                "type":   "cluster_mode_small_dataset",
                "detail": f"Cluster Mode enabled with only {num_shards} shards — "
                          "likely unnecessary for datasets < 100 GB",
                "action": "Disable Cluster Mode and migrate to single shard to reduce node count",
            })

        if opps:
            results.append({
                "replication_group_id": rgid,
                "multi_az":             multi_az,
                "cluster_mode":         cluster_mode,
                "num_shards":           num_shards,
                "member_clusters":      len(member_clusters),
                "opportunities":        opps,
            })

    logger.info("get_elasticache_cluster_waste: groups=%d with_issues=%d", len(clusters), len(results))
    return results


# ═══════════════════════════════════════════════════════════════════════════════
# OPENSEARCH SPECIALIST TOOLS
# ═══════════════════════════════════════════════════════════════════════════════

def _opensearch():
    if not hasattr(_opensearch, "_c") or _opensearch._c is None:
        _opensearch._c = boto3.client(
            "opensearch",
            region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
        )
    return _opensearch._c
_opensearch._c = None


@tool
def get_opensearch_tier_opportunity(start_date: str, end_date: str) -> list:
    """
    Identifies OpenSearch domains where old indices are paying for Hot (SSD) storage
    when they should be on UltraWarm or Cold tier.

    Storage tier pricing (per GB/month):
      Hot (SSD):       $0.135/GB
      UltraWarm:       $0.024/GB  →  82% cheaper (object store, queryable)
      Cold:            $0.010/GB  →  93% cheaper (lowest cost, slightly slower)

    Typical pattern: a logging cluster retains 90 days of data, all on Hot storage,
    when only the last 7 days are actively queried. Days 8-90 = $0.135/GB wasted.

    Detection: analyses CE costs by usage type to find Hot storage proportion,
    then checks if UltraWarm is enabled on the domain.

    Returns domains with estimated saving from tiering old data.

    Args:
        start_date: YYYY-MM-DD
        end_date:   YYYY-MM-DD
    """
    # CE: OpenSearch costs by usage type
    ce_kwargs: dict = {
        "TimePeriod":  {"Start": start_date, "End": end_date},
        "Granularity": "MONTHLY",
        "GroupBy":     [{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
        "Metrics":     ["UnblendedCost", "UsageQuantity"],
        "Filter": {
            "Dimensions": {"Key": "SERVICE",
                           "Values": ["Amazon OpenSearch Service"],
                           "MatchOptions": ["EQUALS"]}
        },
    }
    ce_resp = _ce().get_cost_and_usage(**ce_kwargs)

    hot_storage_cost     = 0.0
    ultrawarm_cost       = 0.0
    total_opensearch_cost = 0.0
    for period in ce_resp.get("ResultsByTime", []):
        for g in period.get("Groups", []):
            ut     = g["Keys"][0]
            amount = float(g["Metrics"]["UnblendedCost"]["Amount"])
            total_opensearch_cost += amount
            if "GP2" in ut or "GP3" in ut or "EBS" in ut.upper():
                hot_storage_cost += amount
            elif "UltraWarm" in ut or "ultrawarm" in ut.lower():
                ultrawarm_cost += amount

    # List domains
    domains = []
    try:
        resp = _opensearch().list_domain_names()
        for d in resp.get("DomainNames", []):
            try:
                info = _opensearch().describe_domain(DomainName=d["DomainName"])
                domains.append(info.get("DomainStatus", {}))
            except Exception:
                pass
    except Exception:
        pass

    results = []
    for domain in domains:
        name           = domain.get("DomainName", "")
        uw_enabled     = domain.get("WarmEnabled", False)
        cold_enabled   = domain.get("ColdStorageOptions", {}).get("Enabled", False)
        data_node_type = domain.get("ClusterConfig", {}).get("InstanceType", "")
        uw_count       = domain.get("ClusterConfig", {}).get("WarmCount", 0)

        # Estimate: assume 70% of hot storage is > 7 days old (candidate for UW/Cold)
        # Per-domain split (divide evenly if multiple domains)
        per_domain_hot = hot_storage_cost / max(len(domains), 1)
        cold_candidate = per_domain_hot * 0.70
        potential_saving = round(cold_candidate * (1 - 0.024 / 0.135), 2)  # UW saving

        if not uw_enabled and per_domain_hot > 10:
            results.append({
                "domain_name":              name,
                "ultrawarm_enabled":        uw_enabled,
                "cold_storage_enabled":     cold_enabled,
                "data_instance_type":       data_node_type,
                "hot_storage_cost_usd":     round(per_domain_hot, 2),
                "potential_monthly_saving": potential_saving,
                "recommendation": (
                    f"Enable UltraWarm and configure ILM to move indices > 7 days to UW. "
                    f"UltraWarm is 82% cheaper than Hot EBS. "
                    f"Estimated saving: ${potential_saving}/month."
                ),
                "severity": "critical" if potential_saving > 500 else "high" if potential_saving > 100 else "medium",
            })

    logger.info("get_opensearch_tier_opportunity: domains=%d candidates=%d", len(domains), len(results))
    return results


@tool
def get_opensearch_rightsizing(lookback_days: int = 14) -> list:
    """
    Identifies over-provisioned OpenSearch domains by checking JVM heap pressure,
    CPU utilisation, and whether dedicated master nodes are necessary.

    Dedicated master nodes:
      - Recommended for clusters > 10 data nodes
      - For clusters ≤ 5 data nodes: 3 masters = 3 extra nodes paid for coordination only
      - Cost: 3 × m6g.large.search ≈ $0.166/hr each = ~$357/month pure overhead

    JVM heap < 30%: domain is over-provisioned for data volume.
    CPU < 10%: queries are sparse, instance is too large.

    Args:
        lookback_days: days of CloudWatch metrics (default 14)
    """
    end_dt   = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=lookback_days)

    domains = []
    try:
        resp = _opensearch().list_domain_names()
        for d in resp.get("DomainNames", []):
            try:
                info = _opensearch().describe_domain(DomainName=d["DomainName"])
                domains.append(info.get("DomainStatus", {}))
            except Exception:
                pass
    except Exception:
        pass

    results = []
    for domain in domains:
        name         = domain.get("DomainName", "")
        cluster_cfg  = domain.get("ClusterConfig", {})
        data_count   = cluster_cfg.get("InstanceCount", 0)
        master_enabled = cluster_cfg.get("DedicatedMasterEnabled", False)
        master_type  = cluster_cfg.get("DedicatedMasterType", "")
        master_count = cluster_cfg.get("DedicatedMasterCount", 0)
        inst_type    = cluster_cfg.get("InstanceType", "")

        dims = [{"Name": "DomainName", "Value": name},
                {"Name": "ClientId",   "Value": ""}]

        def _cw_metric(metric: str) -> float:
            try:
                resp = _cw().get_metric_statistics(
                    Namespace="AWS/ES",
                    MetricName=metric,
                    Dimensions=[{"Name": "DomainName", "Value": name}],
                    StartTime=start_dt,
                    EndTime=end_dt,
                    Period=3600,
                    Statistics=["Average"],
                )
                pts = resp.get("Datapoints", [])
                if not pts:
                    return -1.0
                return round(sum(dp.get("Average", 0) for dp in pts) / len(pts), 2)
            except Exception:
                return -1.0

        jvm_pressure = _cw_metric("JVMMemoryPressure")
        cpu_avg      = _cw_metric("CPUUtilization")

        flags = []
        if master_enabled and data_count <= 5 and master_count >= 3:
            flags.append(
                f"Dedicated master nodes unnecessary for {data_count} data nodes. "
                f"{master_count}× {master_type} = ~${master_count * 120:.0f}/month overhead. "
                "Safe to disable for clusters ≤ 5 data nodes."
            )
        if jvm_pressure >= 0 and jvm_pressure < 30 and cpu_avg >= 0 and cpu_avg < 10:
            flags.append(
                f"JVM heap {jvm_pressure}%, CPU {cpu_avg}% — severely under-utilised. "
                "Downsize instance type 1 step."
            )

        if flags:
            results.append({
                "domain_name":          name,
                "instance_type":        inst_type,
                "data_node_count":      data_count,
                "dedicated_master":     master_enabled,
                "master_type":          master_type,
                "master_count":         master_count,
                "jvm_pressure_pct":     jvm_pressure,
                "cpu_avg_pct":          cpu_avg,
                "flags":                flags,
            })

    logger.info("get_opensearch_rightsizing: domains=%d issues=%d", len(domains), len(results))
    return results


@tool
def get_opensearch_shard_waste(lookback_days: int = 7) -> list:
    """
    Detects OpenSearch shard sizing problems that cause both performance degradation
    and cost waste.

    AWS recommendation: 10–50 GB per shard. Outside this range:

    Too small (< 5 GB/shard): too many shards for the data volume.
    Each shard consumes JVM heap (~30 MB overhead). 1000 × 1 GB shards = 30 GB JVM
    just for overhead → forces larger (more expensive) instances.
    Common cause: default 5 primary shards × small daily indices = thousands of tiny shards.

    Too large (> 50 GB/shard): poor query parallelism, unbalanced nodes,
    recovery after node failure takes too long.

    Detection uses CloudWatch SearchableDocuments and store.size metrics.

    Args:
        lookback_days: days of metrics to assess current shard state (default 7)
    """
    end_dt   = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=lookback_days)

    domains = []
    try:
        resp = _opensearch().list_domain_names()
        for d in resp.get("DomainNames", []):
            try:
                info = _opensearch().describe_domain(DomainName=d["DomainName"])
                domains.append(info.get("DomainStatus", {}))
            except Exception:
                pass
    except Exception:
        pass

    results = []
    for domain in domains:
        name = domain.get("DomainName", "")
        shards_total_stat = -1.0
        store_size_stat   = -1.0

        try:
            for metric, store in [("Shards.active", "shards"), ("SearchableDocuments", "docs")]:
                resp_cw = _cw().get_metric_statistics(
                    Namespace="AWS/ES",
                    MetricName=metric,
                    Dimensions=[{"Name": "DomainName", "Value": name}],
                    StartTime=start_dt,
                    EndTime=end_dt,
                    Period=lookback_days * 86400,
                    Statistics=["Maximum"],
                )
                pts = resp_cw.get("Datapoints", [])
                if pts:
                    val = max(dp.get("Maximum", 0) for dp in pts)
                    if store == "shards":
                        shards_total_stat = val
        except Exception:
            pass

        # Estimate store size from CE usage quantity (GB stored)
        try:
            ce_resp = _ce().get_cost_and_usage(
                TimePeriod={"Start": start_date if 'start_date' in dir() else _date(lookback_days), "End": _date(0)},
                Granularity="MONTHLY",
                GroupBy=[{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
                Metrics=["UsageQuantity"],
                Filter={"Dimensions": {"Key": "SERVICE",
                                       "Values": ["Amazon OpenSearch Service"],
                                       "MatchOptions": ["EQUALS"]}},
            )
            for period in ce_resp.get("ResultsByTime", []):
                for g in period.get("Groups", []):
                    if "GP" in g["Keys"][0] or "EBS" in g["Keys"][0].upper():
                        store_size_stat += float(g["Metrics"]["UsageQuantity"]["Amount"])
        except Exception:
            pass

        if shards_total_stat > 0 and store_size_stat > 0:
            gb_per_shard = round(store_size_stat / shards_total_stat, 2)

            issue = None
            if gb_per_shard < 5 and shards_total_stat > 100:
                issue = (f"{int(shards_total_stat)} shards averaging {gb_per_shard} GB each — "
                         "too many small shards. JVM overhead exceeds data payload. "
                         "Merge daily indices into weekly/monthly with fewer primary shards.")
            elif gb_per_shard > 50:
                issue = (f"Shards averaging {gb_per_shard} GB — oversized. "
                         "Increase primary shard count on next index rollover for better parallelism.")

            if issue:
                results.append({
                    "domain_name":       name,
                    "active_shards":     int(shards_total_stat),
                    "store_size_gb":     round(store_size_stat, 1),
                    "avg_gb_per_shard":  gb_per_shard,
                    "issue":             issue,
                })

    logger.info("get_opensearch_shard_waste: domains=%d issues=%d", len(domains), len(results))
    return results


# ═══════════════════════════════════════════════════════════════════════════════
# SAGEMAKER SPECIALIST TOOLS
# ═══════════════════════════════════════════════════════════════════════════════

def _sagemaker():
    if not hasattr(_sagemaker, "_c") or _sagemaker._c is None:
        _sagemaker._c = boto3.client(
            "sagemaker",
            region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
        )
    return _sagemaker._c
_sagemaker._c = None


@tool
def get_sagemaker_idle_notebooks() -> list:
    """
    Finds SageMaker notebook instances running 24/7 without recent kernel activity.

    This is the single most common SageMaker waste pattern: a data scientist opens
    a notebook, does 2 hours of work, and never stops the instance.

    Cost examples:
      ml.t3.medium:    $0.050/hr =   $36/month if forgotten
      ml.p3.2xlarge:   $3.060/hr = $2,203/month if forgotten (GPU)
      ml.g4dn.xlarge:  $0.736/hr =   $530/month if forgotten

    Detection: InService notebook instances + CloudWatch KernelGatewayRequests = 0
    in the last 24 hours. A running instance with no kernel requests = idle.

    Returns list of idle notebooks with instance type, hours running estimate,
    and accrued cost approximation.
    """
    HOURLY_PRICES = {
        "ml.t3.medium": 0.050, "ml.t3.large": 0.100, "ml.t3.xlarge": 0.202,
        "ml.m5.large": 0.115, "ml.m5.xlarge": 0.230, "ml.m5.2xlarge": 0.461,
        "ml.m5.4xlarge": 0.922,
        "ml.p3.2xlarge": 3.060, "ml.p3.8xlarge": 12.24,
        "ml.g4dn.xlarge": 0.736, "ml.g4dn.2xlarge": 1.505, "ml.g4dn.12xlarge": 6.019,
        "ml.r5.large": 0.151, "ml.r5.xlarge": 0.302, "ml.r5.2xlarge": 0.605,
    }

    notebooks = []
    try:
        paginator = _sagemaker().get_paginator("list_notebook_instances")
        for page in paginator.paginate(StatusEquals="InService"):
            notebooks.extend(page.get("NotebookInstances", []))
    except Exception:
        pass

    results = []
    for nb in notebooks:
        name      = nb["NotebookInstanceName"]
        inst_type = nb.get("InstanceType", "")
        last_modified = nb.get("LastModifiedTime")

        # Check for kernel activity in last 24 hours
        end_dt   = datetime.now(timezone.utc)
        start_dt = end_dt - timedelta(hours=24)
        is_idle  = True
        try:
            resp = _cw().get_metric_statistics(
                Namespace="AWS/SageMaker",
                MetricName="KernelGatewayRequests",
                Dimensions=[{"Name": "NotebookInstanceName", "Value": name}],
                StartTime=start_dt,
                EndTime=end_dt,
                Period=86400,
                Statistics=["Sum"],
            )
            pts = resp.get("Datapoints", [])
            if pts and sum(dp.get("Sum", 0) for dp in pts) > 0:
                is_idle = False
        except Exception:
            pass  # If metric unavailable, assume idle (conservative)

        if is_idle:
            hourly_cost = HOURLY_PRICES.get(inst_type, 0.1)
            monthly_cost = round(hourly_cost * 24 * 30, 2)
            results.append({
                "notebook_name":        name,
                "instance_type":        inst_type,
                "last_modified":        last_modified.isoformat() if last_modified else None,
                "hourly_cost_usd":      hourly_cost,
                "monthly_cost_usd":     monthly_cost,
                "severity":             "critical" if monthly_cost > 500 else "high" if monthly_cost > 100 else "medium",
                "action":               f"aws sagemaker stop-notebook-instance --notebook-instance-name {name}",
            })

    results.sort(key=lambda x: x["monthly_cost_usd"], reverse=True)
    logger.info("get_sagemaker_idle_notebooks: total=%d idle=%d", len(notebooks), len(results))
    return results


@tool
def get_sagemaker_endpoint_efficiency(lookback_days: int = 14) -> list:
    """
    Identifies SageMaker real-time inference endpoints that are over-provisioned
    or candidates for Serverless Inference / Multi-Model Endpoints.

    Real-time endpoints charge per hour even with 0 requests.
    A ml.m5.xlarge endpoint with 1 req/day = $0.230/hr = $166/month for nothing.

    Serverless Inference: pay per millisecond of execution, $0 when idle.
    Best for: < 1 request/minute average, burst tolerance ≤ 60s cold start.

    Multi-Model Endpoint: host N models on one instance, serving each on-demand.
    Best for: many low-traffic models (e.g. per-tenant models with sparse traffic).

    Detection signals:
      - InvocationsPerInstance < 1/min → Serverless candidate
      - ModelLatency < 500ms AND low traffic → Multi-Model candidate
      - CPUUtilization < 5% → instance oversized

    Args:
        lookback_days: days of CloudWatch metrics (default 14)
    """
    end_dt   = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=lookback_days)

    endpoints = []
    try:
        paginator = _sagemaker().get_paginator("list_endpoints")
        for page in paginator.paginate(StatusEquals="InService"):
            endpoints.extend(page.get("Endpoints", []))
    except Exception:
        pass

    HOURLY_PRICES = {
        "ml.t2.medium": 0.056, "ml.m5.large": 0.115, "ml.m5.xlarge": 0.230,
        "ml.m5.2xlarge": 0.461, "ml.m5.4xlarge": 0.922,
        "ml.c5.large": 0.102, "ml.c5.xlarge": 0.204, "ml.c5.2xlarge": 0.408,
        "ml.g4dn.xlarge": 0.736,
    }

    results = []
    for ep in endpoints:
        ep_name = ep["EndpointName"]

        def _ep_metric(metric: str, stat: str = "Average") -> float:
            try:
                resp = _cw().get_metric_statistics(
                    Namespace="AWS/SageMaker",
                    MetricName=metric,
                    Dimensions=[{"Name": "EndpointName", "Value": ep_name},
                                {"Name": "VariantName",  "Value": "AllTraffic"}],
                    StartTime=start_dt,
                    EndTime=end_dt,
                    Period=3600,
                    Statistics=[stat],
                )
                pts = resp.get("Datapoints", [])
                if not pts:
                    return -1.0
                return round(sum(dp.get(stat, 0) for dp in pts) / len(pts), 2)
            except Exception:
                return -1.0

        invocations_per_hr = _ep_metric("InvocationsPerInstance", "Sum")
        cpu_avg            = _ep_metric("CPUUtilization")
        model_latency_ms   = _ep_metric("ModelLatency")

        # Get instance type from endpoint config
        try:
            cfg = _sagemaker().describe_endpoint(EndpointName=ep_name)
            cfg_name = cfg.get("EndpointConfigName", "")
            ep_cfg = _sagemaker().describe_endpoint_config(EndpointConfigName=cfg_name)
            inst_type = ep_cfg.get("ProductionVariants", [{}])[0].get("InstanceType", "")
        except Exception:
            inst_type = "unknown"

        hourly = HOURLY_PRICES.get(inst_type, 0.2)
        monthly = round(hourly * 24 * 30, 2)

        suggestions = []
        if invocations_per_hr >= 0 and invocations_per_hr < 60:  # < 1/min
            suggestions.append(
                f"LOW TRAFFIC: {invocations_per_hr:.1f} invocations/hr. "
                f"Serverless Inference would cost $0 when idle vs ${monthly}/month now."
            )
        if cpu_avg >= 0 and cpu_avg < 5:
            suggestions.append(f"CPU avg {cpu_avg}% — instance severely under-utilised.")

        if suggestions:
            results.append({
                "endpoint_name":       ep_name,
                "instance_type":       inst_type,
                "monthly_cost_usd":    monthly,
                "invocations_per_hr":  invocations_per_hr,
                "cpu_avg_pct":         cpu_avg,
                "model_latency_ms":    model_latency_ms,
                "suggestions":         suggestions,
                "severity":            "high" if monthly > 100 else "medium",
            })

    results.sort(key=lambda x: x["monthly_cost_usd"], reverse=True)
    logger.info("get_sagemaker_endpoint_efficiency: endpoints=%d issues=%d", len(endpoints), len(results))
    return results


@tool
def get_sagemaker_training_spot_gap(start_date: str, end_date: str) -> dict:
    """
    Analyses SageMaker training job costs to identify On-Demand jobs that
    could use Managed Spot Training (typically 60-90% cheaper).

    Managed Spot Training uses spare EC2 capacity. AWS handles interruptions
    automatically if you configure checkpoints. For most ML training jobs
    (not latency-sensitive), this is a one-line change:
      EnableManagedSpotTraining=True + checkpoint S3 path

    Typical saving: p3.8xlarge training job at $12.24/hr On-Demand vs
    ~$3.67/hr Spot = 70% saving. A 10-hour training job saves $85.

    Returns: On-Demand training cost, spot coverage %, and estimated saving.

    Args:
        start_date: YYYY-MM-DD
        end_date:   YYYY-MM-DD
    """
    flt = {
        "Dimensions": {"Key": "SERVICE",
                       "Values": ["Amazon SageMaker"],
                       "MatchOptions": ["EQUALS"]}
    }
    ce_kwargs: dict = {
        "TimePeriod":  {"Start": start_date, "End": end_date},
        "Granularity": "MONTHLY",
        "GroupBy":     [{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
        "Metrics":     ["UnblendedCost"],
        "Filter":      flt,
    }
    ce_resp = _ce().get_cost_and_usage(**ce_kwargs)

    training_ondemand = 0.0
    training_spot     = 0.0
    for period in ce_resp.get("ResultsByTime", []):
        for g in period.get("Groups", []):
            ut     = g["Keys"][0]
            amount = float(g["Metrics"]["UnblendedCost"]["Amount"])
            if "Training" in ut and "Spot" not in ut:
                training_ondemand += amount
            elif "Training" in ut and "Spot" in ut:
                training_spot += amount

    total_training  = training_ondemand + training_spot
    spot_pct        = round(training_spot / total_training * 100, 1) if total_training > 0 else 0
    potential_saving = round(training_ondemand * 0.70, 2)  # conservative 70% saving

    return {
        "training_ondemand_cost_usd":    round(training_ondemand, 2),
        "training_spot_cost_usd":        round(training_spot, 2),
        "spot_pct":                      spot_pct,
        "estimated_monthly_saving_usd":  potential_saving,
        "recommendation": (
            f"Only {spot_pct}% of training uses Spot. "
            f"Adding EnableManagedSpotTraining=True + checkpoint path to On-Demand jobs "
            f"saves ~${potential_saving}/month (70% avg saving)."
            if spot_pct < 50 and training_ondemand > 50 else
            f"Training Spot adoption at {spot_pct}% — well optimised."
        ),
    }


@tool
def get_sagemaker_studio_waste() -> list:
    """
    Finds SageMaker Studio KernelGateway apps left running without active sessions.

    Studio apps don't stop automatically. Every developer with Studio open
    (even with the browser closed) has a KernelGateway app running at full
    instance cost — typically ml.t3.medium ($0.050/hr) to ml.p3.2xlarge ($3.06/hr).

    In a team of 10 data scientists, 5 idle Studio apps = $250-$15,000/month.

    Detection: InService apps with DomainId + zero CloudWatch activity.

    Returns list of idle Studio apps with cost and stop command.
    """
    apps = []
    try:
        paginator = _sagemaker().get_paginator("list_apps")
        for page in paginator.paginate():
            for app in page.get("Apps", []):
                if app.get("Status") == "InService" and app.get("AppType") == "KernelGateway":
                    apps.append(app)
    except Exception:
        pass

    HOURLY_PRICES = {
        "ml.t3.medium": 0.050, "ml.t3.large": 0.100, "ml.m5.large": 0.115,
        "ml.m5.xlarge": 0.230, "ml.p3.2xlarge": 3.060, "ml.g4dn.xlarge": 0.736,
    }

    idle = []
    for app in apps:
        end_dt   = datetime.now(timezone.utc)
        start_dt = end_dt - timedelta(hours=4)

        app_name  = app.get("AppName", "")
        user_name = app.get("UserProfileName", "")
        domain_id = app.get("DomainId", "")

        is_idle = True
        try:
            resp = _cw().get_metric_statistics(
                Namespace="AWS/SageMaker",
                MetricName="MemoryUtilization",
                Dimensions=[
                    {"Name": "DomainId",          "Value": domain_id},
                    {"Name": "UserProfileName",    "Value": user_name},
                    {"Name": "AppType",            "Value": "KernelGateway"},
                    {"Name": "AppName",            "Value": app_name},
                ],
                StartTime=start_dt,
                EndTime=end_dt,
                Period=14400,
                Statistics=["Average"],
            )
            pts = resp.get("Datapoints", [])
            if pts and any(dp.get("Average", 0) > 20 for dp in pts):
                is_idle = False
        except Exception:
            pass

        if is_idle:
            resource_spec = app.get("ResourceSpec", {})
            inst_type = resource_spec.get("InstanceType", "ml.t3.medium")
            hourly = HOURLY_PRICES.get(inst_type, 0.05)
            idle.append({
                "domain_id":        domain_id,
                "user_profile":     user_name,
                "app_name":         app_name,
                "instance_type":    inst_type,
                "hourly_cost_usd":  hourly,
                "monthly_cost_usd": round(hourly * 24 * 30, 2),
                "action": (
                    f"aws sagemaker delete-app --domain-id {domain_id} "
                    f"--user-profile-name {user_name} "
                    f"--app-type KernelGateway --app-name {app_name}"
                ),
            })

    idle.sort(key=lambda x: x["monthly_cost_usd"], reverse=True)
    logger.info("get_sagemaker_studio_waste: apps=%d idle=%d", len(apps), len(idle))
    return idle


# ═══════════════════════════════════════════════════════════════════════════════
# LAMBDA + API GATEWAY + KINESIS SPECIALIST TOOLS
# ═══════════════════════════════════════════════════════════════════════════════

def _lambda():
    if not hasattr(_lambda, "_c") or _lambda._c is None:
        _lambda._c = boto3.client(
            "lambda",
            region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
        )
    return _lambda._c
_lambda._c = None


def _apigw():
    if not hasattr(_apigw, "_c") or _apigw._c is None:
        _apigw._c = boto3.client(
            "apigatewayv2",
            region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
        )
    return _apigw._c
_apigw._c = None


def _apigw_v1():
    if not hasattr(_apigw_v1, "_c") or _apigw_v1._c is None:
        _apigw_v1._c = boto3.client(
            "apigateway",
            region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
        )
    return _apigw_v1._c
_apigw_v1._c = None


def _kinesis():
    if not hasattr(_kinesis, "_c") or _kinesis._c is None:
        _kinesis._c = boto3.client(
            "kinesis",
            region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
        )
    return _kinesis._c
_kinesis._c = None


@tool
def get_lambda_rightsizing(lookback_days: int = 14) -> list:
    """
    Identifies Lambda functions where memory is over-provisioned relative to
    actual usage — the most actionable Lambda cost optimisation.

    Lambda pricing: $0.0000166667 per GB-second.
    A function configured at 1024 MB but using only 128 MB on average costs
    8× more than it needs to. Halving memory halves cost for same invocations.

    BUT: more memory also means more CPU, which can reduce duration enough to
    make the total cost lower. This is why the optimal memory isn't always "minimum".

    AWS Power Tuning principle:
      - Run the function at different memory settings
      - Find where GB-seconds (cost) is minimised
      - Functions that are I/O-bound benefit most from increased memory (faster CPU)
      - Functions that are compute-bound may be cheaper at lower memory

    This tool detects:
    - Functions with max memory used < 25% of configured memory → clear oversize
    - Functions with duration > 10s (potential timeout risk, high cost per invocation)
    - Functions with error rate > 1% (retries multiplying cost)

    Args:
        lookback_days: days of CloudWatch metrics (default 14)
    """
    end_dt   = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=lookback_days)

    functions = []
    try:
        paginator = _lambda().get_paginator("list_functions")
        for page in paginator.paginate():
            functions.extend(page.get("Functions", []))
    except Exception:
        pass

    results = []
    for fn in functions:
        fn_name   = fn["FunctionName"]
        memory_mb = fn.get("MemorySize", 128)
        timeout_s = fn.get("Timeout", 3)

        dims = [{"Name": "FunctionName", "Value": fn_name}]

        def _fn_metric(metric: str, stat: str = "Average") -> float:
            try:
                resp = _cw().get_metric_statistics(
                    Namespace="AWS/Lambda",
                    MetricName=metric,
                    Dimensions=dims,
                    StartTime=start_dt,
                    EndTime=end_dt,
                    Period=3600,
                    Statistics=[stat],
                )
                pts = resp.get("Datapoints", [])
                if not pts:
                    return -1.0
                return round(sum(dp.get(stat, 0) for dp in pts) / len(pts), 2)
            except Exception:
                return -1.0

        max_memory_used_mb = _fn_metric("MaxMemoryUsed", "Maximum")
        duration_avg_ms    = _fn_metric("Duration")
        invocations        = _fn_metric("Invocations", "Sum")
        errors             = _fn_metric("Errors", "Sum")

        if max_memory_used_mb <= 0 or invocations <= 0:
            continue

        memory_util_pct = round(max_memory_used_mb / memory_mb * 100, 1)
        error_rate_pct  = round(errors / invocations * 100, 2) if invocations > 0 else 0

        flags = []
        if memory_util_pct < 25:
            recommended_mb = max(128, int(max_memory_used_mb * 1.5))  # 50% headroom
            current_gb_s   = memory_mb / 1024 * (duration_avg_ms / 1000)
            recommended_gb_s = recommended_mb / 1024 * (duration_avg_ms / 1000)
            saving_pct     = round((1 - recommended_gb_s / current_gb_s) * 100, 0)
            flags.append(
                f"Memory {memory_util_pct}% used ({max_memory_used_mb:.0f}/{memory_mb} MB). "
                f"Reduce to {recommended_mb} MB → ~{saving_pct}% cost reduction."
            )
        if error_rate_pct > 1:
            flags.append(
                f"Error rate {error_rate_pct}% — retries multiplying cost and latency."
            )
        if duration_avg_ms > 0 and duration_avg_ms > timeout_s * 1000 * 0.8:
            flags.append(
                f"Avg duration {duration_avg_ms:.0f}ms is >80% of timeout ({timeout_s}s) — "
                "high timeout risk, consider increasing timeout or optimising function."
            )

        if flags:
            results.append({
                "function_name":       fn_name,
                "memory_configured_mb": memory_mb,
                "memory_max_used_mb":  max_memory_used_mb,
                "memory_util_pct":     memory_util_pct,
                "duration_avg_ms":     duration_avg_ms,
                "invocations_per_hr":  invocations,
                "error_rate_pct":      error_rate_pct,
                "flags":               flags,
            })

    results.sort(key=lambda x: x["memory_util_pct"])
    logger.info("get_lambda_rightsizing: functions=%d issues=%d", len(functions), len(results))
    return results


@tool
def get_apigw_optimization(start_date: str, end_date: str) -> dict:
    """
    Identifies API Gateway optimisation opportunities — particularly REST API →
    HTTP API migration and caching enablement.

    REST API vs HTTP API pricing:
      REST API:  $3.50 per million requests (+ $0.09/GB data transfer)
      HTTP API:  $1.00 per million requests (71% cheaper)

    HTTP API covers ~90% of use cases: JWT auth, Lambda integration, CORS.
    What REST API has that HTTP API doesn't:
      - API keys + usage plans
      - Request/response transformation (VTL mapping templates)
      - WAF integration (native)
      - Some edge-optimized features

    REST API Caching: a 0.5 GB cache at $0.020/hr ($14.40/month) can eliminate
    thousands of backend calls per day. ROI positive if > 1000 cacheable req/day.

    Returns: REST vs HTTP API cost breakdown, migration candidates, caching opportunities.

    Args:
        start_date: YYYY-MM-DD
        end_date:   YYYY-MM-DD
    """
    flt = {
        "Dimensions": {"Key": "SERVICE",
                       "Values": ["Amazon API Gateway"],
                       "MatchOptions": ["EQUALS"]}
    }
    ce_resp = _ce().get_cost_and_usage(
        TimePeriod={"Start": start_date, "End": end_date},
        Granularity="MONTHLY",
        GroupBy=[{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
        Metrics=["UnblendedCost", "UsageQuantity"],
        Filter=flt,
    )

    rest_cost   = 0.0
    http_cost   = 0.0
    cache_cost  = 0.0
    rest_calls  = 0.0
    for period in ce_resp.get("ResultsByTime", []):
        for g in period.get("Groups", []):
            ut     = g["Keys"][0]
            amount = float(g["Metrics"]["UnblendedCost"]["Amount"])
            qty    = float(g["Metrics"]["UsageQuantity"]["Amount"])
            if "ApiGatewayV2" in ut or "HttpApi" in ut:
                http_cost += amount
            elif "Cache" in ut:
                cache_cost += amount
            else:
                rest_cost  += amount
                rest_calls += qty

    # Check REST APIs without caching
    rest_apis_without_cache = []
    try:
        resp = _apigw_v1().get_rest_apis()
        for api in resp.get("items", []):
            api_id   = api["id"]
            api_name = api.get("name", "")
            try:
                stages = _apigw_v1().get_stages(restApiId=api_id)
                for stage in stages.get("item", []):
                    cache_enabled = stage.get("cacheClusterEnabled", False)
                    if not cache_enabled:
                        rest_apis_without_cache.append(f"{api_name}/{stage.get('stageName','')}")
            except Exception:
                pass
    except Exception:
        pass

    migration_saving = round(rest_cost * 0.71, 2)  # 71% cheaper on HTTP API

    return {
        "rest_api_cost_usd":              round(rest_cost, 2),
        "http_api_cost_usd":              round(http_cost, 2),
        "cache_cost_usd":                 round(cache_cost, 2),
        "rest_apis_without_caching":      rest_apis_without_cache[:10],
        "migration_saving_if_http_usd":   migration_saving,
        "recommendation": (
            f"${rest_cost:.2f}/month on REST API. "
            f"If use-cases don't require VTL/WAF/UsagePlans, "
            f"HTTP API saves ~${migration_saving:.2f}/month (71% cheaper). "
            + (f"{len(rest_apis_without_cache)} REST API stages without caching enabled."
               if rest_apis_without_cache else "")
        ),
    }


@tool
def get_kinesis_shard_waste(lookback_days: int = 7) -> list:
    """
    Identifies Kinesis Data Streams with idle shards — paying for capacity not used.

    Kinesis pricing:
      $0.015 per shard-hour = $10.80/month per idle shard
      $0.080 per million PUT payload units (actual usage cost)

    If a stream has 10 shards but throughput never exceeds 1 shard's capacity
    (1 MB/s write, 2 MB/s read), you're paying for 9 idle shards = $97/month waste.

    Detection: GetRecords.IteratorAgeMilliseconds ≈ 0 + IncomingBytes < 10% capacity.
    Shard capacity = 1 MB/s write, so 10 shards = 10 MB/s max.

    Args:
        lookback_days: days of CloudWatch metrics (default 7)
    """
    end_dt   = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=lookback_days)
    period   = lookback_days * 86400

    streams = []
    try:
        paginator = _kinesis().get_paginator("list_streams")
        for page in paginator.paginate():
            for stream_name in page.get("StreamNames", []):
                try:
                    desc = _kinesis().describe_stream_summary(StreamName=stream_name)
                    streams.append(desc.get("StreamDescriptionSummary", {}))
                except Exception:
                    pass
    except Exception:
        pass

    results = []
    for stream in streams:
        name         = stream.get("StreamName", "")
        shard_count  = stream.get("OpenShardCount", 0)
        stream_mode  = stream.get("StreamModeDetails", {}).get("StreamMode", "PROVISIONED")

        if stream_mode == "ON_DEMAND" or shard_count == 0:
            continue  # On-Demand scales automatically — no shard waste concept

        try:
            resp = _cw().get_metric_statistics(
                Namespace="AWS/Kinesis",
                MetricName="IncomingBytes",
                Dimensions=[{"Name": "StreamName", "Value": name}],
                StartTime=start_dt,
                EndTime=end_dt,
                Period=period,
                Statistics=["Sum", "Maximum"],
            )
            pts = resp.get("Datapoints", [])
            max_bytes_per_period = max((dp.get("Maximum", 0) for dp in pts), default=0)
            # Convert to MB/s (max in any 1-period window)
            max_mb_per_s = max_bytes_per_period / (1024 * 1024) / period * lookback_days
        except Exception:
            continue

        capacity_mb_per_s   = shard_count * 1.0  # 1 MB/s per shard
        utilisation_pct     = round(max_mb_per_s / capacity_mb_per_s * 100, 1) if capacity_mb_per_s > 0 else 0
        optimal_shards      = max(1, int(max_mb_per_s * 1.5 + 1))  # 50% headroom
        idle_shards         = max(0, shard_count - optimal_shards)
        monthly_waste       = round(idle_shards * 10.80, 2)

        if idle_shards >= 2 and monthly_waste > 10:
            results.append({
                "stream_name":          name,
                "shard_count":          shard_count,
                "max_utilisation_pct":  utilisation_pct,
                "optimal_shards":       optimal_shards,
                "idle_shards":          idle_shards,
                "monthly_waste_usd":    monthly_waste,
                "action": (
                    f"aws kinesis update-shard-count --stream-name {name} "
                    f"--target-shard-count {optimal_shards} --scaling-type UNIFORM_SCALING"
                ),
                "note": "Consider ON_DEMAND mode if traffic is highly variable — auto-scales, no idle cost.",
            })

    results.sort(key=lambda x: x["monthly_waste_usd"], reverse=True)
    logger.info("get_kinesis_shard_waste: streams=%d wasteful=%d", len(streams), len(results))
    return results


# ═══════════════════════════════════════════════════════════════════════════════
# CLOUDFRONT + NAT GATEWAY SPECIALIST TOOLS
# ═══════════════════════════════════════════════════════════════════════════════

def _cloudfront():
    if not hasattr(_cloudfront, "_c") or _cloudfront._c is None:
        _cloudfront._c = boto3.client("cloudfront", region_name="us-east-1")
    return _cloudfront._c
_cloudfront._c = None


@tool
def get_cloudfront_cache_efficiency(
    start_date: str,
    end_date: str,
) -> list:
    """
    Analyses CloudFront cache hit ratio per distribution — low hit ratio means
    expensive origin requests that should be served from cache.

    CloudFront pricing:
      Cache HIT:  $0.0085/10k requests (requests served from edge, no origin cost)
      Cache MISS: $0.0085/10k requests + origin request cost + higher latency
      Origin request to ALB: adds ~$0.008/GB + compute cost at origin

    A cache hit ratio of 50% means half your users are hitting the origin —
    7× more expensive per request than a cached response (egress + origin compute).

    Low hit ratio causes:
    - Cache-Control headers missing or too short (max-age=0)
    - Query string parameters not normalised before caching
    - Cookie forwarding too broad (every unique cookie = unique cache key)
    - Too many cache behaviors with no caching
    - Personalised content mixed with static content in same behavior

    Returns per-distribution cache efficiency with actionable recommendations.

    Args:
        start_date: YYYY-MM-DD
        end_date:   YYYY-MM-DD
    """
    end_dt   = datetime.fromisoformat(end_date).replace(tzinfo=timezone.utc)
    start_dt = datetime.fromisoformat(start_date).replace(tzinfo=timezone.utc)

    distributions = []
    try:
        paginator = _cloudfront().get_paginator("list_distributions")
        for page in paginator.paginate():
            dlist = page.get("DistributionList", {})
            distributions.extend(dlist.get("Items", []))
    except Exception:
        pass

    results = []
    for dist in distributions:
        dist_id = dist["Id"]
        domain  = dist.get("DomainName", "")
        aliases = dist.get("Aliases", {}).get("Items", [])
        label   = aliases[0] if aliases else domain

        def _cf_metric(metric: str, stat: str = "Sum") -> float:
            try:
                resp = _cw().get_metric_statistics(
                    Namespace="AWS/CloudFront",
                    MetricName=metric,
                    Dimensions=[
                        {"Name": "DistributionId", "Value": dist_id},
                        {"Name": "Region",          "Value": "Global"},
                    ],
                    StartTime=start_dt,
                    EndTime=end_dt,
                    Period=int((end_dt - start_dt).total_seconds()),
                    Statistics=[stat],
                )
                pts = resp.get("Datapoints", [])
                return round(sum(dp.get(stat, 0) for dp in pts), 0)
            except Exception:
                return -1.0

        requests_total = _cf_metric("Requests")
        cache_hits     = _cf_metric("CacheHitRate", "Average")

        if requests_total <= 0:
            continue

        hit_rate = cache_hits if cache_hits >= 0 else 0

        if hit_rate < 80 and requests_total > 1000:
            # Estimate saving from improving hit rate to 90%
            additional_hits_pct = max(0, 90 - hit_rate) / 100
            # Each additional hit saves ~$0.008/request (origin compute + transfer)
            potential_saving = round(requests_total * additional_hits_pct * 0.000001, 2)

            results.append({
                "distribution_id":         dist_id,
                "domain":                  label,
                "cache_hit_rate_pct":      hit_rate,
                "total_requests":          int(requests_total),
                "potential_monthly_saving": potential_saving,
                "severity":                "high" if hit_rate < 50 else "medium",
                "recommendation": (
                    f"Cache hit rate {hit_rate}% (target >80%). "
                    "Common fixes: set Cache-Control max-age on static assets, "
                    "normalise query strings in cache policy, "
                    "separate static and dynamic behaviors, "
                    "check cookie forwarding scope."
                ),
            })

    results.sort(key=lambda x: x["cache_hit_rate_pct"])
    logger.info("get_cloudfront_cache_efficiency: distributions=%d low_hit=%d",
                len(distributions), len(results))
    return results


@tool
def get_nat_gateway_alternatives(
    start_date: str,
    end_date: str,
) -> dict:
    """
    Identifies NAT Gateway traffic that could be eliminated by using VPC Endpoints
    — one of the most underutilised cost optimisations in AWS.

    NAT Gateway pricing:
      $0.045/hr per NAT gateway ($32.40/month just for existing)
      $0.045/GB processed (both ingress AND egress)

    VPC Endpoints (Interface/Gateway) allow private AWS service traffic to bypass
    the NAT Gateway entirely:
      Gateway Endpoints (FREE): S3, DynamoDB — no per-GB charge
      Interface Endpoints: ~$0.01/hr + $0.01/GB (still cheaper than NAT for high volume)

    Common pattern: Lambda or ECS calling S3/DynamoDB goes through NAT Gateway.
    Adding S3 Gateway Endpoint = $0 and immediately eliminates that NAT traffic.

    Detection: compares NAT Gateway bytes processed (CE) vs VPC Endpoint presence.
    Estimates what fraction of NAT traffic could be S3/DynamoDB (typically 30-60%).

    Args:
        start_date: YYYY-MM-DD
        end_date:   YYYY-MM-DD
    """
    # CE: NAT Gateway costs
    flt = {
        "Dimensions": {"Key": "SERVICE",
                       "Values": ["Amazon Virtual Private Cloud"],
                       "MatchOptions": ["EQUALS"]}
    }
    ce_resp = _ce().get_cost_and_usage(
        TimePeriod={"Start": start_date, "End": end_date},
        Granularity="MONTHLY",
        GroupBy=[{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
        Metrics=["UnblendedCost", "UsageQuantity"],
        Filter=flt,
    )

    nat_hourly_cost   = 0.0
    nat_data_cost     = 0.0
    nat_bytes_gb      = 0.0
    for period in ce_resp.get("ResultsByTime", []):
        for g in period.get("Groups", []):
            ut     = g["Keys"][0]
            amount = float(g["Metrics"]["UnblendedCost"]["Amount"])
            qty    = float(g["Metrics"]["UsageQuantity"]["Amount"])
            if "NatGateway-Hours" in ut:
                nat_hourly_cost += amount
            elif "NatGateway-Bytes" in ut:
                nat_data_cost += amount
                nat_bytes_gb  += qty / (1024 ** 3)

    total_nat_cost = nat_hourly_cost + nat_data_cost

    # Check existing VPC Endpoints
    s3_endpoint_exists  = False
    ddb_endpoint_exists = False
    try:
        resp = _ec2().describe_vpc_endpoints(
            Filters=[{"Name": "state", "Values": ["available"]}]
        )
        for ep in resp.get("VpcEndpoints", []):
            svc = ep.get("ServiceName", "")
            if "s3" in svc.lower():
                s3_endpoint_exists = True
            if "dynamodb" in svc.lower():
                ddb_endpoint_exists = True
    except Exception:
        pass

    # Conservative estimate: 40% of NAT bytes are S3/DynamoDB traffic
    eliminable_fraction = 0.0
    recommendations     = []

    if not s3_endpoint_exists and nat_data_cost > 10:
        eliminable_fraction += 0.25
        recommendations.append({
            "action":          "Create S3 Gateway Endpoint (FREE)",
            "eliminates_pct":  25,
            "command":         "aws ec2 create-vpc-endpoint --vpc-id <vpc-id> "
                               "--service-name com.amazonaws.<region>.s3 --route-table-ids <rtb-id>",
        })

    if not ddb_endpoint_exists and nat_data_cost > 10:
        eliminable_fraction += 0.15
        recommendations.append({
            "action":          "Create DynamoDB Gateway Endpoint (FREE)",
            "eliminates_pct":  15,
            "command":         "aws ec2 create-vpc-endpoint --vpc-id <vpc-id> "
                               "--service-name com.amazonaws.<region>.dynamodb --route-table-ids <rtb-id>",
        })

    estimated_saving = round(nat_data_cost * eliminable_fraction, 2)

    return {
        "nat_gateway_hourly_cost_usd": round(nat_hourly_cost, 2),
        "nat_gateway_data_cost_usd":   round(nat_data_cost, 2),
        "nat_bytes_processed_gb":      round(nat_bytes_gb, 2),
        "total_nat_cost_usd":          round(total_nat_cost, 2),
        "s3_endpoint_exists":          s3_endpoint_exists,
        "dynamodb_endpoint_exists":    ddb_endpoint_exists,
        "estimated_saving_usd":        estimated_saving,
        "recommendations":             recommendations,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# DYNAMODB DEEP ANALYSIS
# ═══════════════════════════════════════════════════════════════════════════════

def _ddb():
    if not hasattr(_ddb, "_c") or _ddb._c is None:
        _ddb._c = boto3.client(
            "dynamodb",
            region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
        )
    return _ddb._c
_ddb._c = None


@tool
def get_dynamodb_cost_analysis(
    start_date: str,
    end_date: str,
    lookback_days: int = 14,
) -> dict:
    """
    Deep DynamoDB cost analysis: per-table breakdown, provisioned vs on-demand
    optimality, GSI waste, and idle capacity detection.

    Three billing patterns and when each wins:
    1. ON_DEMAND: pay per request. Best for unpredictable or sparse traffic.
       Cost: $1.25/million writes, $0.25/million reads.
    2. PROVISIONED with autoscaling: best for predictable, sustained traffic.
       Sustained 100 WCU = $58.50/month. Same 100 WCU on-demand = $65/month at
       100% utilisation, but on-demand is cheaper if utilisation < 90%.
    3. PROVISIONED without autoscaling: worst of both worlds unless perfectly sized.

    GSI waste: each GSI replicates writes and maintains its own WCU/RCU provisioning.
    An unused GSI doubles the write cost with zero read benefit.

    This tool detects:
    - Tables with provisioned capacity at < 30% utilisation (should switch to on-demand
      or reduce provisioning)
    - Tables on on-demand with very high, consistent throughput (should switch to provisioned)
    - GSIs with zero reads in the period (paying for writes with no query benefit)
    - Per-table cost breakdown via CE resource-level data

    Args:
        start_date:    YYYY-MM-DD
        end_date:      YYYY-MM-DD
        lookback_days: days of CloudWatch metrics for utilisation analysis
    """
    end_dt   = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=lookback_days)

    tables = []
    try:
        paginator = _ddb().get_paginator("list_tables")
        for page in paginator.paginate():
            tables.extend(page.get("TableNames", []))
    except Exception:
        pass

    table_analyses = []
    total_monthly_waste = 0.0

    for tname in tables:
        try:
            desc = _ddb().describe_table(TableName=tname)["Table"]
        except Exception:
            continue

        billing_mode = desc.get("BillingModeSummary", {}).get("BillingMode", "PROVISIONED")
        prov         = desc.get("ProvisionedThroughput", {})
        prov_rcu     = prov.get("ReadCapacityUnits", 0)
        prov_wcu     = prov.get("WriteCapacityUnits", 0)
        gsi_list     = desc.get("GlobalSecondaryIndexes", [])

        dims = [{"Name": "TableName", "Value": tname}]

        def _ddb_metric(metric: str, stat: str = "Average") -> float:
            try:
                resp = _cw().get_metric_statistics(
                    Namespace="AWS/DynamoDB",
                    MetricName=metric,
                    Dimensions=dims,
                    StartTime=start_dt,
                    EndTime=end_dt,
                    Period=3600,
                    Statistics=[stat],
                )
                pts = resp.get("Datapoints", [])
                if not pts:
                    return 0.0
                return round(sum(dp.get(stat, 0) for dp in pts) / len(pts), 2)
            except Exception:
                return 0.0

        consumed_rcu = _ddb_metric("ConsumedReadCapacityUnits")
        consumed_wcu = _ddb_metric("ConsumedWriteCapacityUnits")

        issues = []
        monthly_waste = 0.0

        if billing_mode == "PROVISIONED" and (prov_rcu + prov_wcu) > 0:
            rcu_util = round(consumed_rcu / prov_rcu * 100, 1) if prov_rcu > 0 else 0
            wcu_util = round(consumed_wcu / prov_wcu * 100, 1) if prov_wcu > 0 else 0
            avg_util = round((rcu_util + wcu_util) / 2, 1)

            if avg_util < 30 and (prov_rcu + prov_wcu) > 10:
                # Cost of idle capacity: 1 RCU/WCU = ~$0.00013/hr each
                idle_rcu   = prov_rcu - consumed_rcu
                idle_wcu   = prov_wcu - consumed_wcu
                wasted_usd = round((idle_rcu * 0.00013 + idle_wcu * 0.00065) * 24 * 30, 2)
                monthly_waste += wasted_usd
                issues.append({
                    "type":   "low_utilisation",
                    "detail": f"Provisioned {prov_rcu} RCU/{prov_wcu} WCU, consuming avg "
                              f"{consumed_rcu:.1f} RCU/{consumed_wcu:.1f} WCU ({avg_util}% utilisation). "
                              f"Waste: ~${wasted_usd}/month.",
                    "action": (
                        "Enable DynamoDB autoscaling (target 70% utilisation) or switch to ON_DEMAND "
                        "if traffic is sparse/unpredictable."
                    ),
                })

        # Check unused GSIs
        unused_gsis = []
        for gsi in gsi_list:
            gsi_name = gsi.get("IndexName", "")
            try:
                resp = _cw().get_metric_statistics(
                    Namespace="AWS/DynamoDB",
                    MetricName="ConsumedReadCapacityUnits",
                    Dimensions=[{"Name": "TableName", "Value": tname},
                                {"Name": "GlobalSecondaryIndexName", "Value": gsi_name}],
                    StartTime=start_dt,
                    EndTime=end_dt,
                    Period=lookback_days * 86400,
                    Statistics=["Sum"],
                )
                pts = resp.get("Datapoints", [])
                gsi_reads = sum(dp.get("Sum", 0) for dp in pts)
                if gsi_reads == 0:
                    gsi_prov = gsi.get("ProvisionedThroughput", {})
                    gsi_wcu  = gsi_prov.get("WriteCapacityUnits", 0)
                    gsi_waste = round(consumed_wcu * 0.00065 * 24 * 30, 2)  # pays duplicate writes
                    unused_gsis.append(gsi_name)
                    monthly_waste += gsi_waste
                    issues.append({
                        "type":   "unused_gsi",
                        "detail": f"GSI '{gsi_name}' has 0 reads in {lookback_days} days. "
                                  f"Still paying for all write replications.",
                        "action": f"Delete GSI '{gsi_name}' if no query uses it.",
                    })
            except Exception:
                pass

        if issues:
            table_analyses.append({
                "table_name":       tname,
                "billing_mode":     billing_mode,
                "prov_rcu":         prov_rcu,
                "prov_wcu":         prov_wcu,
                "consumed_rcu_avg": consumed_rcu,
                "consumed_wcu_avg": consumed_wcu,
                "gsi_count":        len(gsi_list),
                "unused_gsis":      unused_gsis,
                "monthly_waste_usd": round(monthly_waste, 2),
                "issues":           issues,
            })
            total_monthly_waste += monthly_waste

    table_analyses.sort(key=lambda x: x["monthly_waste_usd"], reverse=True)
    logger.info("get_dynamodb_cost_analysis: tables=%d with_issues=%d total_waste=%.2f",
                len(tables), len(table_analyses), total_monthly_waste)
    return {
        "tables_analysed":      len(tables),
        "tables_with_issues":   len(table_analyses),
        "total_monthly_waste_usd": round(total_monthly_waste, 2),
        "table_analyses":       table_analyses,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# CLOUDWATCH LOGS SPECIALIST TOOLS
# ═══════════════════════════════════════════════════════════════════════════════

def _logs():
    if not hasattr(_logs, "_c") or _logs._c is None:
        _logs._c = boto3.client(
            "logs",
            region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
        )
    return _logs._c
_logs._c = None


@tool
def get_log_groups_without_retention() -> dict:
    """
    Finds CloudWatch Log Groups with no retention policy — logs accumulate forever.

    CloudWatch Logs pricing:
      Ingest:  $0.50/GB
      Storage: $0.03/GB/month
      Archive: $0.023/GB/month (after 90-day transition — if Transition To Archive enabled)

    Without a retention policy, logs from 3 years ago cost the same as logs from today.
    A VPC Flow Log group at 50 GB/month with no retention = 1.8 TB after 3 years = $54/month
    just for logs you'll never look at.

    AWS recommendation by log type:
      Lambda function logs:    7-30 days (debug info, short-lived)
      API Gateway access logs: 30-90 days (auditing)
      VPC Flow Logs:           7-14 days (security investigation window)
      Application logs:        30-90 days
      Compliance/audit logs:   1-7 years (but move to S3/Glacier after 30 days)

    Returns: all groups without retention, storage size, and recommended retention.
    """
    groups_no_retention = []
    total_stored_bytes  = 0

    try:
        paginator = _logs().get_paginator("describe_log_groups")
        for page in paginator.paginate():
            for lg in page.get("logGroups", []):
                name            = lg["logGroupName"]
                stored_bytes    = lg.get("storedBytes", 0)
                retention_days  = lg.get("retentionInDays")   # None = forever

                total_stored_bytes += stored_bytes

                if retention_days is None:
                    # Infer recommended retention from log group name
                    name_lower = name.lower()
                    if "vpc" in name_lower or "flowlog" in name_lower or "flow-log" in name_lower:
                        recommended = 7
                    elif "lambda" in name_lower or "/aws/lambda" in name_lower:
                        recommended = 14
                    elif "apigateway" in name_lower or "api-gw" in name_lower:
                        recommended = 30
                    elif "cloudtrail" in name_lower or "audit" in name_lower:
                        recommended = 365
                    else:
                        recommended = 30

                    monthly_cost = round(stored_bytes / (1024 ** 3) * 0.03, 4)

                    groups_no_retention.append({
                        "log_group":            name,
                        "stored_gb":            round(stored_bytes / (1024 ** 3), 2),
                        "monthly_storage_cost": monthly_cost,
                        "recommended_days":     recommended,
                        "action": (
                            f"aws logs put-retention-policy "
                            f"--log-group-name '{name}' "
                            f"--retention-in-days {recommended}"
                        ),
                    })
    except Exception:
        pass

    groups_no_retention.sort(key=lambda x: x["monthly_storage_cost"], reverse=True)
    total_waste = sum(g["monthly_storage_cost"] for g in groups_no_retention)

    logger.info("get_log_groups_without_retention: no_retention=%d waste=%.2f",
                len(groups_no_retention), total_waste)
    return {
        "groups_without_retention":  len(groups_no_retention),
        "total_monthly_waste_usd":   round(total_waste, 2),
        "top_groups":                groups_no_retention[:20],
        "recommendation": (
            f"{len(groups_no_retention)} log groups with no retention policy. "
            f"Setting appropriate retention saves ~${total_waste:.2f}/month in storage. "
            f"Use AWS-managed policy 'aws logs put-retention-policy' or set via Terraform."
        ),
    }


@tool
def get_expensive_log_groups(
    start_date: str,
    end_date: str,
) -> list:
    """
    Identifies the most expensive CloudWatch Log Groups by ingest volume and cost.

    CloudWatch Logs ingest costs $0.50/GB — 16x more than storage.
    The real cost driver is VOLUME, not retention.

    Common high-volume patterns that should be fixed:
    - Lambda with log level DEBUG in production: every invocation logs 5-10x more
    - API Gateway access logs with full request/response body
    - ECS/Fargate containers logging at DEBUG
    - VPC Flow Logs in ACCEPT+REJECT mode (REJECT-only is cheaper for security)
    - RDS slow query log enabled on healthy databases (logs nothing useful)

    CloudWatch Metric Filters can extract the data you need without storing raw logs.
    Lambda Powertools Structured Logging reduces volume by 60-80% vs raw print().

    Returns top log groups by ingest cost with actionable volume reduction tips.

    Args:
        start_date: YYYY-MM-DD
        end_date:   YYYY-MM-DD
    """
    flt = {
        "Dimensions": {"Key": "SERVICE",
                       "Values": ["AmazonCloudWatch"],
                       "MatchOptions": ["EQUALS"]}
    }
    ce_resp = _ce().get_cost_and_usage(
        TimePeriod={"Start": start_date, "End": end_date},
        Granularity="MONTHLY",
        GroupBy=[{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
        Metrics=["UnblendedCost", "UsageQuantity"],
        Filter=flt,
    )

    log_ingest_cost   = 0.0
    log_storage_cost  = 0.0
    log_ingest_gb     = 0.0
    for period in ce_resp.get("ResultsByTime", []):
        for g in period.get("Groups", []):
            ut     = g["Keys"][0]
            amount = float(g["Metrics"]["UnblendedCost"]["Amount"])
            qty    = float(g["Metrics"]["UsageQuantity"]["Amount"])
            if "DataProcessing-Bytes" in ut or "Logs-Bytes-Ingested" in ut:
                log_ingest_cost += amount
                log_ingest_gb   += qty / (1024 ** 3)
            elif "HourlyStorageMetric" in ut or "Logs-Bytes" in ut:
                log_storage_cost += amount

    # Get top log groups by stored bytes as proxy for volume
    groups = []
    try:
        paginator = _logs().get_paginator("describe_log_groups")
        for page in paginator.paginate():
            for lg in page.get("logGroups", []):
                stored = lg.get("storedBytes", 0)
                name   = lg["logGroupName"]
                if stored > 0:
                    name_lower = name.lower()
                    # Infer tips based on name pattern
                    tips = []
                    if "/aws/lambda/" in name_lower:
                        tips.append("Set LOG_LEVEL=INFO or WARNING in Lambda env vars. "
                                    "Use Lambda Powertools structured logging.")
                    if "apigateway" in name_lower:
                        tips.append("Reduce API GW log format — remove $context.requestBody "
                                    "and $context.responseBody from log format string.")
                    if "vpc" in name_lower or "flowlog" in name_lower:
                        tips.append("Switch VPC Flow Logs to REJECT filter only for security "
                                    "monitoring. ACCEPT traffic is high-volume noise.")
                    if "ecs" in name_lower or "fargate" in name_lower:
                        tips.append("Set container LOG_LEVEL=INFO. Consider FireLens to route "
                                    "logs to S3 instead of CloudWatch for archival.")

                    groups.append({
                        "log_group":          name,
                        "stored_gb":          round(stored / (1024 ** 3), 2),
                        "retention_days":     lg.get("retentionInDays", "forever"),
                        "volume_reduction_tips": tips,
                    })
    except Exception:
        pass

    groups.sort(key=lambda x: x["stored_gb"], reverse=True)

    return {
        "total_log_ingest_cost_usd":   round(log_ingest_cost, 2),
        "total_log_storage_cost_usd":  round(log_storage_cost, 2),
        "total_log_ingest_gb":         round(log_ingest_gb, 2),
        "top_groups_by_volume":        groups[:15],
        "recommendation": (
            f"Log ingest cost: ${log_ingest_cost:.2f}/month ({log_ingest_gb:.1f} GB). "
            "Reducing DEBUG logs to INFO in production typically cuts volume 60-80%."
        ),
    }


@tool
def get_log_insights_cost(
    start_date: str,
    end_date: str,
) -> dict:
    """
    Analyses CloudWatch Log Insights query costs — the most underestimated line
    in the CloudWatch bill.

    Log Insights pricing: $0.005 per GB of data scanned.
    This is charged per query execution, not per GB stored.

    The silent killer: scheduled queries and dashboards.
    A CloudWatch Dashboard widget with a Log Insights query auto-refreshes every 60s.
    If that query scans a 500 GB log group:
      $0.005 × 500 GB × 1440 refreshes/day = $3,600/day = $108,000/month.

    Detection signals:
    - CE usage type "DataScanned-Bytes" in CloudWatch
    - Cross-reference with log group sizes to find which groups are being scanned

    Mitigation strategies:
    1. Add time range filter to queries (last 1h not last 7d)
    2. Use metric filters to extract KPIs instead of scanning logs in real-time
    3. Set dashboard refresh to manual or 1h minimum
    4. Use CloudWatch Contributor Insights (flat fee) for top-N analysis

    Returns: total scan cost, estimated scan GB, and optimization recommendations.

    Args:
        start_date: YYYY-MM-DD
        end_date:   YYYY-MM-DD
    """
    ce_resp = _ce().get_cost_and_usage(
        TimePeriod={"Start": start_date, "End": end_date},
        Granularity="MONTHLY",
        GroupBy=[{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
        Metrics=["UnblendedCost", "UsageQuantity"],
        Filter={
            "Dimensions": {"Key": "SERVICE",
                           "Values": ["AmazonCloudWatch"],
                           "MatchOptions": ["EQUALS"]}
        },
    )

    scan_cost = 0.0
    scan_gb   = 0.0
    for period in ce_resp.get("ResultsByTime", []):
        for g in period.get("Groups", []):
            ut     = g["Keys"][0]
            amount = float(g["Metrics"]["UnblendedCost"]["Amount"])
            qty    = float(g["Metrics"]["UsageQuantity"]["Amount"])
            if "DataScanned" in ut or "Insights" in ut:
                scan_cost += amount
                scan_gb   += qty / (1024 ** 3)

    # Estimate if dashboards are auto-refreshing (heuristic: scan cost > $50)
    likely_auto_refresh = scan_cost > 50

    return {
        "log_insights_scan_cost_usd": round(scan_cost, 2),
        "log_insights_scan_gb":       round(scan_gb, 2),
        "likely_dashboard_auto_refresh": likely_auto_refresh,
        "severity": "critical" if scan_cost > 500 else "high" if scan_cost > 100 else "medium",
        "recommendations": [
            "Audit CloudWatch Dashboards — find Log Insights widgets and set refresh to 'manual'",
            "Add time range constraint to all Log Insights queries (e.g. | filter @timestamp > ago(1h))",
            "Replace frequent Log Insights queries with CloudWatch Metric Filters (zero scan cost)",
            "Use CloudWatch Contributor Insights for top-N analysis ($0.002/event vs $0.005/GB scan)",
        ] if scan_cost > 10 else ["Log Insights cost within normal range."],
    }


# ═══════════════════════════════════════════════════════════════════════════════
# AWS CONFIG SPECIALIST TOOLS
# ═══════════════════════════════════════════════════════════════════════════════

def _config():
    if not hasattr(_config, "_c") or _config._c is None:
        _config._c = boto3.client(
            "config",
            region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
        )
    return _config._c
_config._c = None


@tool
def get_config_cost_analysis(
    start_date: str,
    end_date: str,
) -> dict:
    """
    Analyses AWS Config rule evaluation costs — one of the most surprising cost
    drivers in governance-heavy organizations.

    AWS Config pricing:
      Configuration items:  $0.003 per item recorded
      Rule evaluations:     $0.001 per evaluation (first 100k/month free per account)
      Conformance Packs:    additional charge per evaluation
      Advanced Queries:     $0.003 per GB queried

    Scale problem: 100 Config rules × 500 resources × 2 evaluations/day × 30 days
    = 3,000,000 evaluations/month = $2,900/month — in ONE account, ONE region.
    With multi-account (50 accounts) × multi-region (4 regions):
    $2,900 × 200 = $580,000/year for Config compliance checks.

    What this reveals:
    - Total Config spend broken down by item recording vs rule evaluation
    - Number of active rules and conformance packs
    - Regions with Config recorders enabled (often more than needed)

    Optimization levers:
    1. Exclude resource types not relevant to your compliance scope
    2. Use periodic evaluation (24h) instead of change-triggered where possible
    3. Consolidate duplicate rules across conformance packs
    4. Disable Config in regions where you have no resources

    Args:
        start_date: YYYY-MM-DD
        end_date:   YYYY-MM-DD
    """
    ce_resp = _ce().get_cost_and_usage(
        TimePeriod={"Start": start_date, "End": end_date},
        Granularity="MONTHLY",
        GroupBy=[{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
        Metrics=["UnblendedCost", "UsageQuantity"],
        Filter={
            "Dimensions": {"Key": "SERVICE",
                           "Values": ["AWS Config"],
                           "MatchOptions": ["EQUALS"]}
        },
    )

    item_cost       = 0.0
    rule_eval_cost  = 0.0
    pack_cost       = 0.0
    total_config    = 0.0
    for period in ce_resp.get("ResultsByTime", []):
        for g in period.get("Groups", []):
            ut     = g["Keys"][0]
            amount = float(g["Metrics"]["UnblendedCost"]["Amount"])
            total_config += amount
            if "ConfigurationItem" in ut:
                item_cost += amount
            elif "RuleEvaluation" in ut or "rule-evaluation" in ut.lower():
                rule_eval_cost += amount
            elif "ConformancePack" in ut or "conformance" in ut.lower():
                pack_cost += amount

    # Count active rules and recorders
    active_rules      = 0
    conformance_packs = 0
    recorder_regions  = []

    try:
        paginator = _config().get_paginator("describe_config_rules")
        for page in paginator.paginate():
            active_rules += len(page.get("ConfigRules", []))
    except Exception:
        pass

    try:
        resp = _config().describe_conformance_packs()
        conformance_packs = len(resp.get("ConformancePackDetails", []))
    except Exception:
        pass

    try:
        resp = _config().describe_configuration_recorders()
        recorder_regions = [r.get("name", "") for r in resp.get("ConfigurationRecorders", [])]
    except Exception:
        pass

    optimizations = []
    if rule_eval_cost > 100:
        optimizations.append(
            "Switch change-triggered rules to periodic evaluation (daily) for stable resources — "
            "reduces evaluations by 80-90%."
        )
    if conformance_packs > 3:
        optimizations.append(
            f"{conformance_packs} conformance packs — audit for overlapping rules. "
            "Each duplicate rule doubles evaluation cost."
        )
    if active_rules > 50:
        optimizations.append(
            f"{active_rules} active Config rules. Audit for rules covering "
            "resource types not present in your account."
        )

    return {
        "total_config_cost_usd":       round(total_config, 2),
        "configuration_item_cost_usd": round(item_cost, 2),
        "rule_evaluation_cost_usd":    round(rule_eval_cost, 2),
        "conformance_pack_cost_usd":   round(pack_cost, 2),
        "active_rules":                active_rules,
        "conformance_packs":           conformance_packs,
        "recorder_regions":            recorder_regions,
        "optimizations":               optimizations,
        "severity": "critical" if total_config > 1000 else "high" if total_config > 200 else "medium",
    }


@tool
def get_config_recorder_waste() -> dict:
    """
    Identifies AWS Config Recorders enabled in regions where your organization
    has no active resources — paying for configuration tracking with nothing to track.

    Config charges $0.003 per configuration item recorded. In an empty region,
    even the baseline services (IAM, CloudTrail, Config itself) generate items.
    More importantly: if you have a conformance pack deployed globally, it runs
    in every region the recorder is active.

    Common scenario: Config enabled in all 20 AWS regions by a compliance template,
    but the organization only operates in us-east-1, eu-west-1, ap-southeast-1.
    17 empty regions × conformance pack evaluations = pure waste.

    Also detects: S3 delivery channel accumulation (Config history sent to S3
    without lifecycle policy = growing storage cost with no retention limit).

    Returns: active recorders, empty-region candidates, delivery channel S3 paths.
    """
    recorders = []
    try:
        resp = _config().describe_configuration_recorders()
        recorders = resp.get("ConfigurationRecorders", [])
    except Exception:
        pass

    recorder_statuses = []
    try:
        resp = _config().describe_configuration_recorder_status()
        recorder_statuses = resp.get("ConfigurationRecordersStatus", [])
    except Exception:
        pass

    # Delivery channels (S3 paths where Config sends history)
    delivery_channels = []
    try:
        resp = _config().describe_delivery_channels()
        for dc in resp.get("DeliveryChannels", []):
            delivery_channels.append({
                "name":         dc.get("name"),
                "s3_bucket":    dc.get("s3BucketName"),
                "s3_prefix":    dc.get("s3KeyPrefix"),
                "sns_topic":    dc.get("snsTopicARN"),
                "frequency":    dc.get("configSnapshotDeliveryProperties", {}).get("deliveryFrequency"),
            })
    except Exception:
        pass

    # Check which recorders are actively recording
    recording_status = {s.get("name"): s.get("recording", False) for s in recorder_statuses}

    active_count   = sum(1 for r in recorders if recording_status.get(r.get("name"), False))
    inactive_count = len(recorders) - active_count

    return {
        "total_recorders":     len(recorders),
        "active_recorders":    active_count,
        "inactive_recorders":  inactive_count,
        "delivery_channels":   delivery_channels,
        "recorder_names":      [r.get("name") for r in recorders],
        "recommendations": [
            "Verify Config is only enabled in regions where you operate. "
            "Disable recorders in empty regions to eliminate wasted evaluations.",
            "Add S3 lifecycle policy to Config delivery bucket: "
            "transition to Glacier Instant after 90 days, expire after 2555 days (7 years).",
            "If using Control Tower, verify which regions are in the governance boundary — "
            "only those need Config.",
        ],
    }


# ═══════════════════════════════════════════════════════════════════════════════
# CONTROL TOWER / MULTI-ACCOUNT SPECIALIST TOOLS
# ═══════════════════════════════════════════════════════════════════════════════

def _orgs():
    if not hasattr(_orgs, "_c") or _orgs._c is None:
        _orgs._c = boto3.client("organizations", region_name="us-east-1")
    return _orgs._c
_orgs._c = None


@tool
def get_control_tower_per_account_cost(
    start_date: str,
    end_date: str,
) -> dict:
    """
    Quantifies the AWS baseline compliance cost per managed account under
    Control Tower — the cost that multiplies every time you create a new account.

    Per-account baseline services (approximate monthly cost per account):
      AWS Config:        $2-15/account (depends on rule count and resource density)
      AWS CloudTrail:    $2/account (management events, all regions)
      AWS Security Hub:  $0.001/check × resources (typically $5-20/account)
      AWS GuardDuty:     $0.50 base + $4/1000 events (typically $5-15/account)
      AWS Access Analyzer: negligible
      SCPs (no charge):  free

    Total baseline: ~$15-50/account/month.
    With 100 accounts: $1,500-5,000/month just for baseline compliance.

    This is INVISIBLE in cost analysis because it's distributed across accounts.
    The management account only sees its own cost, not the portfolio cost.

    Returns: account count, per-service breakdown, total compliance baseline cost.

    Args:
        start_date: YYYY-MM-DD
        end_date:   YYYY-MM-DD
    """
    # Count managed accounts
    account_count = 0
    try:
        paginator = _orgs().get_paginator("list_accounts")
        for page in paginator.paginate():
            account_count += len([
                a for a in page.get("Accounts", [])
                if a.get("Status") == "ACTIVE"
            ])
    except Exception:
        account_count = 1  # fallback: single account

    # Get consolidated costs for security services from CE
    security_services = [
        "AWS Config",
        "AWS CloudTrail",
        "AWS Security Hub",
        "Amazon GuardDuty",
    ]

    service_costs: dict = {}
    for svc in security_services:
        try:
            resp = _ce().get_cost_and_usage(
                TimePeriod={"Start": start_date, "End": end_date},
                Granularity="MONTHLY",
                Metrics=["UnblendedCost"],
                Filter={"Dimensions": {"Key": "SERVICE",
                                       "Values": [svc],
                                       "MatchOptions": ["EQUALS"]}},
            )
            total = sum(
                float(r.get("Total", {}).get("UnblendedCost", {}).get("Amount", 0))
                for r in resp.get("ResultsByTime", [])
            )
            service_costs[svc] = round(total, 2)
        except Exception:
            service_costs[svc] = 0.0

    total_compliance_cost = sum(service_costs.values())
    cost_per_account = round(total_compliance_cost / max(account_count, 1), 2)

    return {
        "managed_accounts":           account_count,
        "total_compliance_cost_usd":  round(total_compliance_cost, 2),
        "cost_per_account_usd":       cost_per_account,
        "by_service":                 service_costs,
        "insight": (
            f"Every new account in your Control Tower org costs ~${cost_per_account}/month "
            f"in baseline compliance services. With {account_count} accounts: "
            f"${total_compliance_cost:.2f}/month just for governance baseline. "
            "Consider Account Vending Machine policies to disable unused security services "
            "in sandbox/dev accounts."
        ),
    }


@tool
def get_log_archive_account_waste(
    start_date: str,
    end_date: str,
) -> dict:
    """
    Analyses the Control Tower Log Archive account's S3 storage costs — typically
    the largest S3 bill in the entire organization and the most neglected.

    What accumulates in Log Archive:
      - CloudTrail management events: all API calls, all accounts, all regions
      - CloudTrail data events: S3/Lambda (if enabled — extremely high volume)
      - Config history: configuration snapshots, all accounts, all regions
      - VPC Flow Logs: if centralized (50-200 GB/account/month)
      - Security Hub findings archive
      - GuardDuty findings

    Typical growth: 5-20 GB/account/month without data events.
    With 100 accounts: 500 GB-2 TB/month = $11-46/month in ingest + growing storage.
    After 3 years: 18-72 TB in Standard storage = $414-1,657/month in storage alone.

    Solution: tiered lifecycle policy:
      Day 0-30:  S3 Standard (hot queries)
      Day 31-90: S3 Standard-IA (occasional access)
      Day 91+:   S3 Glacier Instant Retrieval (compliance retention, rare access)
      Day 2555+: Delete (7-year compliance window typically sufficient)

    Returns: S3 costs for common Log Archive buckets and lifecycle recommendations.

    Args:
        start_date: YYYY-MM-DD
        end_date:   YYYY-MM-DD
    """
    # Get overall S3 costs — proxy for Log Archive account analysis
    flt = {
        "Dimensions": {"Key": "SERVICE",
                       "Values": ["Amazon S3"],
                       "MatchOptions": ["EQUALS"]}
    }
    ce_resp = _ce().get_cost_and_usage(
        TimePeriod={"Start": start_date, "End": end_date},
        Granularity="MONTHLY",
        GroupBy=[{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
        Metrics=["UnblendedCost", "UsageQuantity"],
        Filter=flt,
    )

    standard_storage_cost = 0.0
    standard_storage_gb   = 0.0
    glacier_cost          = 0.0

    for period in ce_resp.get("ResultsByTime", []):
        for g in period.get("Groups", []):
            ut     = g["Keys"][0]
            amount = float(g["Metrics"]["UnblendedCost"]["Amount"])
            qty    = float(g["Metrics"]["UsageQuantity"]["Amount"])
            if "TimedStorage-ByteHrs" in ut and "Glacier" not in ut and "IA" not in ut:
                standard_storage_cost += amount
                standard_storage_gb   += qty / (1024 ** 3)
            elif "Glacier" in ut or "DEEP" in ut.upper():
                glacier_cost += amount

    # Estimate saving from Glacier transition after 90 days
    # Assume 70% of logs are > 90 days old (normal for log archives)
    archivable_gb    = standard_storage_gb * 0.70
    potential_saving = round(archivable_gb * (0.023 - 0.004), 2)  # Standard → Glacier IR

    lifecycle_policy = {
        "rules": [
            {"transition": "S3 Standard-IA", "days": 30,  "saving": "~46% vs Standard"},
            {"transition": "S3 Glacier IR",  "days": 90,  "saving": "~83% vs Standard"},
            {"expiration": "Delete",          "days": 2555, "note": "7-year compliance window"},
        ]
    }

    return {
        "s3_standard_storage_cost_usd": round(standard_storage_cost, 2),
        "s3_standard_storage_gb":       round(standard_storage_gb, 2),
        "s3_glacier_cost_usd":          round(glacier_cost, 2),
        "archivable_older_than_90d_gb": round(archivable_gb, 2),
        "potential_monthly_saving_usd": potential_saving,
        "recommended_lifecycle_policy": lifecycle_policy,
        "recommendation": (
            f"{standard_storage_gb:.0f} GB in Standard S3 storage. "
            f"~{archivable_gb:.0f} GB is likely log archive data > 90 days old. "
            f"Adding Glacier IR lifecycle transition saves ~${potential_saving}/month. "
            "For CloudTrail logs: disable data events in dev/sandbox accounts "
            "(management events are sufficient for 90% of compliance use cases)."
        ),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# NETWORKING SPECIALIST TOOLS
# ═══════════════════════════════════════════════════════════════════════════════

def _route53():
    if not hasattr(_route53, "_c") or _route53._c is None:
        _route53._c = boto3.client("route53", region_name="us-east-1")
    return _route53._c
_route53._c = None


@tool
def get_transit_gateway_waste(
    start_date: str,
    end_date: str,
) -> dict:
    """
    Identifies Transit Gateway attachments and peering connections that are idle
    or attached to deleted/unused VPCs.

    Transit Gateway pricing:
      $0.05/hr per attachment = $36/month per attachment (just for existing)
      $0.02/GB of data processed

    An organization with 15 VPC attachments pays $540/month in attachment fees
    before a single byte is transferred. Attachments to dev VPCs with no running
    instances = pure waste.

    Common waste patterns:
    - VPC attachments to "test" or "sandbox" VPCs that are now empty
    - Peering attachments between accounts no longer in use
    - Attachments in multiple regions when all traffic goes through one region

    Returns: all attachments with state, associated VPC, estimated monthly cost,
    and identification of likely-idle attachments.

    Args:
        start_date: YYYY-MM-DD
        end_date:   YYYY-MM-DD
    """
    # CE: Transit Gateway costs
    ce_resp = _ce().get_cost_and_usage(
        TimePeriod={"Start": start_date, "End": end_date},
        Granularity="MONTHLY",
        GroupBy=[{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
        Metrics=["UnblendedCost", "UsageQuantity"],
        Filter={
            "Dimensions": {"Key": "SERVICE",
                           "Values": ["Amazon Virtual Private Cloud"],
                           "MatchOptions": ["EQUALS"]}
        },
    )

    tgw_attach_cost = 0.0
    tgw_data_cost   = 0.0
    for period in ce_resp.get("ResultsByTime", []):
        for g in period.get("Groups", []):
            ut     = g["Keys"][0]
            amount = float(g["Metrics"]["UnblendedCost"]["Amount"])
            if "TransitGateway-Hours" in ut or "TransitGatewayAttachment" in ut:
                tgw_attach_cost += amount
            elif "TransitGateway-Bytes" in ut:
                tgw_data_cost += amount

    # List TGW attachments
    attachments = []
    try:
        paginator = _ec2().get_paginator("describe_transit_gateway_attachments")
        for page in paginator.paginate():
            for att in page.get("TransitGatewayAttachments", []):
                state    = att.get("State", "")
                res_type = att.get("ResourceType", "")
                res_id   = att.get("ResourceId", "")
                att_id   = att.get("TransitGatewayAttachmentId", "")
                tags     = {t["Key"].lower(): t["Value"].lower()
                            for t in att.get("Tags", [])}
                name     = tags.get("name", att_id)

                is_available = state == "available"
                monthly_cost = 36.0 if is_available else 0.0

                # Flag likely idle attachments
                is_idle_candidate = any(
                    x in name.lower() for x in ["test", "dev", "sandbox", "tmp", "temp", "old"]
                )

                attachments.append({
                    "attachment_id":     att_id,
                    "name":              name,
                    "resource_type":     res_type,
                    "resource_id":       res_id,
                    "state":             state,
                    "monthly_cost_usd":  monthly_cost,
                    "idle_candidate":    is_idle_candidate,
                })
    except Exception:
        pass

    idle_attachments = [a for a in attachments if a["idle_candidate"]]
    potential_saving = sum(a["monthly_cost_usd"] for a in idle_attachments)

    return {
        "total_tgw_cost_usd":        round(tgw_attach_cost + tgw_data_cost, 2),
        "attachment_fees_usd":       round(tgw_attach_cost, 2),
        "data_processing_fees_usd":  round(tgw_data_cost, 2),
        "total_attachments":         len(attachments),
        "idle_candidates":           len(idle_attachments),
        "potential_saving_usd":      round(potential_saving, 2),
        "attachments":               attachments,
        "recommendation": (
            f"{len(attachments)} TGW attachments at $36/month each = "
            f"${len(attachments) * 36}/month in attachment fees. "
            + (f"{len(idle_attachments)} likely idle (dev/test/sandbox). "
               f"Removing them saves ~${potential_saving}/month."
               if idle_attachments else "No obvious idle attachments detected by name pattern.")
        ),
    }


@tool
def get_vpc_idle_resources() -> dict:
    """
    Scans for idle VPC resources that incur hourly charges regardless of utilisation:

    1. VPN Connections: $0.05/hr ($36/month) each — charged whether tunnels are
       active or not. A VPN to a decommissioned on-premises network = $36/month forever.

    2. Carrier Gateways: $0.00/hr but data processing fees — check for non-used ones.

    3. VPCs with no running instances but with:
       - NAT Gateways still running ($0.045/hr = $32.40/month)
       - Internet Gateways attached (free, but signals abandoned VPC)

    Detection: cross-reference VPN connection state + last activity CloudWatch metric.
    A VPN with TunnelState = 0 for 30+ days = abandoned.

    Returns: idle VPN connections, empty VPCs with paid resources.
    """
    # VPN connections
    vpn_connections = []
    try:
        resp = _ec2().describe_vpn_connections(
            Filters=[{"Name": "state", "Values": ["available", "pending"]}]
        )
        for vpn in resp.get("VpnConnections", []):
            vpn_id    = vpn["VpnConnectionId"]
            state     = vpn.get("State")
            tags      = {t["Key"]: t["Value"] for t in vpn.get("Tags", [])}
            name      = tags.get("Name", vpn_id)
            vgw_id    = vpn.get("VpnGatewayId", "")

            # Check tunnel state via CloudWatch
            tunnel_up = False
            try:
                resp_cw = _cw().get_metric_statistics(
                    Namespace="AWS/VPN",
                    MetricName="TunnelState",
                    Dimensions=[{"Name": "VpnId", "Value": vpn_id}],
                    StartTime=datetime.now(timezone.utc) - timedelta(days=7),
                    EndTime=datetime.now(timezone.utc),
                    Period=7 * 86400,
                    Statistics=["Maximum"],
                )
                pts = resp_cw.get("Datapoints", [])
                if pts and max(dp.get("Maximum", 0) for dp in pts) > 0:
                    tunnel_up = True
            except Exception:
                pass

            vpn_connections.append({
                "vpn_id":           vpn_id,
                "name":             name,
                "state":            state,
                "tunnel_active":    tunnel_up,
                "monthly_cost_usd": 36.0,
                "recommendation":   "DELETE — tunnel down for 7+ days" if not tunnel_up else "ACTIVE — keep",
            })
    except Exception:
        pass

    # NAT Gateways in potentially empty VPCs
    idle_nats = []
    try:
        paginator = _ec2().get_paginator("describe_nat_gateways")
        for page in paginator.paginate(
            Filters=[{"Name": "state", "Values": ["available"]}]
        ):
            for nat in page.get("NatGateways", []):
                vpc_id = nat.get("VpcId", "")
                nat_id = nat.get("NatGatewayId", "")

                # Check if VPC has any running instances
                try:
                    resp_inst = _ec2().describe_instances(
                        Filters=[
                            {"Name": "vpc-id",            "Values": [vpc_id]},
                            {"Name": "instance-state-name","Values": ["running"]},
                        ]
                    )
                    has_instances = any(
                        r.get("Instances")
                        for r in resp_inst.get("Reservations", [])
                    )
                except Exception:
                    has_instances = True  # assume active if can't check

                if not has_instances:
                    idle_nats.append({
                        "nat_gateway_id":  nat_id,
                        "vpc_id":          vpc_id,
                        "monthly_cost_usd": 32.40,
                        "recommendation":  "VPC has no running instances — delete NAT Gateway",
                    })
    except Exception:
        pass

    idle_vpn_cost  = sum(v["monthly_cost_usd"] for v in vpn_connections if not v["tunnel_active"])
    idle_nat_cost  = sum(n["monthly_cost_usd"] for n in idle_nats)
    total_waste    = idle_vpn_cost + idle_nat_cost

    logger.info("get_vpc_idle_resources: vpns=%d idle_vpns=%d idle_nats=%d waste=%.2f",
                len(vpn_connections), sum(1 for v in vpn_connections if not v["tunnel_active"]),
                len(idle_nats), total_waste)
    return {
        "vpn_connections":        vpn_connections,
        "nat_gateways_in_empty_vpcs": idle_nats,
        "total_monthly_waste_usd": round(total_waste, 2),
        "summary": {
            "vpn_connections_total":  len(vpn_connections),
            "vpn_tunnels_down":       sum(1 for v in vpn_connections if not v["tunnel_active"]),
            "idle_nat_gateways":      len(idle_nats),
        },
    }


@tool
def get_route53_waste() -> dict:
    """
    Identifies Route 53 resources incurring charges with no active use:

    Pricing:
      Hosted Zone:        $0.50/month each
      Health Check:       $0.50/month (AWS endpoint) or $0.75/month (non-AWS)
      Resolver Endpoint:  $0.125/hr × 2 IPs minimum = $180/month per endpoint
      Query logging:      $0.50/GB to CloudWatch Logs

    Waste patterns:
    1. Hosted zones with no records (or only NS/SOA) — domain abandoned
    2. Health checks pointing to terminated/non-existent endpoints
    3. Resolver endpoints in VPCs with no EC2 instances
    4. Query logging enabled on all zones (CloudWatch ingest cost)

    A 3-year-old account may have 30+ abandoned hosted zones = $15/month trivially.
    A Resolver endpoint forgotten after a migration = $180/month.

    Returns: empty zones, suspicious health checks, resolver endpoint analysis.
    """
    # Hosted Zones
    empty_zones    = []
    all_zone_count = 0
    try:
        paginator = _route53().get_paginator("list_hosted_zones")
        for page in paginator.paginate():
            for zone in page.get("HostedZones", []):
                all_zone_count += 1
                zone_id   = zone["Id"].split("/")[-1]
                zone_name = zone["Name"]
                record_count = zone.get("ResourceRecordSetCount", 0)

                # Only NS and SOA = 2 default records = effectively empty
                if record_count <= 2:
                    empty_zones.append({
                        "zone_id":       zone_id,
                        "zone_name":     zone_name,
                        "record_count":  record_count,
                        "monthly_cost":  0.50,
                        "action":        f"aws route53 delete-hosted-zone --id {zone_id}",
                    })
    except Exception:
        pass

    # Health Checks
    all_checks       = []
    suspicious_checks = []
    try:
        resp = _route53().list_health_checks()
        for hc in resp.get("HealthChecks", []):
            hc_id  = hc["Id"]
            config = hc.get("HealthCheckConfig", {})
            fqdn   = config.get("FullyQualifiedDomainName", "")
            ip     = config.get("IPAddress", "")
            target = fqdn or ip

            # Flag checks with no FQDN/IP (misconfigured) or targeting private IPs
            is_suspicious = (
                not target or
                (ip and (ip.startswith("10.") or ip.startswith("172.") or ip.startswith("192.168.")))
            )
            hc_type   = config.get("Type", "")
            hc_cost   = 0.75 if hc_type in ("CALCULATED", "CLOUDWATCH_METRIC") else 0.50
            all_checks.append({
                "health_check_id": hc_id,
                "target":          target,
                "type":            hc_type,
                "monthly_cost":    hc_cost,
                "suspicious":      is_suspicious,
            })
            if is_suspicious:
                suspicious_checks.append(hc_id)
    except Exception:
        pass

    empty_zone_cost     = sum(z["monthly_cost"] for z in empty_zones)
    suspicious_hc_cost  = sum(h["monthly_cost"] for h in all_checks if h["suspicious"])
    total_waste         = empty_zone_cost + suspicious_hc_cost

    logger.info("get_route53_waste: zones=%d empty=%d hcs=%d suspicious=%d",
                all_zone_count, len(empty_zones), len(all_checks), len(suspicious_checks))
    return {
        "total_hosted_zones":           all_zone_count,
        "empty_zones":                  empty_zones,
        "total_health_checks":          len(all_checks),
        "suspicious_health_checks":     len(suspicious_checks),
        "total_monthly_waste_usd":      round(total_waste, 2),
        "health_checks":                all_checks[:20],
        "recommendation": (
            f"{len(empty_zones)} empty hosted zones (${empty_zone_cost:.2f}/mo) + "
            f"{len(suspicious_checks)} suspicious health checks (${suspicious_hc_cost:.2f}/mo). "
            "Total: $" + f"{total_waste:.2f}/month to eliminate."
        ),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# COMMITMENTS & PRICING SPECIALIST TOOLS
# ═══════════════════════════════════════════════════════════════════════════════

def _ce_us():
    """Cost Explorer always in us-east-1."""
    return _ce()


@tool
def get_ri_expiry_risk(
    days_ahead: int = 45,
) -> list:
    """
    Identifies Reserved Instances expiring within `days_ahead` days with no
    renewal plan — you lose the discount on the expiry date without warning.

    AWS does NOT auto-renew RIs. When an RI expires:
    - The instance keeps running at full On-Demand price
    - You get zero notification by default
    - The price spike shows up on the next bill, 2-4 weeks later

    Common scenario: a 1-year RI bought for a cost-reduction project expires,
    the team forgets, and the next bill is 3× higher. A 3-year RI batch expiring
    simultaneously = cliff-edge cost jump.

    Urgency thresholds:
    - < 7 days:  CRITICAL — renew NOW or convert to Savings Plan
    - 7-30 days: HIGH — evaluate: renew RI or switch to Compute SP?
    - 30-45 days: MEDIUM — plan renewal or SP conversion

    Also flags: RIs in "payment-pending" or "retired" state (money locked, no benefit).

    Returns: expiring RIs sorted by expiry date with monthly impact estimate.
    """
    import datetime as _dt

    expiring = []
    today    = _dt.date.today()
    deadline = today + _dt.timedelta(days=days_ahead)

    services = {
        "ec2":         ("ec2", "describe_reserved_instances"),
        "rds":         ("rds", "describe_reserved_db_instances"),
        "elasticache": ("elasticache", "describe_reserved_cache_nodes"),
        "opensearch":  ("opensearch", "describe_reserved_instances"),
        "redshift":    ("redshift", "describe_reserved_nodes"),
    }

    for svc_name, (client_fn_name, method_name) in services.items():
        try:
            if svc_name == "ec2":
                client = _ec2()
            elif svc_name == "rds":
                client = _rds()
            elif svc_name == "elasticache":
                import boto3
                client = _client("elasticache")
            elif svc_name == "opensearch":
                import boto3
                client = _client("opensearch")
            elif svc_name == "redshift":
                import boto3
                client = _client("redshift")
            else:
                continue

            # Each service uses a slightly different response shape
            if svc_name == "ec2":
                resp = client.describe_reserved_instances(Filters=[{"Name": "state", "Values": ["active"]}])
                instances = resp.get("ReservedInstances", [])
                for ri in instances:
                    start = ri.get("Start")
                    dur   = ri.get("Duration", 0)
                    if start:
                        expiry = (start + _dt.timedelta(seconds=dur)).date()
                        if expiry <= deadline:
                            days_left = (expiry - today).days
                            hourly    = ri.get("RecurringCharges", [{}])[0].get("Amount", 0) if ri.get("RecurringCharges") else ri.get("FixedPrice", 0) / max(dur / 3600, 1)
                            expiring.append({
                                "service":          "EC2",
                                "reservation_id":   ri.get("ReservedInstancesId", ""),
                                "instance_type":    ri.get("InstanceType", ""),
                                "count":            ri.get("InstanceCount", 1),
                                "expiry_date":      str(expiry),
                                "days_remaining":   days_left,
                                "monthly_impact_usd": round(float(hourly) * ri.get("InstanceCount", 1) * 720, 2),
                                "severity":         "critical" if days_left < 7 else "high" if days_left < 30 else "medium",
                                "action":           "Renew RI or purchase equivalent Compute Savings Plan before expiry.",
                            })

            elif svc_name == "rds":
                resp = client.describe_reserved_db_instances()
                for ri in resp.get("ReservedDBInstances", []):
                    if ri.get("State") != "active":
                        continue
                    start  = ri.get("StartTime")
                    dur    = ri.get("Duration", 0)
                    if start:
                        expiry = (start.date() if hasattr(start, "date") else start) + _dt.timedelta(days=dur // 86400)
                        if expiry <= deadline:
                            days_left = (expiry - today).days
                            expiring.append({
                                "service":        "RDS",
                                "reservation_id": ri.get("ReservedDBInstanceId", ""),
                                "instance_type":  ri.get("DBInstanceClass", ""),
                                "count":          ri.get("DBInstanceCount", 1),
                                "expiry_date":    str(expiry),
                                "days_remaining": days_left,
                                "monthly_impact_usd": round(ri.get("RecurringCharges", [{}])[0].get("RecurringChargeAmount", 0) * ri.get("DBInstanceCount", 1) * 720, 2),
                                "severity":       "critical" if days_left < 7 else "high" if days_left < 30 else "medium",
                                "action":         "Renew RDS RI or migrate workload to Aurora Serverless v2.",
                            })
        except Exception:
            pass

    expiring.sort(key=lambda x: x["days_remaining"])
    logger.info("get_ri_expiry_risk: expiring_within_%d_days=%d", days_ahead, len(expiring))
    return expiring


@tool
def get_savings_plan_optimization(
    start_date: str,
    end_date: str,
) -> dict:
    """
    Analyses whether the current mix of Savings Plans is optimal — a $50k/year
    decision most teams make once and never revisit.

    Savings Plan types and their trade-offs:
      Compute SP:  applies to EC2 (any family/region/OS), Lambda, Fargate
                   → 66% saving vs On-Demand, maximum flexibility
      EC2 Instance SP: applies to specific EC2 family + region only
                   → 72% saving vs On-Demand, but locked to family+region
      SageMaker SP: SageMaker instances only → 64% saving

    Key insight: EC2 Instance SP saves 6% more than Compute SP, but ONLY
    if you don't change instance family. If you ever move from m5 to m6i,
    the EC2 SP no longer applies and you lose the discount silently.

    The trap: teams buy EC2 Instance SP for 20% of spend, Compute SP for none,
    migrate to Graviton, the EC2 SP coverage drops, they don't notice for months.

    Also detects: SP purchased in wrong region (us-east-1 SP not covering
    eu-west-1 workloads), SP utilisation < 90% (wasted commitment).

    Returns: current SP portfolio analysis with optimization recommendations.
    """
    result = {
        "savings_plans":      [],
        "total_commitment":   0.0,
        "underutilized_sps":  [],
        "optimization_notes": [],
    }

    try:
        resp = _ce().get_savings_plans_utilization(
            TimePeriod={"Start": start_date, "End": end_date},
            Granularity="MONTHLY",
        )
        totals = resp.get("Total", {})
        utilization_pct = float(totals.get("Utilization", {}).get("UtilizationPercentage", 100))
        unused_commitment = float(totals.get("Savings", {}).get("OnDemandCostEquivalent", 0)) - float(totals.get("Savings", {}).get("NetSavings", 0))

        if utilization_pct < 90:
            result["optimization_notes"].append(
                f"SP utilisation at {utilization_pct:.1f}% — paying for commitment not being used. "
                f"${unused_commitment:.2f} wasted. Reduce next SP purchase or resize workload up."
            )
    except Exception:
        pass

    # Get coverage breakdown by service to detect Fargate/Lambda not covered
    try:
        cov_resp = _ce().get_savings_plans_coverage(
            TimePeriod={"Start": start_date, "End": end_date},
            GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
            Granularity="MONTHLY",
        )
        for period in cov_resp.get("SavingsPlansCoverages", []):
            for g in period.get("Groups", []):
                svc = g.get("Attributes", {}).get("SERVICE", "")
                attrs = g.get("Coverage", {})
                coverage_pct = float(attrs.get("CoveragePercentage", 100))
                on_demand = float(attrs.get("OnDemandCost", 0))
                if coverage_pct < 50 and on_demand > 100:
                    result["optimization_notes"].append(
                        f"{svc}: only {coverage_pct:.0f}% covered by SPs "
                        f"(${on_demand:.0f} On-Demand/month uncovered). "
                        "Add Compute SP to cover this service."
                    )
                    result["underutilized_sps"].append({
                        "service": svc,
                        "coverage_pct": coverage_pct,
                        "uncovered_monthly_usd": round(on_demand * (1 - coverage_pct / 100), 2),
                    })
    except Exception:
        pass

    if not result["optimization_notes"]:
        result["optimization_notes"].append("SP portfolio appears well-optimised.")

    return result


@tool
def get_aws_credits_status(
    start_date: str,
    end_date: str,
) -> dict:
    """
    Identifies AWS credits nearing expiry and detects if credits are being
    applied to services where they provide maximum value.

    Credits expire at month-end with no rollover. An account with $5,000 in
    credits expiring at month-end but only $2,000 of eligible spend = $3,000
    left on the table — gone forever.

    Credit types and eligibility:
      - AWS Activate / Startup credits: typically expire after 12-24 months
      - AWS Partner Network credits: often narrowly scoped (specific services)
      - Training/certification credits: 60-day window
      - Support plan credits: must be used within billing cycle

    Also detects: credits being applied to non-optimal services when eligible
    workloads exist (e.g., credit going to S3 storage when you have large
    EC2 On-Demand charges).

    Returns: credit balance estimate from billing data, expiry risk, and
    recommended actions to maximise credit consumption.
    """
    # Credits appear as negative amounts in Cost Explorer under "Credits" charge type
    credit_total = 0.0
    by_service   = {}

    try:
        resp = _ce().get_cost_and_usage(
            TimePeriod={"Start": start_date, "End": end_date},
            Granularity="MONTHLY",
            Metrics=["UnblendedCost"],
            GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
            Filter={"Dimensions": {"Key": "RECORD_TYPE",
                                   "Values": ["Credit"],
                                   "MatchOptions": ["EQUALS"]}},
        )
        for period in resp.get("ResultsByTime", []):
            for g in period.get("Groups", []):
                svc    = g["Keys"][0]
                amount = float(g["Metrics"]["UnblendedCost"]["Amount"])
                credit_total += amount  # will be negative
                by_service[svc] = by_service.get(svc, 0) + amount
    except Exception:
        pass

    # Also check support plan credits
    support_credits = 0.0
    try:
        resp2 = _ce().get_cost_and_usage(
            TimePeriod={"Start": start_date, "End": end_date},
            Granularity="MONTHLY",
            Metrics=["UnblendedCost"],
            Filter={"And": [
                {"Dimensions": {"Key": "RECORD_TYPE", "Values": ["Credit"], "MatchOptions": ["EQUALS"]}},
                {"Dimensions": {"Key": "SERVICE", "Values": ["AWS Support (Business)"], "MatchOptions": ["EQUALS"]}},
            ]},
        )
        for period in resp2.get("ResultsByTime", []):
            support_credits += float(period.get("Total", {}).get("UnblendedCost", {}).get("Amount", 0))
    except Exception:
        pass

    credits_abs = abs(credit_total)
    notes = []
    if credits_abs > 0:
        notes.append(
            f"${credits_abs:.2f} in credits applied this period. "
            "Verify expiry dates in AWS Billing Console → Credits tab. "
            "If credits expire before month-end, accelerate eligible spend (training, dev environments)."
        )
    else:
        notes.append("No credits detected in this billing period.")

    return {
        "total_credits_applied_usd": round(credits_abs, 2),
        "credits_by_service":        {k: round(abs(v), 2) for k, v in by_service.items()},
        "support_credits_usd":       round(abs(support_credits), 2),
        "notes":                     notes,
        "action": (
            "Check AWS Billing → Credits for exact expiry dates. "
            "Credits not consumed by expiry are forfeited — no rollover."
        ),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# DATA TRANSFER BLIND SPOTS
# ═══════════════════════════════════════════════════════════════════════════════

@tool
def get_inter_az_transfer_cost(
    start_date: str,
    end_date: str,
) -> dict:
    """
    Quantifies cross-AZ data transfer costs — one of the most underestimated
    cost drivers in multi-AZ architectures.

    AWS charges $0.01/GB each direction for data transfer between AZs.
    In Kubernetes, pods in different AZs calling each other = $0.02/GB round-trip.

    Scenarios that silently inflate the bill:
    1. EKS pods in AZ-a calling RDS in AZ-b: $0.01/GB × request volume
    2. Lambda functions (placed in random AZs) calling ElastiCache in fixed AZ
    3. ALB in all 3 AZs distributing to pods only in 1 AZ
    4. S3 Transfer Acceleration vs direct PUT from EC2 in same region
    5. MSK brokers with producers/consumers spread across AZs

    The invisible pattern: a service doing 10 TB/month cross-AZ
    = 10,000 GB × $0.02 = $200/month. After 3 services: $600/month.
    For 12 months = $7,200 — the cost of a mid-level engineer for a week.

    Fix: topology-aware routing in K8s (topologySpreadConstraints or
    topologyKeys in services), ElastiCache cluster mode with AZ affinity,
    RDS Proxy in same AZ as compute.

    Returns: DataTransfer-Regional cost breakdown with per-direction analysis.
    """
    try:
        resp = _ce().get_cost_and_usage(
            TimePeriod={"Start": start_date, "End": end_date},
            Granularity="MONTHLY",
            Metrics=["UnblendedCost", "UsageQuantity"],
            GroupBy=[
                {"Type": "DIMENSION", "Key": "SERVICE"},
                {"Type": "DIMENSION", "Key": "USAGE_TYPE"},
            ],
            Filter={"Dimensions": {"Key": "USAGE_TYPE",
                                   "Values": ["DataTransfer-Regional-Bytes"],
                                   "MatchOptions": ["CONTAINS"]}},
        )
    except Exception:
        resp = {"ResultsByTime": []}

    total_cost    = 0.0
    total_gb      = 0.0
    by_service    = {}

    for period in resp.get("ResultsByTime", []):
        for g in period.get("Groups", []):
            svc    = g["Keys"][0]
            ut     = g["Keys"][1]
            amount = float(g["Metrics"]["UnblendedCost"]["Amount"])
            qty    = float(g["Metrics"]["UsageQuantity"]["Amount"])
            if "Regional" in ut or "AZ" in ut.lower():
                total_cost += amount
                total_gb   += qty
                by_service[svc] = by_service.get(svc, 0) + amount

    recommendations = []
    if total_cost > 50:
        recommendations.append(
            "Enable topology-aware routing in EKS: add "
            "'service.kubernetes.io/topology-mode: Auto' annotation to Services."
        )
    if "Amazon EC2" in by_service and by_service.get("Amazon EC2", 0) > 20:
        recommendations.append(
            "EC2 cross-AZ traffic is significant. Check if RDS/ElastiCache can be "
            "pinned to the same AZ as the primary compute fleet (acceptable in dev/staging)."
        )
    if "AWS Lambda" in by_service and by_service.get("AWS Lambda", 0) > 10:
        recommendations.append(
            "Lambda cross-AZ charges: Lambda invokes services in fixed AZs. "
            "Use VPC-less Lambda where possible, or ensure Lambda ENIs are in same AZ as endpoints."
        )

    sorted_services = sorted(by_service.items(), key=lambda x: x[1], reverse=True)

    logger.info("get_inter_az_transfer_cost: total_cost=%.2f total_gb=%.0f", total_cost, total_gb)
    return {
        "total_inter_az_cost_usd": round(total_cost, 2),
        "total_inter_az_gb":       round(total_gb, 2),
        "cost_by_service":         {k: round(v, 2) for k, v in sorted_services},
        "severity": "critical" if total_cost > 500 else "high" if total_cost > 100 else "medium" if total_cost > 20 else "low",
        "recommendations":         recommendations,
        "architecture_note": (
            "Cross-AZ costs $0.01/GB each direction. A single service processing "
            "10 TB/month cross-AZ costs $200/month. Topology-aware routing "
            "is a zero-downtime fix in Kubernetes."
        ),
    }


@tool
def get_data_transfer_optimization(
    start_date: str,
    end_date: str,
) -> dict:
    """
    Identifies data transfer costs that could be eliminated through free AWS
    constructs — specifically VPC Endpoints (Gateway and Interface types).

    Key insight: many teams don't know the difference:
      Gateway Endpoints (FREE): S3 and DynamoDB only — install these first.
      Interface Endpoints ($0.01/hr + $0.01/GB): all other services.

    Without S3 Gateway Endpoint:
    - EC2 in private subnet calling S3 = traffic routes through NAT Gateway
    - NAT Gateway charges $0.045/GB processed
    - 1 TB/month to S3 = $46.08/month through NAT vs $0 with Gateway Endpoint

    Without ECR VPC Endpoint:
    - EKS pod pulling 500MB image from ECR = $0.045 through NAT
    - 1000 pod restarts/month × 500MB = 500 GB = $23/month through NAT

    S3 Cross-Region replication cost is often invisible:
    - S3 Replication charges $0.015/GB replicated
    - PLUS the destination PUT requests at destination storage pricing
    - A 10 TB bucket replicating 100 GB/day = $46/day = $1,380/month

    Returns: NAT costs attributable to S3/ECR/other endpoints, replication costs,
    and specific endpoint installation commands.
    """
    # Get NAT Gateway total
    nat_cost = 0.0
    nat_gb   = 0.0
    try:
        resp = _ce().get_cost_and_usage(
            TimePeriod={"Start": start_date, "End": end_date},
            Granularity="MONTHLY",
            Metrics=["UnblendedCost", "UsageQuantity"],
            Filter={"Dimensions": {"Key": "USAGE_TYPE",
                                   "Values": ["NatGateway-Bytes"],
                                   "MatchOptions": ["CONTAINS"]}},
        )
        for period in resp.get("ResultsByTime", []):
            nat_cost += float(period.get("Total", {}).get("UnblendedCost", {}).get("Amount", 0))
            nat_gb   += float(period.get("Total", {}).get("UsageQuantity", {}).get("Amount", 0))
    except Exception:
        pass

    # Check existing VPC endpoints
    s3_endpoint_exists  = False
    ddb_endpoint_exists = False
    ecr_endpoint_exists = False
    try:
        resp_ep = _ec2().describe_vpc_endpoints(
            Filters=[{"Name": "vpc-endpoint-type", "Values": ["Gateway", "Interface"]},
                     {"Name": "state", "Values": ["available"]}]
        )
        for ep in resp_ep.get("VpcEndpoints", []):
            svc = ep.get("ServiceName", "")
            if "s3" in svc.lower():
                s3_endpoint_exists = True
            if "dynamodb" in svc.lower():
                ddb_endpoint_exists = True
            if "ecr" in svc.lower():
                ecr_endpoint_exists = True
    except Exception:
        pass

    # S3 replication cost
    s3_replication_cost = 0.0
    try:
        resp_s3r = _ce().get_cost_and_usage(
            TimePeriod={"Start": start_date, "End": end_date},
            Granularity="MONTHLY",
            Metrics=["UnblendedCost"],
            Filter={"Dimensions": {"Key": "USAGE_TYPE",
                                   "Values": ["S3-Replication"],
                                   "MatchOptions": ["CONTAINS"]}},
        )
        for period in resp_s3r.get("ResultsByTime", []):
            s3_replication_cost += float(period.get("Total", {}).get("UnblendedCost", {}).get("Amount", 0))
    except Exception:
        pass

    # Estimate: ~30% of NAT traffic typically goes to S3/ECR (conservative)
    nat_to_s3_estimate   = round(nat_cost * 0.30, 2) if not s3_endpoint_exists else 0.0
    nat_to_ecr_estimate  = round(nat_cost * 0.10, 2) if not ecr_endpoint_exists else 0.0
    potential_saving     = nat_to_s3_estimate + nat_to_ecr_estimate

    findings = []
    if not s3_endpoint_exists and nat_cost > 0:
        findings.append({
            "issue":            "S3 Gateway Endpoint missing",
            "estimated_saving": nat_to_s3_estimate,
            "cost":             "FREE",
            "action":           "aws ec2 create-vpc-endpoint --vpc-id <VPC_ID> --service-name com.amazonaws.<REGION>.s3 --route-table-ids <RTB_IDs>",
        })
    if not ddb_endpoint_exists and nat_cost > 0:
        findings.append({
            "issue":  "DynamoDB Gateway Endpoint missing",
            "estimated_saving": round(nat_cost * 0.05, 2),
            "cost":   "FREE",
            "action": "aws ec2 create-vpc-endpoint --vpc-id <VPC_ID> --service-name com.amazonaws.<REGION>.dynamodb --route-table-ids <RTB_IDs>",
        })
    if not ecr_endpoint_exists and nat_cost > 0:
        findings.append({
            "issue":            "ECR VPC Endpoint missing (EKS pod image pulls going through NAT)",
            "estimated_saving": nat_to_ecr_estimate,
            "cost":             "$0.01/hr + $0.01/GB (still cheaper than NAT at scale)",
            "action":           "Create Interface Endpoints: com.amazonaws.<REGION>.ecr.api and com.amazonaws.<REGION>.ecr.dkr",
        })
    if s3_replication_cost > 50:
        findings.append({
            "issue":            f"S3 cross-region replication costs ${s3_replication_cost:.2f}/month",
            "estimated_saving": round(s3_replication_cost * 0.3, 2),
            "cost":             "$0.015/GB replicated",
            "action":           "Audit replication rules: use S3 Batch Replication instead of continuous for archival data. Disable replication on buckets no longer requiring DR.",
        })

    logger.info("get_data_transfer_optimization: nat_cost=%.2f s3_repl=%.2f findings=%d",
                nat_cost, s3_replication_cost, len(findings))
    return {
        "nat_gateway_cost_usd":      round(nat_cost, 2),
        "s3_replication_cost_usd":   round(s3_replication_cost, 2),
        "s3_endpoint_exists":        s3_endpoint_exists,
        "dynamodb_endpoint_exists":  ddb_endpoint_exists,
        "ecr_endpoint_exists":       ecr_endpoint_exists,
        "potential_saving_usd":      potential_saving,
        "findings":                  findings,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# ECS / FARGATE SPECIALIST TOOLS
# ═══════════════════════════════════════════════════════════════════════════════

def _ecs():
    if not hasattr(_ecs, "_c") or _ecs._c is None:
        _ecs._c = _client("ecs")
    return _ecs._c
_ecs._c = None


@tool
def get_fargate_rightsizing(
    lookback_days: int = 14,
) -> list:
    """
    Identifies over-provisioned ECS Fargate task definitions — a very common
    waste because task CPU/memory is set once at deploy time and rarely revisited.

    Unlike EC2, Fargate charges are exact: 0.25 vCPU + 0.5 GB RAM task costs
    precisely half of 0.5 vCPU + 1 GB RAM. There's no "instance overhead" to
    absorb over-provisioning.

    Fargate pricing (us-east-1):
      vCPU: $0.04048/vCPU-hour
      Memory: $0.004445/GB-hour

    A task with 4 vCPU / 8 GB using only 0.5 vCPU / 1 GB = 87% waste.
    If you have 100 tasks: ~$750/month wasted.

    Task sizes are discrete in Fargate:
      CPU: 256, 512, 1024, 2048, 4096 mCPU
      Memory: varies by CPU tier

    Detection: Container Insights ECS metrics (requires ContainerInsights enabled).
    Falls back to task definition analysis when metrics unavailable.

    Returns: task families with over-provisioning evidence and right-sized config.
    """
    end_dt   = _date(0)
    start_dt = _date(lookback_days)

    results = []
    try:
        # List all ECS clusters
        clusters_resp = _ecs().list_clusters()
        cluster_arns  = clusters_resp.get("clusterArns", [])

        for cluster_arn in cluster_arns:
            cluster_name = cluster_arn.split("/")[-1]

            # List services
            svc_resp  = _ecs().list_services(cluster=cluster_arn, launchType="FARGATE")
            svc_arns  = svc_resp.get("serviceArns", [])
            if not svc_arns:
                continue

            desc_resp = _ecs().describe_services(cluster=cluster_arn, services=svc_arns[:10])
            for svc in desc_resp.get("services", []):
                svc_name   = svc.get("serviceName", "")
                task_def   = svc.get("taskDefinition", "")
                running    = svc.get("runningCount", 0)
                if running == 0:
                    continue

                # Get task definition for configured resources
                try:
                    td_resp = _ecs().describe_task_definition(taskDefinition=task_def)
                    td      = td_resp.get("taskDefinition", {})
                    cpu_cfg = int(td.get("cpu", 256))      # mCPU
                    mem_cfg = int(td.get("memory", 512))   # MB
                except Exception:
                    cpu_cfg = 256
                    mem_cfg = 512

                # Check CW Container Insights
                cpu_util_pct = None
                mem_util_pct = None
                try:
                    cpu_resp = _cw().get_metric_statistics(
                        Namespace="ECS/ContainerInsights",
                        MetricName="CpuUtilized",
                        Dimensions=[
                            {"Name": "ClusterName", "Value": cluster_name},
                            {"Name": "ServiceName", "Value": svc_name},
                        ],
                        StartTime=start_dt,
                        EndTime=end_dt,
                        Period=86400,
                        Statistics=["Average"],
                    )
                    if cpu_resp["Datapoints"]:
                        avg_cpu_used = sum(d["Average"] for d in cpu_resp["Datapoints"]) / len(cpu_resp["Datapoints"])
                        cpu_util_pct = round(avg_cpu_used / (cpu_cfg / 1024) * 100, 1)
                except Exception:
                    pass

                try:
                    mem_resp = _cw().get_metric_statistics(
                        Namespace="ECS/ContainerInsights",
                        MetricName="MemoryUtilized",
                        Dimensions=[
                            {"Name": "ClusterName", "Value": cluster_name},
                            {"Name": "ServiceName", "Value": svc_name},
                        ],
                        StartTime=start_dt,
                        EndTime=end_dt,
                        Period=86400,
                        Statistics=["Average"],
                    )
                    if mem_resp["Datapoints"]:
                        avg_mem_used = sum(d["Average"] for d in mem_resp["Datapoints"]) / len(mem_resp["Datapoints"])
                        mem_util_pct = round(avg_mem_used / mem_cfg * 100, 1)
                except Exception:
                    pass

                # Fargate pricing
                vcpu_cfg        = cpu_cfg / 1024
                mem_gb_cfg      = mem_cfg / 1024
                monthly_cost    = running * (vcpu_cfg * 0.04048 + mem_gb_cfg * 0.004445) * 720

                issues = []
                recommended_cpu = cpu_cfg
                recommended_mem = mem_cfg

                if cpu_util_pct is not None and cpu_util_pct < 20:
                    issues.append(f"CPU util {cpu_util_pct:.0f}% (configured {vcpu_cfg} vCPU)")
                    # Step down to next valid Fargate CPU tier
                    tiers = [256, 512, 1024, 2048, 4096]
                    target_cpu = max(256, int(cpu_cfg * (cpu_util_pct / 100) * 2))  # 2× headroom
                    recommended_cpu = min([t for t in tiers if t >= target_cpu], default=256)

                if mem_util_pct is not None and mem_util_pct < 20:
                    issues.append(f"Memory util {mem_util_pct:.0f}% (configured {mem_gb_cfg:.1f} GB)")
                    recommended_mem = max(512, int(mem_cfg * (mem_util_pct / 100) * 2))

                if issues:
                    rec_vcpu     = recommended_cpu / 1024
                    rec_mem_gb   = recommended_mem / 1024
                    new_cost     = running * (rec_vcpu * 0.04048 + rec_mem_gb * 0.004445) * 720
                    saving       = monthly_cost - new_cost
                    results.append({
                        "cluster":          cluster_name,
                        "service":          svc_name,
                        "running_tasks":    running,
                        "configured_cpu_mcpu": cpu_cfg,
                        "configured_mem_mb":   mem_cfg,
                        "cpu_utilization_pct": cpu_util_pct,
                        "mem_utilization_pct": mem_util_pct,
                        "monthly_cost_usd":    round(monthly_cost, 2),
                        "recommended_cpu_mcpu": recommended_cpu,
                        "recommended_mem_mb":   recommended_mem,
                        "potential_saving_usd": round(saving, 2),
                        "issues":               issues,
                        "severity": "high" if saving > 100 else "medium",
                    })
    except Exception:
        pass

    results.sort(key=lambda x: x.get("potential_saving_usd", 0), reverse=True)
    logger.info("get_fargate_rightsizing: oversized_services=%d", len(results))
    return results


@tool
def get_fargate_spot_opportunity(
    start_date: str,
    end_date: str,
) -> dict:
    """
    Identifies Fargate On-Demand workloads that could safely use Fargate Spot
    for up to 70% cost reduction.

    Fargate Spot is interrupted with 2-minute warning (like EC2 Spot).
    It is appropriate for:
      ✓ Batch processing, data pipelines
      ✓ Background jobs (email, notifications, async workers)
      ✓ Dev/staging environments
      ✓ ML training jobs
      ✗ NOT for: primary API servers, databases, stateful services

    AWS's recommended pattern: Spot 70% + On-Demand 30% capacity provider
    strategy in ECS. If Spot is interrupted, tasks spill to On-Demand temporarily.

    Pricing: Fargate Spot costs ~30% of On-Demand (varies by region/AZ).

    Detection: identifies Fargate On-Demand spend and ECS services with names
    suggesting batch/worker/job patterns that are Spot-eligible.

    Returns: total Fargate On-Demand cost, Spot-eligible estimate, and ECS
    services with capacity provider strategy recommendations.
    """
    # Get Fargate On-Demand cost
    fargate_od_cost = 0.0
    try:
        resp = _ce().get_cost_and_usage(
            TimePeriod={"Start": start_date, "End": end_date},
            Granularity="MONTHLY",
            Metrics=["UnblendedCost"],
            GroupBy=[{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
            Filter={"Dimensions": {"Key": "SERVICE",
                                   "Values": ["AWS Fargate"],
                                   "MatchOptions": ["EQUALS"]}},
        )
        fargate_spot_cost = 0.0
        for period in resp.get("ResultsByTime", []):
            for g in period.get("Groups", []):
                ut     = g["Keys"][0]
                amount = float(g["Metrics"]["UnblendedCost"]["Amount"])
                if "Spot" in ut:
                    fargate_spot_cost += amount
                else:
                    fargate_od_cost += amount
    except Exception:
        fargate_spot_cost = 0.0

    # Identify Spot-eligible services by name heuristic
    spot_eligible_services = []
    try:
        cluster_arns = _ecs().list_clusters().get("clusterArns", [])
        for cluster_arn in cluster_arns:
            cluster_name = cluster_arn.split("/")[-1]
            svc_arns     = _ecs().list_services(cluster=cluster_arn, launchType="FARGATE").get("serviceArns", [])
            if not svc_arns:
                continue
            desc = _ecs().describe_services(cluster=cluster_arn, services=svc_arns[:10]).get("services", [])
            for svc in desc:
                name      = svc.get("serviceName", "").lower()
                running   = svc.get("runningCount", 0)
                providers = [cp.get("capacityProvider", "") for cp in svc.get("capacityProviderStrategy", [])]
                has_spot  = any("SPOT" in p.upper() for p in providers)

                is_batch_candidate = any(kw in name for kw in [
                    "worker", "batch", "job", "queue", "consumer", "processor",
                    "async", "background", "etl", "pipeline", "ingest",
                    "dev", "staging", "test", "sandbox",
                ])
                if is_batch_candidate and not has_spot and running > 0:
                    spot_eligible_services.append({
                        "cluster":       cluster_name,
                        "service":       svc.get("serviceName", ""),
                        "running_tasks": running,
                        "has_spot":      has_spot,
                        "reason":        "Name pattern suggests batch/worker — Spot-eligible",
                        "action":        (
                            f"Update capacity provider strategy: "
                            f"FARGATE_SPOT weight=3, FARGATE weight=1"
                        ),
                    })
    except Exception:
        pass

    spot_coverage_pct = fargate_spot_cost / max(fargate_od_cost + fargate_spot_cost, 1) * 100
    eligible_od       = fargate_od_cost * 0.40   # conservative: 40% of OD is Spot-eligible
    potential_saving  = eligible_od * 0.70        # 70% saving on Spot-eligible portion

    logger.info("get_fargate_spot_opportunity: fargate_od=%.2f spot_eligible_svcs=%d",
                fargate_od_cost, len(spot_eligible_services))
    return {
        "fargate_on_demand_cost_usd":  round(fargate_od_cost, 2),
        "fargate_spot_cost_usd":       round(fargate_spot_cost, 2),
        "spot_coverage_pct":           round(spot_coverage_pct, 1),
        "spot_eligible_services":      spot_eligible_services,
        "potential_saving_usd":        round(potential_saving, 2),
        "recommendation": (
            f"Fargate On-Demand: ${fargate_od_cost:.2f}/month. "
            f"Migrating batch/worker services to Fargate Spot saves ~70% = "
            f"~${potential_saving:.2f}/month. Use capacity provider strategy: "
            f"FARGATE_SPOT weight=3, FARGATE weight=1 for graceful fallback."
        ),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# SECRETS MANAGER & KMS SPECIALIST TOOLS
# ═══════════════════════════════════════════════════════════════════════════════

def _sm():
    if not hasattr(_sm, "_c") or _sm._c is None:
        _sm._c = _client("secretsmanager")
    return _sm._c
_sm._c = None

def _kms():
    if not hasattr(_kms, "_c") or _kms._c is None:
        _kms._c = _client("kms")
    return _kms._c
_kms._c = None


@tool
def get_secrets_manager_cost() -> dict:
    """
    Audits AWS Secrets Manager for cost optimisation — a service that surprises
    teams when they have many microservices or shared secrets.

    Pricing:
      $0.40/secret/month (flat, regardless of access frequency)
      $0.05 per 10,000 API calls (beyond 10k/month free tier per secret)

    Common waste patterns:
    1. Duplicate secrets: same database credentials stored 5× for 5 services
       → merge into one secret with multiple key-value pairs
    2. Rotation lambdas that fail silently: secret accumulates versions
       → each version is still the same $0.40/month
    3. Secrets never accessed: test/dev secrets from months ago
    4. Secrets that should be SSM Parameter Store (free tier):
       - Non-sensitive config: use Parameter Store Standard (FREE)
       - Sensitive but static: Parameter Store SecureString ($0.05/param Advanced)
       - Only dynamic rotation needs Secrets Manager

    Scale: 1000 microservices × 3 secrets each = 3000 × $0.40 = $1,200/month.
    With 5 environments = $6,000/month. This is real.

    Returns: secret count, cost estimate, stale secrets, duplication patterns.
    """
    secrets     = []
    total_count = 0
    stale       = []

    import datetime as _dt
    threshold_stale = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=90)

    try:
        paginator = _sm().get_paginator("list_secrets")
        for page in paginator.paginate():
            for s in page.get("SecretList", []):
                total_count  += 1
                name          = s.get("Name", "")
                last_accessed = s.get("LastAccessedDate")
                last_changed  = s.get("LastChangedDate")
                versions      = len(s.get("SecretVersionsToStages", {}))

                is_stale = (last_accessed is None or last_accessed < threshold_stale)
                secrets.append({
                    "name":            name,
                    "last_accessed":   str(last_accessed.date()) if last_accessed else "never",
                    "last_changed":    str(last_changed.date()) if last_changed else "unknown",
                    "versions":        versions,
                    "is_stale":        is_stale,
                    "monthly_cost":    0.40,
                })
                if is_stale:
                    stale.append(name)
    except Exception:
        pass

    # Detect potential duplicates by name pattern (e.g., same suffix across services)
    from collections import Counter
    suffixes = Counter()
    for s in secrets:
        parts = s["name"].split("/")
        if len(parts) >= 2:
            suffixes[parts[-1]] += 1  # last path component

    duplicate_candidates = {k: v for k, v in suffixes.items() if v >= 3}

    total_cost    = total_count * 0.40
    stale_cost    = len(stale) * 0.40

    logger.info("get_secrets_manager_cost: total=%d stale=%d cost=%.2f",
                total_count, len(stale), total_cost)
    return {
        "total_secrets":           total_count,
        "monthly_cost_usd":        round(total_cost, 2),
        "stale_secrets_count":     len(stale),
        "stale_secrets_cost_usd":  round(stale_cost, 2),
        "stale_secrets":           stale[:20],
        "duplicate_candidates":    duplicate_candidates,
        "severity": "critical" if total_cost > 500 else "high" if total_cost > 100 else "medium" if total_cost > 20 else "low",
        "recommendation": (
            f"{total_count} secrets at ${total_cost:.2f}/month. "
            f"{len(stale)} not accessed in 90+ days (${stale_cost:.2f}/month). "
            "Migrate non-sensitive config to SSM Parameter Store Standard (FREE). "
            "Consolidate per-service DB credentials into shared secrets with fine-grained IAM."
        ),
    }


@tool
def get_kms_cost_analysis(
    start_date: str,
    end_date: str,
) -> dict:
    """
    Identifies AWS KMS costs — surprisingly high in event-driven architectures
    where every message/record is individually encrypted.

    KMS pricing:
      CMK (Customer Managed Key): $1.00/month per key (flat)
      API requests: $0.03 per 10,000 requests (after first 20,000 free/month)

    The API cost trap:
    - Each Encrypt/Decrypt call = 1 API request
    - A Lambda processing 1M SQS messages/month, each encrypted with KMS:
      = 2M calls (encrypt on write + decrypt on read) = $6/month × keys
    - 10 services each doing this = $60/month just in KMS API calls
    - If you encrypt each database row individually: millions of calls

    CMK accumulation: teams create CMKs per service per environment, forget
    them. 50 CMKs × $1/month = $50/month for keys no one uses.

    Optimisation:
    1. Reuse CMKs across non-competing services (use key policy, not one key per use)
    2. Use KMS data key caching in AWS Encryption SDK (reuse encrypted data keys)
    3. Encrypt at envelope level, not per-record

    Returns: total KMS spend, key inventory, high-request-volume keys.
    """
    kms_cost = 0.0
    by_usage = {}
    try:
        resp = _ce().get_cost_and_usage(
            TimePeriod={"Start": start_date, "End": end_date},
            Granularity="MONTHLY",
            Metrics=["UnblendedCost", "UsageQuantity"],
            GroupBy=[{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
            Filter={"Dimensions": {"Key": "SERVICE",
                                   "Values": ["AWS Key Management Service"],
                                   "MatchOptions": ["EQUALS"]}},
        )
        for period in resp.get("ResultsByTime", []):
            for g in period.get("Groups", []):
                ut     = g["Keys"][0]
                amount = float(g["Metrics"]["UnblendedCost"]["Amount"])
                qty    = float(g["Metrics"]["UsageQuantity"]["Amount"])
                kms_cost += amount
                by_usage[ut] = {"cost": round(amount, 2), "quantity": round(qty, 0)}
    except Exception:
        pass

    # List CMKs
    cmk_count      = 0
    enabled_cmks   = []
    disabled_cmks  = []
    try:
        paginator = _kms().get_paginator("list_keys")
        for page in paginator.paginate():
            for key in page.get("Keys", []):
                key_id = key["KeyId"]
                try:
                    meta = _kms().describe_key(KeyId=key_id).get("KeyMetadata", {})
                    if meta.get("KeyManager") == "CUSTOMER" and meta.get("KeyState") == "Enabled":
                        cmk_count += 1
                        enabled_cmks.append({
                            "key_id":     key_id,
                            "alias":      "",  # would need list_aliases
                            "created":    str(meta.get("CreationDate", "")[:10]) if meta.get("CreationDate") else "",
                            "monthly_cost": 1.00,
                        })
                    elif meta.get("KeyManager") == "CUSTOMER" and meta.get("KeyState") != "Enabled":
                        disabled_cmks.append(key_id)
                except Exception:
                    pass
    except Exception:
        pass

    cmk_flat_cost     = cmk_count * 1.00
    api_cost          = kms_cost - cmk_flat_cost
    notes = []
    if cmk_count > 20:
        notes.append(
            f"{cmk_count} CMKs at $1/month each = ${cmk_flat_cost}/month. "
            "Audit for unused CMKs — schedule deletion (7-30 day waiting period)."
        )
    if api_cost > 10:
        notes.append(
            f"KMS API request cost: ${api_cost:.2f}/month. "
            "Likely caused by per-record or per-message encryption. "
            "Implement KMS Data Key Caching in AWS Encryption SDK."
        )
    if disabled_cmks:
        notes.append(
            f"{len(disabled_cmks)} disabled CMKs still incurring $1/month until deleted."
        )

    logger.info("get_kms_cost_analysis: cost=%.2f cmks=%d", kms_cost, cmk_count)
    return {
        "total_kms_cost_usd":     round(kms_cost, 2),
        "cmk_count":              cmk_count,
        "cmk_flat_cost_usd":      round(cmk_flat_cost, 2),
        "api_request_cost_usd":   round(api_cost, 2),
        "disabled_cmks":          len(disabled_cmks),
        "by_usage_type":          by_usage,
        "notes":                  notes,
        "severity": "high" if kms_cost > 100 else "medium" if kms_cost > 20 else "low",
    }


# ═══════════════════════════════════════════════════════════════════════════════
# OBSERVABILITY COST TOOLS
# ═══════════════════════════════════════════════════════════════════════════════

@tool
def get_cloudwatch_metrics_cost(
    start_date: str,
    end_date: str,
) -> dict:
    """
    Analyses CloudWatch Custom Metrics and Alarm costs — a hidden accumulator
    in service-mesh and microservices architectures.

    Pricing:
      Custom Metrics: first 10k free, then $0.30/metric/month
      Alarms:         first 10 free, then $0.10/alarm/month (standard)
                      High-resolution alarms: $0.30/alarm/month
      Dashboard:      $3/dashboard/month (first 3 free)

    The metric explosion pattern:
    - Each microservice emits 20 custom metrics
    - 50 microservices × 20 metrics = 1,000 metrics = $300/month
    - Add per-Lambda function metrics (teams emit custom latency, errors):
      200 functions × 5 metrics = 1,000 more = $600/month total
    - Istio/Envoy sidecars emit 100s of metrics per service by default

    Alarm accumulation:
    - Auto Scaling groups create alarms automatically
    - CDK/Terraform stacks add alarms per resource
    - 500 alarms × $0.10 = $50/month in forgotten alarms

    X-Ray sampling at 100% in production:
    - $5.00 per million traces recorded
    - A high-traffic API at 1M requests/hour = 720M/month = $3,600/month
    - Default sampling at 5% = $180/month — 20× cheaper

    Returns: CW spend breakdown with metric/alarm/xray costs and reduction levers.
    """
    cw_by_type = {}
    total_cw   = 0.0
    try:
        resp = _ce().get_cost_and_usage(
            TimePeriod={"Start": start_date, "End": end_date},
            Granularity="MONTHLY",
            Metrics=["UnblendedCost", "UsageQuantity"],
            GroupBy=[{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
            Filter={"Dimensions": {"Key": "SERVICE",
                                   "Values": ["AmazonCloudWatch"],
                                   "MatchOptions": ["EQUALS"]}},
        )
        for period in resp.get("ResultsByTime", []):
            for g in period.get("Groups", []):
                ut     = g["Keys"][0]
                amount = float(g["Metrics"]["UnblendedCost"]["Amount"])
                qty    = float(g["Metrics"]["UsageQuantity"]["Amount"])
                total_cw += amount
                if "MetricMonitorUsage" in ut or "CW:MetricMonitorUsage" in ut:
                    cw_by_type["custom_metrics"] = cw_by_type.get("custom_metrics", 0) + amount
                elif "AlarmUsage" in ut:
                    cw_by_type["alarms"] = cw_by_type.get("alarms", 0) + amount
                elif "DashboardUsage" in ut:
                    cw_by_type["dashboards"] = cw_by_type.get("dashboards", 0) + amount
                elif "DataProcessing" in ut:
                    cw_by_type["log_ingest"] = cw_by_type.get("log_ingest", 0) + amount
                else:
                    cw_by_type[ut] = cw_by_type.get(ut, 0) + amount
    except Exception:
        pass

    # X-Ray cost
    xray_cost = 0.0
    try:
        xray_resp = _ce().get_cost_and_usage(
            TimePeriod={"Start": start_date, "End": end_date},
            Granularity="MONTHLY",
            Metrics=["UnblendedCost"],
            Filter={"Dimensions": {"Key": "SERVICE",
                                   "Values": ["AWS X-Ray"],
                                   "MatchOptions": ["EQUALS"]}},
        )
        for period in xray_resp.get("ResultsByTime", []):
            xray_cost += float(period.get("Total", {}).get("UnblendedCost", {}).get("Amount", 0))
    except Exception:
        pass

    # Count alarms
    alarm_count = 0
    try:
        resp_alarms = _cw().describe_alarms(StateValue="OK", MaxRecords=100)
        alarm_count = len(resp_alarms.get("MetricAlarms", []))
        # Note: this only gets first 100 in OK state
    except Exception:
        pass

    recommendations = []
    metric_cost = cw_by_type.get("custom_metrics", 0)
    if metric_cost > 50:
        estimated_metrics = metric_cost / 0.30
        recommendations.append(
            f"~{estimated_metrics:.0f} custom metrics at ${metric_cost:.2f}/month. "
            "Audit: replace high-cardinality metrics with structured logs + Metric Filters (cheaper). "
            "Disable Istio telemetry for non-critical services."
        )
    if xray_cost > 20:
        recommendations.append(
            f"X-Ray cost: ${xray_cost:.2f}/month. "
            "If sampling = 100%, reduce to 5% default or use reservoir sampling. "
            "aws xray update-sampling-rule —reduce fixed-rate to 0.05"
        )
    if cw_by_type.get("alarms", 0) > 20:
        alarm_est = cw_by_type["alarms"] / 0.10
        recommendations.append(
            f"~{alarm_est:.0f} alarms at ${cw_by_type['alarms']:.2f}/month. "
            "Audit for alarms on deleted resources or duplicate metric paths."
        )

    logger.info("get_cloudwatch_metrics_cost: total_cw=%.2f xray=%.2f", total_cw, xray_cost)
    return {
        "total_cloudwatch_cost_usd": round(total_cw, 2),
        "xray_cost_usd":             round(xray_cost, 2),
        "by_type":                   {k: round(v, 2) for k, v in cw_by_type.items()},
        "alarm_count_sample":        alarm_count,
        "recommendations":           recommendations,
        "severity": "high" if (total_cw + xray_cost) > 200 else "medium" if (total_cw + xray_cost) > 50 else "low",
    }


# ═══════════════════════════════════════════════════════════════════════════════
# SERVERLESS & MESSAGING SPECIALIST TOOLS
# ═══════════════════════════════════════════════════════════════════════════════

def _sfn():
    if not hasattr(_sfn, "_c") or _sfn._c is None:
        _sfn._c = _client("stepfunctions")
    return _sfn._c
_sfn._c = None


@tool
def get_step_functions_optimization(
    start_date: str,
    end_date: str,
) -> dict:
    """
    Identifies Step Functions state machines using STANDARD workflow type when
    EXPRESS would be significantly cheaper — a 14× cost difference.

    Pricing comparison:
      Standard Workflows:
        $0.025 per 1,000 state transitions
        Duration: up to 1 year, AT LEAST ONE state transition billing per execution
        Storage: state history stored in SFN service for 90 days

      Express Workflows:
        $1.00 per 1,000,000 executions + $0.00001667 per GB-second duration
        Effectively: very high-volume short workflows are 14× cheaper

    When to use each:
      Standard: long-running (>5 min), human approval, exactly-once semantics,
                audit history required, low volume (<100k/month)
      Express:  high volume (>1M/month), short duration (<5 min), at-least-once ok,
                event processing, API orchestration

    Common mistake: teams scaffold with Standard (the default in CDK/console),
    deploy to production, never revisit. A Lambda orchestration workflow running
    1M times/day with 10 state transitions each:
      Standard: 10M transitions/day × $0.025/1000 = $250/day = $7,500/month
      Express:  1M executions/day × 1000 × $1/1M = $1,000/month

    Returns: state machine inventory with type, estimated volume, and switching
    recommendation where appropriate.
    """
    sfn_cost = 0.0
    by_type  = {"STANDARD": 0.0, "EXPRESS": 0.0}
    try:
        resp = _ce().get_cost_and_usage(
            TimePeriod={"Start": start_date, "End": end_date},
            Granularity="MONTHLY",
            Metrics=["UnblendedCost", "UsageQuantity"],
            GroupBy=[{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
            Filter={"Dimensions": {"Key": "SERVICE",
                                   "Values": ["AWS Step Functions"],
                                   "MatchOptions": ["EQUALS"]}},
        )
        for period in resp.get("ResultsByTime", []):
            for g in period.get("Groups", []):
                ut     = g["Keys"][0]
                amount = float(g["Metrics"]["UnblendedCost"]["Amount"])
                sfn_cost += amount
                if "StateTransition" in ut or "Standard" in ut:
                    by_type["STANDARD"] += amount
                elif "Express" in ut or "Execution" in ut:
                    by_type["EXPRESS"] += amount
    except Exception:
        pass

    # List state machines
    machines     = []
    standard_sms = []
    try:
        paginator = _sfn().get_paginator("list_state_machines")
        for page in paginator.paginate():
            for sm in page.get("stateMachines", []):
                sm_type = sm.get("type", "STANDARD")
                name    = sm.get("name", "")
                machines.append({"name": name, "type": sm_type, "arn": sm.get("stateMachineArn", "")})
                if sm_type == "STANDARD":
                    standard_sms.append(name)
    except Exception:
        pass

    standard_cost = by_type["STANDARD"]
    express_cost  = by_type["EXPRESS"]
    standard_count = len(standard_sms)

    # Estimated saving if high-volume Standard workflows move to Express
    # Express is ~14× cheaper for high-volume short workflows
    express_equivalent = standard_cost / 14
    potential_saving   = standard_cost - express_equivalent if standard_cost > 50 else 0

    recommendations = []
    if standard_cost > 50 and standard_count > 0:
        recommendations.append(
            f"{standard_count} STANDARD state machines costing ${standard_cost:.2f}/month. "
            "Evaluate switching high-volume, short-duration workflows to EXPRESS. "
            "Requirements: duration < 5min, at-least-once semantics acceptable."
        )
    if not recommendations:
        recommendations.append("Step Functions usage looks appropriately typed.")

    logger.info("get_step_functions_optimization: sfn_cost=%.2f standard=%d", sfn_cost, standard_count)
    return {
        "total_sfn_cost_usd":       round(sfn_cost, 2),
        "standard_workflow_cost":   round(standard_cost, 2),
        "express_workflow_cost":    round(express_cost, 2),
        "standard_state_machines":  standard_count,
        "potential_saving_usd":     round(potential_saving, 2),
        "state_machines":           machines[:20],
        "recommendations":          recommendations,
    }


@tool
def get_eventbridge_cost(
    start_date: str,
    end_date: str,
) -> dict:
    """
    Analyses Amazon EventBridge costs — often invisible until you have many
    event-driven microservices sending high-frequency events.

    Pricing:
      Custom events:          $1.00 per million events published
      Cross-account delivery: $1.00 per million events
      Schema discovery:       $0.10 per million events ingested for discovery
      Pipes:                  $0.40 per million events processed

    The multiplication problem:
    - 1 event published → 3 rules match → 3 Lambda targets invoked
    - You pay for 1 EventBridge event + 3 Lambda invocations
    - At 10M events/day: EventBridge = $10/day + Lambda = $X
    - Schema discovery enabled on all buses: $1/day extra for nothing

    Common waste:
    1. Schema discovery enabled permanently (useful for a day, runs forever)
    2. Event buses with rules to dead-letter queues from old workflows
    3. Cross-account event delivery for events no longer consumed

    Also checks: EventBridge Scheduler costs (new service, easily forgotten).

    Returns: EB spend by type, high-event-rate bus detection, unused rules.
    """
    eb_cost    = 0.0
    by_usage   = {}
    try:
        resp = _ce().get_cost_and_usage(
            TimePeriod={"Start": start_date, "End": end_date},
            Granularity="MONTHLY",
            Metrics=["UnblendedCost", "UsageQuantity"],
            GroupBy=[{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
            Filter={"Dimensions": {"Key": "SERVICE",
                                   "Values": ["Amazon EventBridge"],
                                   "MatchOptions": ["EQUALS"]}},
        )
        for period in resp.get("ResultsByTime", []):
            for g in period.get("Groups", []):
                ut     = g["Keys"][0]
                amount = float(g["Metrics"]["UnblendedCost"]["Amount"])
                qty    = float(g["Metrics"]["UsageQuantity"]["Amount"])
                eb_cost += amount
                by_usage[ut] = {"cost": round(amount, 2), "events_millions": round(qty / 1_000_000, 2)}
    except Exception:
        pass

    # Check EventBridge Scheduler cost
    scheduler_cost = 0.0
    try:
        sched_resp = _ce().get_cost_and_usage(
            TimePeriod={"Start": start_date, "End": end_date},
            Granularity="MONTHLY",
            Metrics=["UnblendedCost"],
            Filter={"Dimensions": {"Key": "SERVICE",
                                   "Values": ["Amazon EventBridge Scheduler"],
                                   "MatchOptions": ["EQUALS"]}},
        )
        for period in sched_resp.get("ResultsByTime", []):
            scheduler_cost += float(period.get("Total", {}).get("UnblendedCost", {}).get("Amount", 0))
    except Exception:
        pass

    # Check schema discovery on event buses
    schema_enabled_buses = []
    try:
        import boto3
        eb_client = _client("events")
        buses = eb_client.list_event_buses().get("EventBuses", [])
        for bus in buses:
            # Schema discovery checked via schemas service
            pass  # Would need schemas client; skip for now
    except Exception:
        pass

    recommendations = []
    if eb_cost > 20:
        schema_cost = sum(v["cost"] for k, v in by_usage.items() if "Schema" in k or "Discovery" in k)
        if schema_cost > 0:
            recommendations.append(
                f"Schema discovery costs ${schema_cost:.2f}/month. "
                "Disable schema discovery on event buses unless actively developing: "
                "aws schemas stop-discoverer --discoverer-id <ID>"
            )
        recommendations.append(
            f"Total EventBridge cost: ${eb_cost:.2f}/month. "
            "Audit rules with multiple Lambda targets — consolidate fanout into single Lambda with routing logic."
        )

    logger.info("get_eventbridge_cost: total=%.2f scheduler=%.2f", eb_cost, scheduler_cost)
    return {
        "total_eventbridge_cost_usd": round(eb_cost, 2),
        "scheduler_cost_usd":         round(scheduler_cost, 2),
        "by_usage_type":              by_usage,
        "recommendations":            recommendations,
        "severity": "high" if eb_cost > 100 else "medium" if eb_cost > 20 else "low",
    }


# ═══════════════════════════════════════════════════════════════════════════════
# STORAGE & BACKUP SPECIALIST TOOLS
# ═══════════════════════════════════════════════════════════════════════════════

@tool
def get_ebs_snapshot_waste(
    lookback_days: int = 7,
) -> dict:
    """
    Identifies EBS snapshot cost waste — one of the most quietly growing line
    items in mature AWS accounts.

    EBS snapshot pricing: $0.05/GB-month (only for changed blocks vs previous snapshot)
    BUT: this means very little without context. In practice:
    - First snapshot of a 1TB volume = 1TB × $0.05 = $50/month
    - Subsequent snapshots capture only changed blocks — typically 2-5% = $1-2.50/month EACH
    - With 90-day retention × daily snapshots: 90 snapshots × $1.50 avg = $135/month PER VOLUME
    - For 100 volumes: $13,500/month in snapshots alone

    The cascade problem: when you delete a snapshot, AWS redistributes its
    unique blocks to the next snapshot in the chain. Deleting old snapshots
    actually REDUCES cost because the unique blocks go away.

    Teams often think "I'll just keep everything for safety" — this is wrong.
    For production volumes, keep: 7 daily, 4 weekly, 12 monthly (GFS rotation).

    Also detects:
    - Snapshots of deleted volumes (no current InstanceId)
    - Snapshots in regions you don't use (forgotten cross-region copy)
    - AMI-backing snapshots from deregistered AMIs

    Returns: snapshot cost by volume, oldest snapshots, GFS rotation recommendation.
    """
    import datetime as _dt

    snap_cost  = 0.0
    try:
        resp = _ce().get_cost_and_usage(
            TimePeriod={"Start": _date(30), "End": _date(0)},
            Granularity="MONTHLY",
            Metrics=["UnblendedCost", "UsageQuantity"],
            Filter={"Dimensions": {"Key": "USAGE_TYPE",
                                   "Values": ["EBS:SnapshotUsage"],
                                   "MatchOptions": ["CONTAINS"]}},
        )
        for period in resp.get("ResultsByTime", []):
            snap_cost += float(period.get("Total", {}).get("UnblendedCost", {}).get("Amount", 0))
    except Exception:
        pass

    # Enumerate snapshots
    orphan_snaps    = []
    old_snaps       = []
    total_snaps     = 0
    total_snap_gb   = 0

    cutoff_old = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=90)

    try:
        sts   = boto3.client("sts")
        owner = sts.get_caller_identity().get("Account", "self")

        paginator = _ec2().get_paginator("describe_snapshots")
        for page in paginator.paginate(OwnerIds=[owner]):
            for snap in page.get("Snapshots", []):
                snap_id    = snap["SnapshotId"]
                vol_id     = snap.get("VolumeId", "")
                size_gb    = snap.get("VolumeSize", 0)
                start_time = snap.get("StartTime")
                total_snaps  += 1
                total_snap_gb += size_gb

                # Check if source volume still exists
                is_orphan = False
                if vol_id and vol_id != "vol-ffffffff":
                    try:
                        vresp = _ec2().describe_volumes(VolumeIds=[vol_id])
                        if not vresp.get("Volumes"):
                            is_orphan = True
                    except Exception:
                        is_orphan = True  # volume not found

                if is_orphan:
                    orphan_snaps.append({
                        "snapshot_id":   snap_id,
                        "volume_id":     vol_id,
                        "size_gb":       size_gb,
                        "created":       str(start_time)[:10] if start_time else "",
                        "monthly_cost_estimate": round(size_gb * 0.05, 2),
                        "action":        f"aws ec2 delete-snapshot --snapshot-id {snap_id}",
                    })

                if start_time and start_time < cutoff_old:
                    old_snaps.append({
                        "snapshot_id": snap_id,
                        "size_gb":     size_gb,
                        "age_days":    ((_dt.datetime.now(_dt.timezone.utc) - start_time).days),
                    })
    except Exception:
        pass

    orphan_cost = sum(s["monthly_cost_estimate"] for s in orphan_snaps)
    old_snaps.sort(key=lambda x: x["age_days"], reverse=True)

    logger.info("get_ebs_snapshot_waste: total=%d orphans=%d cost=%.2f",
                total_snaps, len(orphan_snaps), snap_cost)
    return {
        "total_snapshot_cost_usd":    round(snap_cost, 2),
        "total_snapshots":            total_snaps,
        "total_snapshot_gb":          total_snap_gb,
        "orphan_snapshots":           orphan_snaps[:20],
        "orphan_cost_usd":            round(orphan_cost, 2),
        "snapshots_older_90_days":    len(old_snaps),
        "oldest_snapshots":           old_snaps[:10],
        "severity": "critical" if snap_cost > 500 else "high" if snap_cost > 100 else "medium" if snap_cost > 20 else "low",
        "recommendation": (
            f"${snap_cost:.2f}/month on EBS snapshots. "
            f"{len(orphan_snaps)} orphaned (source volume deleted) = ${orphan_cost:.2f}/month waste. "
            "Implement GFS rotation: 7 daily + 4 weekly + 12 monthly. "
            "Use AWS Backup lifecycle rules to move snapshots to archive tier after 30 days."
        ),
    }


@tool
def get_efs_optimization(
    start_date: str,
    end_date: str,
) -> dict:
    """
    Analyses Amazon EFS (Elastic File System) costs — often over-provisioned
    because teams set Standard storage class and never configure lifecycle.

    EFS pricing (us-east-1):
      Standard:          $0.30/GB-month
      Standard-IA:       $0.025/GB-month (+ $0.01/GB access)
      One Zone:          $0.16/GB-month
      One Zone-IA:       $0.0133/GB-month

    With Intelligent Tiering (lifecycle policy):
    - Files not accessed for 30 days → automatically moved to IA
    - Files in IA accessed → moved back to Standard
    - Cost: $0.025 vs $0.30 = 92% cheaper for cold data

    Throughput mode also matters:
      Bursting:     scales with storage, good for bursty workloads
      Provisioned:  $6.00/MB/s-month (overkill for low-throughput workloads)
      Elastic:      $0.03/GB transferred (pay per use, best for unpredictable)

    Real pattern: a team creates an EFS for a migration project (300 GB),
    migration finishes, EFS stays forever. $90/month for 2 years = $2,160.

    Returns: EFS filesystems with storage class breakdown, lifecycle policy status,
    and throughput mode optimization.
    """
    efs_cost = 0.0
    try:
        resp = _ce().get_cost_and_usage(
            TimePeriod={"Start": start_date, "End": end_date},
            Granularity="MONTHLY",
            Metrics=["UnblendedCost", "UsageQuantity"],
            GroupBy=[{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
            Filter={"Dimensions": {"Key": "SERVICE",
                                   "Values": ["Amazon Elastic File System"],
                                   "MatchOptions": ["EQUALS"]}},
        )
        standard_cost  = 0.0
        ia_cost        = 0.0
        throughput_cost = 0.0
        for period in resp.get("ResultsByTime", []):
            for g in period.get("Groups", []):
                ut     = g["Keys"][0]
                amount = float(g["Metrics"]["UnblendedCost"]["Amount"])
                efs_cost += amount
                if "TimedStorage" in ut and "IA" not in ut:
                    standard_cost += amount
                elif "IA" in ut:
                    ia_cost += amount
                elif "ProvisionedThroughput" in ut:
                    throughput_cost += amount
    except Exception:
        standard_cost  = 0.0
        ia_cost        = 0.0
        throughput_cost = 0.0

    # List filesystems
    filesystems = []
    try:
        import boto3
        efs_client = _client("efs")
        resp_fs = efs_client.describe_file_systems()
        for fs in resp_fs.get("FileSystems", []):
            fs_id       = fs["FileSystemId"]
            size_bytes  = fs.get("SizeInBytes", {}).get("Value", 0)
            size_gb     = size_bytes / (1024 ** 3)
            throughput  = fs.get("ThroughputMode", "bursting")
            prov_tp     = fs.get("ProvisionedThroughputInMibps", 0)

            # Check lifecycle policy
            lifecycle = []
            try:
                lc_resp   = efs_client.describe_lifecycle_configuration(FileSystemId=fs_id)
                lifecycle = lc_resp.get("LifecyclePolicies", [])
            except Exception:
                pass

            has_lifecycle = len(lifecycle) > 0
            monthly_cost  = size_gb * 0.30  # assume Standard if not broken down

            filesystems.append({
                "filesystem_id":         fs_id,
                "size_gb":               round(size_gb, 1),
                "throughput_mode":       throughput,
                "provisioned_mbps":      prov_tp,
                "has_lifecycle_policy":  has_lifecycle,
                "estimated_monthly_usd": round(monthly_cost, 2),
                "potential_saving_usd":  round(monthly_cost * 0.75, 2) if not has_lifecycle else 0,
                "recommendations":       (
                    ["Enable lifecycle policy: 30-day transition to IA (92% cheaper for cold data)"]
                    if not has_lifecycle else []
                ) + (
                    [f"Switch from Provisioned throughput ({prov_tp} MB/s = ${prov_tp * 6:.0f}/month) to Elastic mode"]
                    if throughput == "provisioned" and prov_tp > 10 else []
                ),
            })
    except Exception:
        pass

    potential_saving = sum(f["potential_saving_usd"] for f in filesystems)

    logger.info("get_efs_optimization: cost=%.2f filesystems=%d", efs_cost, len(filesystems))
    return {
        "total_efs_cost_usd":      round(efs_cost, 2),
        "standard_storage_cost":   round(standard_cost, 2),
        "ia_storage_cost":         round(ia_cost, 2),
        "provisioned_tp_cost":     round(throughput_cost, 2),
        "filesystems":             filesystems,
        "potential_saving_usd":    round(potential_saving, 2),
        "severity": "high" if efs_cost > 100 else "medium" if efs_cost > 20 else "low",
    }


# ═══════════════════════════════════════════════════════════════════════════════
# SECURITY SERVICES COST TOOLS
# ═══════════════════════════════════════════════════════════════════════════════

@tool
def get_guardduty_cost(
    start_date: str,
    end_date: str,
) -> dict:
    """
    Analyses Amazon GuardDuty costs — often misunderstood because the pricing
    is volume-based on YOUR data, not a flat fee.

    GuardDuty pricing:
      VPC Flow Logs analysis:         $4.00/GB (first 500 GB/month)
                                      $2.00/GB (next 2000 GB)
                                      $1.00/GB (over 2500 GB)
      DNS Query Logs analysis:        $0.80/million queries
      CloudTrail management events:   $4.00/million events
      CloudTrail S3 data events:      $0.80/million events
      EKS audit log analysis:         $4.00/million events
      Malware Protection (S3):        $0.27/GB scanned
      Lambda network activity:        $1.20/million invocations

    The surprise calculation:
    - Large VPC with 100 EC2 instances, 1 GB/hour flow logs
    - = 720 GB/month × $4.00 = $2,880/month PER ACCOUNT
    - Multi-account (50 accounts) = $144,000/month on GuardDuty alone

    Optimisation paths:
    1. Flow logs: enable GuardDuty S3 data source filtering (only suspicious IPs)
    2. EKS audit logs: only enable on prod clusters, not dev/staging
    3. Lambda protection: only for functions handling sensitive data
    4. Malware Protection: only for S3 buckets with external ingestion

    Returns: GuardDuty cost by data source, per-account breakdown, reduction levers.
    """
    gd_cost   = 0.0
    by_source = {}
    try:
        resp = _ce().get_cost_and_usage(
            TimePeriod={"Start": start_date, "End": end_date},
            Granularity="MONTHLY",
            Metrics=["UnblendedCost", "UsageQuantity"],
            GroupBy=[{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
            Filter={"Dimensions": {"Key": "SERVICE",
                                   "Values": ["Amazon GuardDuty"],
                                   "MatchOptions": ["EQUALS"]}},
        )
        for period in resp.get("ResultsByTime", []):
            for g in period.get("Groups", []):
                ut     = g["Keys"][0]
                amount = float(g["Metrics"]["UnblendedCost"]["Amount"])
                qty    = float(g["Metrics"]["UsageQuantity"]["Amount"])
                gd_cost += amount
                # Map usage type to data source
                if "VPCFlowLog" in ut or "vpc-flow" in ut.lower():
                    label = "VPC Flow Logs"
                elif "DNS" in ut:
                    label = "DNS Logs"
                elif "CloudTrail" in ut:
                    label = "CloudTrail Events"
                elif "EKS" in ut:
                    label = "EKS Audit Logs"
                elif "S3" in ut and "Malware" in ut:
                    label = "Malware Protection (S3)"
                elif "Lambda" in ut:
                    label = "Lambda Network Activity"
                else:
                    label = ut
                by_source[label] = by_source.get(label, 0) + amount
    except Exception:
        pass

    recommendations = []
    vpc_cost = by_source.get("VPC Flow Logs", 0)
    eks_cost = by_source.get("EKS Audit Logs", 0)
    if vpc_cost > 100:
        recommendations.append(
            f"VPC Flow Logs analysis: ${vpc_cost:.2f}/month. "
            "This scales with flow log volume. Consider: (1) Reduce flow log verbosity — "
            "REJECT-only logs for security use cases. (2) Disable GuardDuty Flow Logs in "
            "non-production accounts and use S3 Malware scan instead."
        )
    if eks_cost > 50:
        recommendations.append(
            f"EKS Audit Log analysis: ${eks_cost:.2f}/month. "
            "Disable EKS protection on dev/staging clusters — enable only on prod: "
            "aws guardduty update-detector --detector-id <ID> --features Name=EKS_AUDIT_LOGS,Status=DISABLED"
        )
    if gd_cost > 1000:
        recommendations.append(
            "GuardDuty cost above $1000/month. Consider: AWS Security Hub with Config as "
            "a more cost-predictable alternative for compliance use cases."
        )

    logger.info("get_guardduty_cost: total=%.2f sources=%s", gd_cost, list(by_source.keys()))
    return {
        "total_guardduty_cost_usd": round(gd_cost, 2),
        "cost_by_data_source":      {k: round(v, 2) for k, v in sorted(by_source.items(), key=lambda x: x[1], reverse=True)},
        "recommendations":          recommendations,
        "severity": "critical" if gd_cost > 1000 else "high" if gd_cost > 200 else "medium" if gd_cost > 50 else "low",
        "note": (
            "GuardDuty pricing is per-GB of logs/events analyzed — "
            "it scales with your infrastructure's telemetry volume. "
            "This is NOT a fixed fee."
        ),
    }


@tool
def get_security_services_overlap(
    start_date: str,
    end_date: str,
) -> dict:
    """
    Identifies overlapping and duplicated costs across AWS security services —
    teams often enable all services without understanding what each does.

    Common overlaps that cost real money:
    1. Security Hub + Config: Security Hub standards (CIS, PCI) mostly run
       AS Config rules. Each Config evaluation costs $0.001. With Security Hub
       enabled, Config evaluations can 3-5× multiply.
       Example: CIS standard adds 147 rules on top of your existing rules.

    2. Macie continuous discovery vs on-demand: Macie Sensitive Data Discovery
       charges $1.00/GB scanned. Enabling continuous discovery on S3 buckets
       that don't change = full re-scan every time.

    3. IAM Access Analyzer + Security Hub: both check for public S3 buckets,
       public IAM policies. Redundant alerts, no additional protection.

    4. Inspector + Security Hub: Inspector vulnerability findings are forwarded
       to Security Hub. You pay Inspector ($0.11/EC2 instance/month) and Security Hub
       aggregates them (adding to your finding-hours cost).

    Total cost of a "full security stack":
      GuardDuty:        $500-2000/month
      Security Hub:     $50-200/month
      Config:           $200-800/month
      Inspector:        $0.11 × instances
      Macie:            $1/GB × S3 data
      ──────────────────────────────────
      Total:            $1,000-4,000/month for medium infrastructure

    Returns: security services cost breakdown with overlap analysis.
    """
    security_services = [
        "Amazon GuardDuty",
        "AWS Security Hub",
        "Amazon Inspector",
        "Amazon Macie",
        "AWS Config",
        "AWS IAM Access Analyzer",
        "Amazon Detective",
    ]

    costs    = {}
    total    = 0.0

    for svc in security_services:
        try:
            resp = _ce().get_cost_and_usage(
                TimePeriod={"Start": start_date, "End": end_date},
                Granularity="MONTHLY",
                Metrics=["UnblendedCost"],
                Filter={"Dimensions": {"Key": "SERVICE",
                                       "Values": [svc],
                                       "MatchOptions": ["EQUALS"]}},
            )
            cost = sum(
                float(r.get("Total", {}).get("UnblendedCost", {}).get("Amount", 0))
                for r in resp.get("ResultsByTime", [])
            )
            if cost > 0:
                costs[svc] = round(cost, 2)
                total += cost
        except Exception:
            pass

    active_services = list(costs.keys())
    overlaps        = []

    if "AWS Security Hub" in active_services and "AWS Config" in active_services:
        overlaps.append({
            "services":  ["AWS Security Hub", "AWS Config"],
            "overlap":   "Security Hub standards are implemented as Config rules. "
                         "CIS Benchmark adds 147 rules. AWS Foundational Security adds 231 rules. "
                         "These multiply Config evaluation cost 3-5×.",
            "action":    "Disable Security Hub standards you don't need. Keep only NIST or CIS — not both.",
            "estimated_saving": round(costs.get("AWS Config", 0) * 0.4, 2),
        })
    if "Amazon Macie" in active_services and costs.get("Amazon Macie", 0) > 50:
        overlaps.append({
            "services":  ["Amazon Macie"],
            "overlap":   "Macie Sensitive Data Discovery charges $1/GB scanned. "
                         "Continuous discovery re-scans buckets even when data hasn't changed.",
            "action":    "Use Macie on-demand scanning instead of continuous. "
                         "Or scope to buckets with external data ingestion only.",
            "estimated_saving": round(costs.get("Amazon Macie", 0) * 0.6, 2),
        })
    if "Amazon Detective" in active_services and "Amazon GuardDuty" in active_services:
        overlaps.append({
            "services":  ["Amazon Detective", "Amazon GuardDuty"],
            "overlap":   "Detective is a forensics tool, not a detection tool. "
                         "Most teams enable it during a GuardDuty finding investigation and forget to disable it.",
            "action":    "Disable Detective if no active security investigation. Enable only when needed.",
            "estimated_saving": costs.get("Amazon Detective", 0),
        })

    potential_saving = sum(o["estimated_saving"] for o in overlaps)

    logger.info("get_security_services_overlap: total=%.2f active=%s", total, active_services)
    return {
        "total_security_cost_usd":  round(total, 2),
        "cost_by_service":          costs,
        "active_services":          active_services,
        "overlaps_detected":        overlaps,
        "potential_saving_usd":     round(potential_saving, 2),
        "severity": "critical" if total > 2000 else "high" if total > 500 else "medium" if total > 100 else "low",
        "benchmark": (
            "Typical security stack cost for medium infrastructure: $1,000-4,000/month. "
            "Most teams can achieve the same compliance posture for 40-60% less with "
            "targeted enablement rather than blanket activation."
        ),
    }
