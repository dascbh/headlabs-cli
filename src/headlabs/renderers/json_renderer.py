"""JSON report renderer."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from headlabs.result import Result


def render_json(result: Result, path: str) -> None:
    """Save structured JSON report."""
    data = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        **asdict(result),
    }
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, indent=2, default=str))
