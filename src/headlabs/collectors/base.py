"""Base collector abstract class."""

from abc import ABC, abstractmethod

import boto3


class BaseCollector(ABC):
    """Abstract base for all data collectors."""

    def __init__(self, session: boto3.Session):
        self.session = session

    @abstractmethod
    def collect(self, **kwargs) -> dict:
        """Collect data and return structured dict."""
        ...

    @property
    @abstractmethod
    def required_permissions(self) -> list[str]:
        """IAM permissions required by this collector."""
        ...
