# browser-devtools MCP

Headless Chromium inspection for **running** front-ends, as a HeadLabs
`framework=container` MCP. Gives Claude-backed platform agents (e.g. the
`loop-inspector`) real browser capability — load a URL and see console errors,
uncaught JS exceptions, failed network requests, rendered DOM/text, screenshots,
and evaluate JS — instead of the plain `http_get` the inspector uses today
(which can't see a JS-rendered SPA).

## Tools

- `inspect_page(url, wait_ms=2000, screenshot=False)` → `{http_status, title,
  console_errors[], console_warnings[], page_errors[], failed_requests[],
  request_count, rendered_text_excerpt, screenshot_base64?}`
- `evaluate_js(url, script, wait_ms=2000)` → `{result}` (a11y/DOM probes, e.g.
  count `<img>` without alt)

Both are read-only and **stateless** — one fresh browser per call, closed in
`finally` (matches `stateless_http=True` and the inspector's fragmented per-unit
model; mirrors the platform render-service recipe).

## Design

- Async Playwright (`playwright.async_api`) with `async def` tools — FastMCP's
  streamable-HTTP runtime is asyncio, so no thread worker / event-loop conflict.
- Chromium launched with `--no-sandbox --disable-dev-shm-usage --single-process
  --no-zygote` (same args as the platform render-service).
- Playwright imported lazily inside the tools so the module boots for
  `tools/list` verification without a Chromium binary.
- Docker base `mcr.microsoft.com/playwright/python:v1.48.0-jammy` (arm64) —
  Chromium + system libs preinstalled.

## Deploy

```bash
headlabs mcps push browser-devtools --profile <ecr-profile> --wait
```

Registers the MCP, uploads source, builds the arm64 image, pushes to ECR, and
creates the AgentCore container runtime. Served (private, tenant-scoped) at
`https://mcps.headlabs.ai/browser-devtools/mcp`.

## Attach to an agent

Add to the agent's `manifest.mcp`:

```json
{ "manifest": { "mcp": [{ "server": "browser-devtools" }] } }
```

For the platform inspector, add the same entry to
`agents/loop-inspector/config.yaml` `tools.mcp` (currently `[]`) and redeploy —
this upgrades `labs inspect` frontend reviews from `http_get` to a real browser.

## Verified

- Local: captured an uncaught `TypeError` (pageerror), a 404 console error +
  failed request, and img-without-alt on a demo page.
- Deployed (AgentCore): `evaluate_js('navigator.userAgent')` →
  `...HeadlessChrome/130 ... Linux aarch64` and `inspect_page` returned the
  rendered title/status/requests — real Chromium running in the container.
