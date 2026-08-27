# Reasoning LLM — Ollama + Qwen3

Ops Brain uses a local LLM for *node-level* reasoning (recommendations + confidence).
Cluster (federation) reasoning is deterministic.

## Install & pull the model

1. Install Ollama: `curl -fsSL https://ollama.com/install.sh | sh`
2. Pull the reasoning model:
   ```bash
   ollama pull qwen3:14b
   ```
   (fallback model `qwen2.5-coder:14b` is used automatically if `qwen3:14b` is absent.)
3. Confirm it serves: `curl -s http://localhost:11434/api/tags` lists the model.

## CRITICAL — Qwen3/Ollama request shape (do not "fix")

`qwen3:14b` via `/api/generate` **short-circuits to a bare `{}` (2 tokens)** for
reasonably large prompts unless **both**:

1. you send an explicit `"think": true` (or false) field — set `ollama.think: true` in
   config, **and**
2. you **omit** `"format": "json"`.

Sending `format:json` alongside `think` makes Qwen3 emit `{}`. The prompt
(`reasoner/prompt.txt`) already mandates JSON, and `reasoner.extract_json()` recovers the
object with a balanced-brace regex. This is handled by the code — just never try to "fix"
the call shape back to `format:json`.

## Config

```yaml
ollama:
  base_url: http://localhost:11434
  model: qwen3:14b
  fallback_model: qwen2.5-coder:14b
  think: true      # REQUIRED — see quirk above
```

Ollama must be reachable at the configured `base_url`. If the model is cold, the first
cycle can be slow (~10–15s). `num_ctx` is 32768; the reasoner feeds Qwen a **compact risk
digest** (`summarize_collector`) that only includes anomalies, not the full container
fleet, to stay within the context window.

The model often keys actions as `"action"` not `"type"`; `sanitize()` accepts both.