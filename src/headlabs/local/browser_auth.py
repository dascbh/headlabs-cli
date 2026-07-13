"""Browser authentication for `headlabs local` page inspection.

Describes how to authenticate an inspected page so the usability/frontend
inspector can drive a login-gated site — or a locally-served app behind auth —
instead of only publicly reachable URLs.

All three mechanisms map directly onto Playwright's ``browser.new_context(...)``
keyword arguments, so a single :class:`BrowserAuth` value flows unchanged into
both the deterministic probe (``browser_probe``) and the LLM-driven
``browser_devtools`` tool:

- ``storage_state``: a saved logged-in session (cookies + localStorage) captured
  once with ``playwright ... storage_state``. The RECOMMENDED path — no password
  ever touches the CLI, and it survives SPA/JWT sessions the same way.
- ``http_credentials``: HTTP Basic auth (``user:password``).
- ``extra_http_headers``: static headers, e.g. an ``Authorization: Bearer <token>``.

Note: these apply to the *local* browser paths. The remote browser-devtools MCP
exposes no auth parameters, so an authenticated target is inherently a
local/served-browser scenario.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class BrowserAuth:
    """Authentication material for an inspected page. Empty by default."""

    storage_state: str | None = None
    http_credentials: tuple[str, str] | None = None
    extra_http_headers: dict[str, str] = field(default_factory=dict)

    def is_empty(self) -> bool:
        return not (self.storage_state or self.http_credentials or self.extra_http_headers)

    def context_kwargs(self) -> dict:
        """Map to ``browser.new_context(...)`` kwargs.

        Raises ``ValueError`` if a ``storage_state`` path was given but does not
        exist — failing loudly here beats a silent unauthenticated inspection.
        """
        kw: dict = {}
        if self.storage_state:
            p = Path(self.storage_state).expanduser()
            if not p.is_file():
                raise ValueError(f"storage_state file not found: {p}")
            kw["storage_state"] = str(p)
        if self.http_credentials:
            user, pwd = self.http_credentials
            kw["http_credentials"] = {"username": user, "password": pwd}
        if self.extra_http_headers:
            kw["extra_http_headers"] = dict(self.extra_http_headers)
        return kw

    @classmethod
    def from_cli(cls, *, storage: str | None = None, basic: str | None = None,
                 headers: list[str] | None = None) -> "BrowserAuth":
        """Build from raw CLI flag values.

        - ``storage``: path to a Playwright storageState JSON (``--auth-storage``).
        - ``basic``: ``"user:password"`` (``--auth-basic``).
        - ``headers``: list of ``"Key: Value"`` strings (``--auth-header``, repeatable).

        Raises ``ValueError`` on malformed input so the CLI can report it.
        """
        creds = None
        if basic:
            if ":" not in basic:
                raise ValueError("--auth-basic must be in the form user:password")
            user, pwd = basic.split(":", 1)
            creds = (user, pwd)
        hdrs: dict[str, str] = {}
        for h in headers or []:
            if ":" not in h:
                raise ValueError(f"--auth-header must be 'Key: Value', got: {h!r}")
            k, v = h.split(":", 1)
            key = k.strip()
            if not key:
                raise ValueError(f"--auth-header has an empty key: {h!r}")
            hdrs[key] = v.strip()
        return cls(storage_state=storage, http_credentials=creds, extra_http_headers=hdrs)
