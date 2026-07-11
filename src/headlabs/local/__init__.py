"""headlabs local — standalone agent runtime.

A self-contained coding-agent loop (provider + tools + permission + engine)
that talks to a self-hosted, OpenAI-compatible LLM endpoint (vLLM, Ollama,
LM Studio, TGI, ...) instead of the HeadLabs platform or Bedrock.

Independent of `run` / `chat` / `agents` / `run --local` — those keep talking
to the HeadLabs platform (or a Dockerized platform agent) unchanged.
"""
