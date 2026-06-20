"""FinOps data collector using AWS Cost Explorer."""

from __future__ import annotations

from datetime import datetime, timedelta

from headlabs.collectors.base import BaseCollector


class FinOpsCollector(BaseCollector):
    """Collects cost data from AWS Cost Explorer."""

    @property
    def required_permissions(self) -> list[str]:
        return ["ce:GetCostAndUsage"]

    def collect(self, **kwargs) -> dict:
        days = kwargs.get("days", 30)
        ce = self.session.client("ce", region_name="us-east-1")

        end = datetime.utcnow().date()
        start = end - timedelta(days=days)
        time_period = {"Start": str(start), "End": str(end)}

        # Total cost
        total_resp = ce.get_cost_and_usage(
            TimePeriod=time_period,
            Granularity="MONTHLY",
            Metrics=["UnblendedCost"],
        )
        total_usd = sum(
            float(r["Total"]["UnblendedCost"]["Amount"])
            for r in total_resp["ResultsByTime"]
        )

        # By service
        svc_resp = ce.get_cost_and_usage(
            TimePeriod=time_period,
            Granularity="MONTHLY",
            Metrics=["UnblendedCost"],
            GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
        )
        top_services = {}
        for result in svc_resp["ResultsByTime"]:
            for group in result.get("Groups", []):
                svc = group["Keys"][0]
                amt = float(group["Metrics"]["UnblendedCost"]["Amount"])
                top_services[svc] = top_services.get(svc, 0.0) + amt
        top_services = dict(
            sorted(top_services.items(), key=lambda x: x[1], reverse=True)[:10]
        )

        # By linked account
        acct_resp = ce.get_cost_and_usage(
            TimePeriod=time_period,
            Granularity="MONTHLY",
            Metrics=["UnblendedCost"],
            GroupBy=[{"Type": "DIMENSION", "Key": "LINKED_ACCOUNT"}],
        )
        by_account = {}
        for result in acct_resp["ResultsByTime"]:
            for group in result.get("Groups", []):
                acct = group["Keys"][0]
                amt = float(group["Metrics"]["UnblendedCost"]["Amount"])
                by_account[acct] = by_account.get(acct, 0.0) + amt

        return {
            "period": {"start": str(start), "end": str(end), "days": days},
            "total_usd": round(total_usd, 2),
            "top_services": {k: round(v, 2) for k, v in top_services.items()},
            "by_account": {k: round(v, 2) for k, v in by_account.items()},
        }
