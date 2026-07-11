# Manifests k8s — RASCUNHO, não aplicados

Este diretório contém rascunhos de manifests Kubernetes para servir um LLM
self-hosted (para `headlabs local`) no EKS existente da HeadLabs. **Nada aqui
foi aplicado a nenhum cluster.** São um ponto de partida para quando a
migração de Docker Compose → EKS for decidida.

Ver `docs/local-runtime.md` seção 2 para o contexto completo da migração.

## Arquivos

- `vllm-deployment.draft.yaml` — vLLM (alto throughput, GPU), usando AWS Deep
  Learning Containers. Recomendado para produção real.
- `ollama-deployment.draft.yaml` — Ollama (mais simples de operar), mesma
  imagem usada na validação local via Docker Compose. Bom para transição
  intermediária antes de otimizar para vLLM.

## Antes de aplicar de verdade, confirmar

Estes manifests têm placeholders explícitos (`namespace`, `nodeSelector`,
tags de imagem, `storageClassName`, escolha final de modelo) que **precisam**
ser ajustados contra a configuração real do cluster HeadLabs:

1. **Namespace** — qual namespace usar (`headlabs-local-llm` é só um placeholder).
2. **Node group / Karpenter NodePool** — labels reais usados no cluster para
   nodes com GPU (`g5`/`g6`), não o `nodeSelector` de exemplo.
3. **Modelo + parser de tool-calling** — validar empiricamente (via `curl`
   direto, como fizemos na validação local) que o par modelo+parser escolhido
   de fato emite `tool_calls` no formato estruturado antes de considerar a
   migração completa. Não assumir com base em documentação do modelo.
4. **StorageClass** — usar a classe de storage já padronizada no cluster,
   compatível com os nodes de GPU escolhidos.
5. **Exposição** — decidir se o endpoint fica só interno ao cluster
   (`ClusterIP`, acessado via `kubectl port-forward` ou de dentro do cluster)
   ou precisa de `Ingress`/`LoadBalancer` para acesso externo — e, se externo,
   como fica autenticação/rede (hoje o `headlabs local` não implementa nenhum
   mecanismo de auth além de um `api_key` opcional no header `Authorization`).
