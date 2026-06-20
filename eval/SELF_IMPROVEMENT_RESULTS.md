# Self-improvement — measured baseline-vs-tuned Δ

A reproducible demonstration that FreePalp's self-improvement loop produces a **measured,
gated improvement** on a held-out coding benchmark — not a hand-tuned number.

## The loop (what happened, end to end)

1. **Baseline measured** on 32 hard coding tasks (`eval/hard_tasks.json`), config frozen
   (`FREEPALP_NO_AUTOIMPROVE=1`): **27/32 (84.4%)**, val 11/15, holdout 16/17.
2. **Evaluator found the real failure mode.** 3 tasks failed because the worker showed code
   as text instead of calling `write_file` (`code_not_saved`). Earlier the evaluator ranked
   problems by *type-average* and missed this — these failures hid inside an otherwise high
   coding average. Failure-mode targeting (group failures by canonical cause) surfaces it.
3. **Improver generated a fix** — strengthened the `coding_small` worker prompt: *"if asked
   to create/save a file, you must emit a `write_file` tool_call, not just text code."*
4. **Gate accepted it.** `test_mvp` passed; held-out validation showed no regression. (A key
   bug was fixed here: the held-out judge previously scored single-shot prose and penalised a
   correct `tool_call` answer as "not an answer" — it now measures the same thing the real
   eval does.) Version `v1.0.27` activated.
5. **Re-measured** on the same 32 tasks at `v1.0.27`.

## Result

| Split | Baseline (v1.0.26) | Tuned (v1.0.27) |
|---|---|---|
| Overall | 27/32 = **84.4%** | 29/32 = **90.6%** |
| val | 11/15 = 73.3% | 13/15 = **86.7%** |
| holdout (frozen) | 16/17 = 94% | 16/17 = 94% |

**Flipped:** `spiral_order`, `kmp_search`, `longest_palindrome` ❌→✅ (the targeted
`code_not_saved` mode — files are now written); `fraction_to_decimal` ✅→❌ (one regression).
Net +2 tasks. Holdout unchanged — no overfitting to the validation set.

## Honest caveats

- **Single run, no averaging.** Free-tier models have run-to-run variance, so part of the
  flip set is noise (`longest_palindrome` passed here but failed in a spot-check;
  `fraction_to_decimal` regressed). The **clean signal** is the targeted failure mode: all
  three "code shown as text" failures now produce a file — a direct consequence of the fix,
  least subject to variance.
- For statistically significant numbers, average with `--runs N` and a larger task set
  (100+ val). This is a seed-scale demonstration of the mechanism, not a final benchmark.
- The point is not "+X% guaranteed" — it is **safe, gated self-modification**: the system
  found its own failure mode, fixed it, and a regression gate guarded the change.

## Reproduce

```bash
# baseline (freeze config), then run the self-improvement cycle, then re-measure
python _qa/baseline_hard.py 32          # measures the active version on the hard set
python _qa/run_selfimprove.py           # one self-improvement cycle (analyse → fix → gate)
python _qa/baseline_hard.py 32          # re-measure the new active version
```
(The `_qa/` helpers are session tooling, not part of the package.)
