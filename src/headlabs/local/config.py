"""Configuration for the `headlabs local` agent runtime.

Deliberately separate from ``headlabs.config`` (platform ``api_key``/``api_url``):
this file describes a self-hosted LLM endpoint, not the HeadLabs platform, and
the two must never be conflated.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path

LOCAL_CONFIG_DIR = Path.home() / ".headlabs"
LOCAL_CONFIG_FILE = LOCAL_CONFIG_DIR / "local_config.json"

DEFAULT_MAX_ITERATIONS = 30


@dataclass
class LocalConfig:
    base_url: str | None = None
    model: str | None = None
    api_key: str | None = None  # often a dummy value for self-hosted servers
    max_iterations: int = DEFAULT_MAX_ITERATIONS
    timeout_s: float = 120.0

    def is_configured(self) -> bool:
        return bool(self.base_url and self.model)


def load_local_config() -> LocalConfig:
    """Load ``~/.headlabs/local_config.json``, or defaults if absent/corrupt."""
    if not LOCAL_CONFIG_FILE.exists():
        return LocalConfig()
    try:
        data = json.loads(LOCAL_CONFIG_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return LocalConfig()
    known = {f for f in LocalConfig.__dataclass_fields__}
    return LocalConfig(**{k: v for k, v in data.items() if k in known})


def save_local_config(cfg: LocalConfig) -> None:
    LOCAL_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    LOCAL_CONFIG_FILE.write_text(json.dumps(asdict(cfg), indent=2))


def update_local_config(**kwargs) -> LocalConfig:
    """Load current config, apply non-None overrides, persist, return it."""
    cfg = load_local_config()
    for key, value in kwargs.items():
        if value is not None and hasattr(cfg, key):
            setattr(cfg, key, value)
    save_local_config(cfg)
    return cfg
