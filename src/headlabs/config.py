"""Configuration management for HeadLabs CLI."""

import json
from pathlib import Path

CONFIG_DIR = Path.home() / ".headlabs"
CONFIG_FILE = CONFIG_DIR / "config.json"
REPORTS_DIR = Path("./reports")

DEFAULT_API_URL = "https://api.headlabs.ai/api/v1"


def load_config() -> dict:
    """Load config from ~/.headlabs/config.json."""
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text())
    return {}


def save_config(config: dict) -> None:
    """Save config to ~/.headlabs/config.json."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(config, indent=2))


def get_api_key() -> str | None:
    return load_config().get("api_key")


def get_api_url() -> str:
    return load_config().get("api_url", DEFAULT_API_URL)


def get_tenant() -> str | None:
    """Optional tenant override (used to poll executions on the chat path,
    where the server may not echo the tenant)."""
    return load_config().get("tenant")
