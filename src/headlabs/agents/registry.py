"""Agent registry with available agent configurations."""

AGENT_REGISTRY: dict[str, dict] = {
    "finops": {
        "agent_id": "finops-advisor",
        "chat_agent_id": "finops-advisor",
        "collector": "finops",
        "description": "Analyzes AWS costs and recommends optimizations",
    },
    "threat-detector": {
        "agent_id": "threat-detector",
        "collector": "security",
        "description": "Detects security threats and misconfigurations",
    },
    "compliance": {
        "agent_id": "compliance-auditor",
        "collector": "compliance",
        "description": "Audits compliance against frameworks (CIS, SOC2, HIPAA)",
    },
    "reliability": {
        "agent_id": "reliability-advisor",
        "collector": "infrastructure",
        "description": "Evaluates infrastructure reliability and suggests improvements",
    },
}
