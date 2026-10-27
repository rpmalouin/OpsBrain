# Reasoning LLM — Ollama + a per-model `think` gate

Ops Brain uses a local LLM for *node-level* reasoning (warnings, recommendations,
confidence). Cluster (federation) reasoning is deterministic — no LLM.

## Install & pull the model

1. Install Ollama: `curl -fsSL https://ollama.com/install.sh | sh`
2. Pull the reasoning model and create the 128k tag:
   ```bash
   ollama pull org/qwen2.5-1m:7b          # 1M-native base; needed for a 131072 window
   # FROM org/qwen2.5-1m:7b + PARAMETER num_ctx 131072 + temperature 0.2 + top_p 0.95
   ollama create qwen2.5-7b-128k -f /appdata/ollama/qwen2.5-7b-128k.Modelfile
   ```
3. Confirm it serves: `curl -s http://localhost:11434/api/tags` lists the model, and
   `curl -s http://localhost:11434/api/show -d '{"model":"qwen2.5-7b-128k:latest"}'` reports
   `"capabilities": ["completion", "tools"]` — **no `thinking`**, which is why the reasoner sends
   no `think` field for this tag. (`ollama.model` in `config/ops_brain.yaml` is the only switch;
   the current tag is `qwen2.5-7b-128k:latest`.)

## Model selection — only what the config names

`reasoner.resolve_model()` → `pick_model()` reads `/api/tags` and takes the first
installed model from `ollama.model` → `ollama.fallback_model` → `ollama.alt_models`
(tags compared with a trailing `:latest` ignored). If none of them is installed the stage
exits **rc=3** and logs every model that *is* installed.

There is deliberately **no "pick any model of family X" fallback**. That fallback once
handed the pipeline an unrelated 7B after the primary model was removed, which is how the
whole reasoner stage died silently for hours while actions replayed an old decision.

## CRITICAL — `think` is per model (do not "fix")

A **thinking** model (`qwen3:*`, `deepseek-r1:*`) via `/api/generate` **short-circuits to
a bare `{}` (2 tokens)** for reasonably large prompts unless you send an explicit
`"think": true` — *and* you **omit** `"format": "json"` (`format:json` alongside `think`
makes them emit `{}`).

A **non-thinking** model (e.g. `qwen2.5:*`) is the exact opposite: sending `think` returns
**HTTP 400 `"… does not support thinking"`**. So the field cannot be a global switch:

- `reasoner.fetch_capabilities()` reads `/api/show` → `capabilities` (thinking models list
  `"thinking"`).
- `reasoner.decide_think()` sends `think` only when the model declares it. Capability
  unknown (older Ollama) ⇒ keep sending it, and a 400 whose body mentions thinking is
  retried **once** without `think`.
- `format:json` is never sent. `reasoner/prompt.txt` mandates JSON, and
  `reasoner.extract_json()` recovers the object from fenced, prose-wrapped, or repeated
  model output (balanced-brace candidates, preferring the latest one that carries a
  decision key).

## Config

```yaml
ollama:
  url: http://localhost:11434/api/generate
  model: qwen2.5-7b-128k:latest   # the host's only LLM; non-thinking
  timeout: 120
  think: true            # ALLOW flag only — sent only to models that support it (see above)
  options:
    temperature: 0.2
    num_ctx: 131072    # matches every other consumer on the host — see "Why 131072" below
    keep_alive: 5m
```

`fallback_model` / `alt_models` are deliberately absent: with one installed LLM a fallback can
only name a model that does not exist.

Ollama must be reachable at the configured `url`. `keep_alive: 5m` means the model unloads
between hourly cycles, so each cycle pays a reload (and VRAM stays free in between).
`num_ctx` is 131072; the reasoner feeds the model a **compact risk digest**
(`summarize_collector`) containing anomalies only, not the full container fleet.

### Why 131072 (and why 32768 used to be the ceiling)

Sizing is about VRAM, not the prompt, and the host's VRAM changed. On the single 16 GB card the
7B at `num_ctx: 65536` could not be held: ollama does *not* error, it caps the window at 40960,
spills ~8% of the layers to CPU and throughput collapses 74 → 4 tok/s; the 120 s
`ollama.timeout` then fired every cycle and the reasoner stage exited **rc=4** (actions keep
running with the LLM half suppressed, so the pipeline still looks alive). The config was cut to
32768 to recover.

The host now has two cards (5070 Ti 16 GB + 3060 12 GB, 27.1 GiB pool). At `num_ctx: 131072` the
7B measures **8.8 GB, `100% GPU`, 153 tok/s**, so the old cap no longer describes a limit. The
remaining reason to pin 131072 is consistency: with `OLLAMA_NUM_PARALLEL=1` the runner loads at
whatever `num_ctx` the LAST caller asked for, and every other consumer here (paperless-ngx,
hindsight, paperless-gpt, open-webui) calls at 131072 — a mismatch makes ollama unload and reload
the model once for this hourly call and again for the next consumer.

The digest is ~2.5k tokens, so 131072 is pure headroom. Verify any change with
`docker exec ollama ollama ps`: PROCESSOR must read `100% GPU`, and the CONTEXT column reports the
EFFECTIVE window (which is also why a tag named `-64k` can be running at 40960). The KV-shrinking
container flags (`OLLAMA_FLASH_ATTENTION=1`, `OLLAMA_KV_CACHE_TYPE=q8_0`) remove the spill but do
**not** raise a hardware cap.

The model often keys actions as `"action"` rather than `"type"`; `sanitize()` accepts both
and drops any verb outside the allow-list.

## The decision contract: the detector decides, the model relays

`reasoner/prompt.txt` states two things the model must not decide for itself, because it has no
baseline and no thresholds:

- **`confidence` is the model's own certainty, 0.0-1.0.** The template used to show the field as
  a literal `"confidence": 0.0` with no definition, and the 7B copied that literal into every
  answer — every cycle then sat below `actions.qwen_confidence_floor` and the LLM half was inert
  while `reasoner.fresh` still read true (measured: 1-3 below-floor cycles/day before the
  `qwen2.5-7b-128k` swap, then 24/24). A template value is an instruction: label it an example and
  say what the field means.
- **`gpu_drift` must be a copy of the state's `drift_flags`.** The flags come from the collector's
  deterministic detector; a model that re-derives them invents the thresholds (*"8.93 W exceeds
  the idle max of 8.5 W"* against a configured 40 W) and triggers critical alerts. Since
  2026-10-27 the actions layer corroborates every flag on `drift_flags` anyway
  (docs/remediation.md), so an invented one is dropped — the prompt rule keeps the model's prose
  consistent with what actually fired.

## Freshness guard (a stale decision is never replayed)

`logs/reasoner_result.json` records the collector snapshot `timestamp` the decision was
made for, plus `model` and `thinking`. `hermes_actions.reasoner_decision_state()` compares
that with the current collector snapshot: on a mismatch the **LLM half of the cycle is
suppressed** (deterministic rules still run), the reason is logged, `reasoner: {fresh:
false, reason}` is written to `logs/actions_result.json`, and a `reasoner_stale`
notification fires at most once per UTC day.

A failed reasoner stage is therefore visible in three places: the stage's rc in
`logs/opsbrain.log`, `reasoner.fresh` in `actions_result.json`, and the notification queue.

## Below-floor capture (why a 0.00 is now readable)

A decision below `actions.qwen_confidence_floor` suppresses the LLM half of the cycle, so nothing
downstream can show *why* it was low. `reasoner.capture_low_confidence()` therefore appends one
record per such cycle to **`logs/reasoner_low_conf.jsonl`** — `ts`, `snapshot`, `model`, `thinking`,
`confidence`, `floor`, `eval_count`, `warnings`/`actions` counts, `summary`, `raw_chars` and the
model's `raw` text (capped at 4000 chars) — and logs
`confidence X < floor Y — raw model text kept in logs/reasoner_low_conf.jsonl`. The parse-failure
path is captured too (it also lands at 0.0), and the capture never changes a decision or notifies.

```bash
tail -1 logs/reasoner_low_conf.jsonl | python3 -m json.tool | head -20
```

Diagnostic only, and deliberately not config-gated: an empty file means every cycle cleared the
floor. Measured 2026-10-10: the answer is sporadically degenerate (0.00 with an "all healthy"
summary on an input that yields 0.75-0.85 in fresh runs; 7.2 % of 6,243 cycles below the floor),
so the raw text is the only way to tell "the model said nothing useful" from "we could not read it".

## Verifying a model change end-to-end

```bash
curl -s localhost:11434/api/tags                       # what is installed
python3 reasoner/reasoner.py --raw                     # one real call, writes the decision
python3 -c "import json;print(json.load(open('logs/reasoner_result.json'))['model'])"
systemctl restart opsbrain.service                     # daemon runs a full cycle immediately
grep -E "reasoner|cycle complete" logs/opsbrain.log | tail -5
```
