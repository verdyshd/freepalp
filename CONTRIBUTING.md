# Contributing to FreePalp

Thanks for your interest! FreePalp is a multi-agent orchestrator that runs on
free and local LLMs. Contributions of all sizes are welcome — bug reports,
docs, new tools, provider integrations, or evals.

## Quick dev setup

```bash
git clone https://github.com/verdyshd/freepalp
cd freepalp
pip install -e .            # installs the `freepalp` command
cp .env.example .env        # optional — works on local Ollama with no keys
```

Run the WebUI with `freepalp` (or `python -m freepalp.app --web`).

## Before opening a PR — run the checks

```bash
python test_mvp.py        # deterministic gate: parser, router, tools, memory
python test_security.py   # security suite (sandbox, whitelist, injections, sanitization)
```

Both must pass (they also run in CI on Ubuntu/Windows × Python 3.11/3.12). For
changes to agent quality, you can also run the eval harness:

```bash
python eval_harness.py    # code-execution-accuracy on val/hold-out splits
```

## Project layout (where things live)

- `freepalp/core/` — orchestrator, router, model discovery, task parser,
  self-improvement, context checkpoints.
- `freepalp/agents/` — worker (ReAct loop), critic (two-tier), architect (DAG),
  tool agent (the single tool-execution chokepoint, incl. pre-exec validation).
- `freepalp/tools/` — the 47 built-in tools (files, shell, web, browser, GitHub,
  notifications, system). Add a new tool here and register it in `tool_agent.py`.
- `freepalp/memory/` — HOT/WARM/COLD tiers, vector store, consolidation, FTS5 history.
- `freepalp/web/static/` — the WebUI (single `index.html`).
- `freepalp/config/` — prompts, model fallbacks, MCP servers, version snapshots.

## Conventions

- **Match the surrounding code** — comment density, naming, idioms.
- **Determinism over LLM trust**: prefer deterministic checks/detectors where a
  result can be verified in code (this is a core design value — see THREAT_MODEL.md).
- **Windows-friendly**: `encoding="utf-8"` on file ops; use `core/winproc.no_window()`
  for any `subprocess` so no console window flashes.
- **No fabricated numbers** in docs — only real, reproducible figures.

## Good areas to contribute

- New **provider integrations** (OpenAI-compatible endpoints in `model_discovery.py`).
- New **tools** for the worker (a `run_tests`/lint tool, language servers, etc.).
- **Eval tasks** for `eval_harness.py` (expand the val/hold-out sets).
- **Docs / translations** (README is EN/RU/ZH).

## Reporting bugs

Open an issue with: what you ran, what happened, what you expected, and the
provider/model in use (`/providers` in the CLI or the Providers tab shows status).

By contributing you agree your work is licensed under the project's
[MIT License](LICENSE).
