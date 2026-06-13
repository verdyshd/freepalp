<p align="center">
  <img src="freepalp/web/static/freepalp-logo.svg" alt="FreePalp" width="520">
</p>

<p align="center">
  <b>Multi-agent AI orchestrator that runs on free models.</b><br>
  Self-improving · persistent memory · MCP · token streaming · WebUI
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="MIT"></a>
  <img src="https://img.shields.io/badge/python-3.10%2B-blue.svg" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/providers-10%2B-green.svg" alt="10+ providers">
  <a href="README.ru.md"><img src="https://img.shields.io/badge/lang-Русский-red.svg" alt="Russian"></a>
</p>

<p align="center">
  <b>English</b> · <a href="README.ru.md">Русский</a> · <a href="README.zh.md">中文</a>
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
stronger one succeeds, the working procedure is distilled into a reusable
`SKILL.md` (Claude-Code-compatible) and injected into the prompt next time the
same kind of task shows up — so the cheap model gets it right on the first try.
Free students, paid teachers, growing skill.

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
git clone https://github.com/verdyshd/freepalp
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
- **Local-first** via Ollama — unlimited fallback, fully offline-capable, and
  auto-started on launch if you used it before.
- **DAG decomposition + parallel subagents** — complex multi-file tasks are
  split by an Architect into a dependency graph; independent steps run in
  parallel, each a focused worker that sees what previous steps produced (with
  provider fallback and deterministic "you promised a file — create it" checks).
- **Teacher→skill distillation** — successful retries become reusable
  `SKILL.md` procedures that make the cheap model stronger over time.
- **MCP client** — connect any [Model Context Protocol](https://modelcontextprotocol.io)
  server in `config/mcp_servers.json` and its tools appear to the agent
  automatically (filesystem, GitHub, databases, hundreds of ready servers).
- **OpenAI-compatible API** — point any IDE plugin (Continue.dev, etc.) or
  OpenAI client at `http://localhost:28800/v1` and the whole orchestrator
  (routing, tools, DAG, memory) runs under your editor.
- **Token streaming** — the final answer types out token-by-token in the WebUI.
- **Deep research** — one tool does a multi-angle web search, fetches the top
  pages, and the agent writes a report citing **real** sources (no hallucinated
  links — a deterministic trigger forces the live search).
- **Artifacts preview** — HTML the agent creates (games, pages) opens and plays
  right inside the chat, in a sandboxed iframe.
- **Persistent memory** — HOT / WARM / COLD tiers + a vector index you can
  explore as a real graph, plus **FTS5 search over your whole session history**
  ("when did we discuss X?").
- **Self-improvement** — proposes prompt versions, gated by a held-out metric,
  auto-rollback on regression.
- **Reliability over LLM trust** — deterministic detectors catch hallucinated
  file writes, identity slips, blind rewrites, leaked tool calls, and stub
  content before they reach you.
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

## Support

FreePalp is free and MIT-licensed. If it's useful to you, support keeps it going:

- 💖 **GitHub Sponsors:** https://github.com/sponsors/verdyshd
- 🪙 **USDT (TRC-20):** `TFAfqkvNDpJPeKWqm4QiFiU5fa7RC97GZx`

GitHub Sponsors takes cards from anyone, anywhere; crypto works worldwide too.

## License

MIT — see [LICENSE](LICENSE).
