"""Schema for threat-detector — AWS security posture reasoning agent."""
from __future__ import annotations
import json
from typing import Any, Optional
from pydantic import BaseModel, Field, field_validator

from headlabs_sdk.sdk import CloudTargetInput


class ThreatDetectorInput(CloudTargetInput):
    # Cloud-target credential fields come from CloudTargetInput (canonical
    # client-side ephemeral pattern).
    tenant_id:       str
    question:        Optional[str] = None


class SecurityFinding(BaseModel):
    # Tolerant to LLM output variance — coerce non-strings, ignore extra keys.
    model_config = {"extra": "ignore"}

    category:    str = "general"
    severity:    str = "medium"
    title:       str = ""
    finding:     str = ""
    evidence:    dict = Field(default_factory=dict)
    remediation: str = ""
    resource:    Optional[str] = None
    frameworks:  list[str] = Field(default_factory=list)  # e.g. ["CIS 1.4", "NIST AC-2"]
    id:          Optional[str] = None
    # API-remediable action for the platform "Fix" button (allowlisted, reversible only).
    remediation_action: Optional[dict] = None  # {"action_id": "...", "params": {...}}

    @field_validator("category", "severity", "title", "finding", "remediation", mode="before")
    @classmethod
    def _stringify(cls, v):
        if v is None:
            return ""
        return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)

    @field_validator("evidence", mode="before")
    @classmethod
    def _wrap_evidence(cls, v):
        return v if isinstance(v, dict) else ({} if v is None else {"detail": v})

    @field_validator("frameworks", mode="before")
    @classmethod
    def _listify(cls, v):
        if v is None:
            return []
        return v if isinstance(v, list) else [str(v)]


class ThreatDetectorOutput(BaseModel):
    model_config = {"extra": "ignore"}

    tenant_id:      str = ""
    question:       Optional[str] = None
    findings:       list[SecurityFinding] = Field(default_factory=list)
    summary:        Optional[str] = None
    critical_count: int = 0
    error:          Optional[str] = None
