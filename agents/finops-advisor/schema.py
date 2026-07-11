"""Schema for finops-advisor — LLM-powered FinOps intelligence agent."""
from __future__ import annotations
import json
import re
from typing import Optional
from pydantic import BaseModel, Field, field_validator

from headlabs_sdk.sdk import CloudTargetInput


class FinOpsAdvisorInput(CloudTargetInput):
    # Cloud-target credential fields (account_id, aws_*, target_role_arn, aws_region)
    # come from CloudTargetInput — the canonical client-side ephemeral pattern.
    tenant_id:       str
    question:        Optional[str] = None
    lookback_days:   int = 30


class FinOpsInsight(BaseModel):
    # Tolerant to LLM output variance — coerce non-strings, ignore extras (mirror SecurityFinding).
    model_config = {"extra": "ignore"}

    category:   str = "general"
    severity:   str = "medium"
    title:      str = ""
    finding:    str = ""
    evidence:   dict = Field(default_factory=dict)
    action:     str = ""
    saving_usd: Optional[float] = None

    @field_validator("category", "severity", "title", "finding", "action", mode="before")
    @classmethod
    def _stringify(cls, v):
        if v is None:
            return ""
        return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)

    @field_validator("evidence", mode="before")
    @classmethod
    def _wrap_evidence(cls, v):
        return v if isinstance(v, dict) else ({} if v is None else {"detail": v})

    @field_validator("saving_usd", mode="before")
    @classmethod
    def _num(cls, v):
        if v is None or isinstance(v, (int, float)):
            return v
        m = re.search(r"-?\d+(?:\.\d+)?", str(v).replace(",", ""))   # strip "$", "/mês", etc.
        return float(m.group()) if m else None


class FinOpsAdvisorOutput(BaseModel):
    model_config = {"extra": "ignore"}

    tenant_id:        str = ""
    question:         Optional[str]   = None
    insights:         list[FinOpsInsight] = Field(default_factory=list)
    summary:          Optional[str]   = None
    total_saving_usd: float           = 0.0
    error:            Optional[str]   = None

    @field_validator("total_saving_usd", mode="before")
    @classmethod
    def _num(cls, v):
        if v is None:
            return 0.0
        if isinstance(v, (int, float)):
            return v
        m = re.search(r"-?\d+(?:\.\d+)?", str(v).replace(",", ""))
        return float(m.group()) if m else 0.0
