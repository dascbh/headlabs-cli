"""Data collectors for HeadLabs agents."""

from headlabs.collectors.base import BaseCollector

COLLECTOR_REGISTRY = {
    "finops": "headlabs.collectors.finops.FinOpsCollector",
}

__all__ = ["BaseCollector", "COLLECTOR_REGISTRY"]
