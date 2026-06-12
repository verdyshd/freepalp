<p align="center">
  <img src="freepalp/web/static/freepalp-logo.svg" alt="FreePalp" width="520">
</p>

<p align="center">
  <b>Multi-agent AI orchestrator that runs on free models.</b><br>
  Self-improving · persistent memory · autonomous tool use · WebUI
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="MIT"></a>
  <img src="https://img.shields.io/badge/python-3.10%2B-blue.svg" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/providers-10%2B-green.svg" alt="10+ providers">
  <a href="README.ru.md"><img src="https://img.shields.io/badge/lang-Русский-red.svg" alt="Russian"></a>
</p>

---

## What is it

FreePalp is a multi-agent orchestrator that gets real work done using **free and
local LLMs**. A router picks the best available model per task across 10+
providers (Groq, OpenRouter, Cerebras, Gemini, Mistral, Together, the
[models.dev](https://models.dev) catalog, and local **Ollama**), a worker agent
executes via a ReAct tool loop, and a two-tier critic verifies the result —
cheap deterministic checks first, an LLM critic only when needed.

The differentiator: **corrections accumulate**. When a cheap model fails and a
stronger one succeeds, the working procedure is distilled into memory so the
cheap model handles it next time. Free students, paid teachers, growing skill.

```
User input
    ↓
Task Parser   → classifies the task (coding / research / chat / ...)
    ↓
Router        → live discovery across 10+ providers, picks best available model
    ↓
[Architect]   → plans complex tasks (DAG)
    ↓
Worker        → executes via LLM + ReAct loop (calls tools itself)
    ↓
Critic        → tier 1: deterministic checks · tier 2: LLM score (0–1)
    ↓ retry if it actually failed (max 3)
Result
```

## Quick start

```bash
git clone https://github.com/dmitrychaiko/freepalp
cd freepalp
pip install -e .          # installs the `freepalp` command
freepalp                  # launches the WebUI at http://localhost:28800
```

First run with **no keys** still works through local Ollama. To add a free cloud
model, drop a key into `.env` (a free Groq key gives 500k tokens/day):

```bash
cp .env.example .env
# put GROQ_API_KEY=gsk_... in .env  — get one at https://console.groq.com/keys
```

Any OpenAI-compatible provider from the models.dev catalog activates just by
adding its key to `.env` (e.g. `DEEPSEEK_API_KEY`, `XAI_API_KEY`).

## Features

- **10+ providers, 50+ models** with automatic routing by task type and live
  quota/cooldown awareness.
- **Local-first** via Ollama — unlimited fallback, fully offline-capable.
- **Persistent memory** — HOT / WARM / COLD tiers + a vector index you can
  explore as a real graph in the UI.
- **Self-improvement** — proposes prompt versions, gated by a held-out metric,
  auto-rollback on regression.
- **Reliability over LLM trust** — deterministic detectors catch hallucinated
  file writes, identity slips, blind rewrites, and leaked tool calls before they
  reach you.
- **WebUI** — chat, live token/quota meters, memory graph, metrics, settings.
- **Stop button** — interrupt the agent mid-task (UI and Ctrl+C in CLI).

## Security

FreePalp uses tools, shell, and self-modifies its own source, so security is
taken seriously: sandboxed file access, a shell whitelist that blocks
metacharacter injection, and a deterministic test suite (`test_security.py`,
29 cases). See [THREAT_MODEL.md](THREAT_MODEL.md) for the honest picture,
including residual risks.

## Development

```bash
python test_mvp.py        # deterministic gate (parser, router, tools, memory)
python test_security.py   # security suite
```

CI runs both on Ubuntu and Windows (Python 3.11 / 3.12).

## License

MIT — see [LICENSE](LICENSE).
