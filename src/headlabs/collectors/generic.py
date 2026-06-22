"""Generic collector — just gathers account identity. Agent uses its own tools."""

from .base import BaseCollector


class GenericCollector(BaseCollector):
    required_permissions = ["sts:GetCallerIdentity"]

    def collect(self, **kwargs):
        sts = self.session.client("sts")
        identity = sts.get_caller_identity()
        return {
            "account_id": identity["Account"],
            "arn": identity["Arn"],
        }
