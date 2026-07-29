# powerbench-faisal — RL for PowerPoint generation

GRPO training of **Qwen3.6-27B** to write `python-pptx` code that builds PowerPoint slides,
evaluated on **SlidesBench** (from [AutoPresent](https://github.com/para-lost/AutoPresent)),
with a deterministic geometric reward.

Run: 30 GRPO steps, 22h10m on 8×A100-80GB, verl 0.8.0 + vLLM, FSDP2 with CPU offload.

---

## Result, stated honestly

**The RL pipeline works. The reward it optimised does not measure slide quality.**

| | step 0 | step 30 |
|---|---|---|
| held-out reward (as-trained grader) | 0.5591 | **0.8926** |
| held-out reward (corrected grader) | 0.4199 | **0.8184** |
| valid decks (code runs, deck produced) | 73% | **96%** |

n = 45 unseen prompts, greedy decode, paired across steps.

**What is real.** The validity gain is not gameable — either the generated program runs and
produces a `.pptx` or it does not. That is a third of the total improvement, and code
correctness and colour-contrast gains hold up under inspection.

**What is not.** Measured on the same 32 held-out tasks before and after training, the model
cut text per slide **−41.5%**, shapes per slide **−51.5%**, and font size **−53.9%** while its
score rose. On one task it scored **0.578 → 0.934 while replacing real quiz answers with
"a) Option A / b) Option B / c) Option C."** Nothing in the reward noticed that the content was
destroyed, because every metric was a *defect* detector and a near-empty slide has no defects.

**External anchor.** On the same 45 prompts and the same corrected grader: Claude Fable-5 scores
0.7924 single-turn and 0.8248 with an agentic harness (render → self-correct), against our
0.8184. That is a statistical tie (SE ≈ 0.04), not a win — and our model was trained against
this grader while Fable never saw it.

**The finding that matters most.** A blind audit (45 decks rendered to PNG, anonymised,
shuffled, graded by eye with grades committed *before* unblinding) found the v1 grader
**anti-correlated** with designer judgement: Spearman **−0.139** overall, **−0.291** within
human-authored decks. It also scored 100 human-authored decks at 0.6391 — below our model.
A grader that ranks an RL-tuned 27B above real human work is not measuring quality.

Crucially that damage is **scoped**: GRPO only ranks same-prompt rollouts against each other,
and within-group ordering held at **+0.63**. The training signal was usable; the *evaluation*
metric was invalid.

---

## Grader v2

Five fixes, each measured against the blind grades:

| check | v1 | v2 |
|---|---|---|
| rank correlation vs designer | **−0.139** | **+0.255** (p = 0.043) |
| human-vs-model gap | −0.1094 | **−0.0154** |
| within-group ordering (what GRPO consumes) | +0.417 | **+0.629** |
| our decks above the human median | 67.4% | 55.6% |
| determinism / integrity | FAIL | **PASS** |

1. **Render-based scoring** (`render_metrics.py`). Geometry reads the shape *box*; a viewer sees
   the rendered *ink*. A deck whose third bullet is visibly cut off at the slide edge scored
   `collision 1.000, overflow 1.000, textfit 1.000`, total 0.9204 — its text box was legally
   inside the canvas. Only pixels see that. Costs 0.34 s/deck batched ≈ **0.8% of a step**; the
   "rendering is too slow for RL" assumption was never measured and was false.
2. **Content term** — the strongest single signal found (ρ = +0.544 vs ρ = −0.139 for the whole
   v1 grader). Band-limited on both sides so neither an empty slide nor a wall of padding scores.
3. **Type-size floor** — shrinking text was the cheapest way to make `textfit` perfect.
4. **Calibration gate** (`calibration.py`) — human decks must outrank model output, and rank
   correlation against blind grades must be positive. This test would have caught the failure on
   day one. A skipped check reports FAIL, never PASS.
5. **Saturation fix** — seven of nine v1 metrics returned exactly 1.000 on any slide without
   visible defects, so the reward was blind precisely where the policy lived.

**v2 is better but does not yet pass its own gate** (needs ρ ≥ +0.40 and ≤50% separation).
The remaining ceiling is that the content term measures rendered ink, which conflates *has
visuals* with *has substance*. Closing it likely needs a semantic term — which is also what the
literature says: no published slide grader relies on render-free geometry alone.

---

## Layout

```
reward/              the grader, tests, and baselines -- the core work
  geometric_reward.py        WEIGHTS, score_deck, soft-min + emptiness gates
  geom_shapes.py             geometric metrics (collision/overflow/textfit/imbalance/...)
  render_metrics.py          v2 pixel metrics: content, clipping (validated measurement lab)
  text_metrics.py            real font metrics via PIL getlength()
  calibration.py             the human-deck calibration gate
  grader_tests.py            invariant suite -- run before spending GPU-hours
  test_layering.py           layering regressions (cards, edge bleed, real defects)
  make_fixtures.py           adversarial fixtures: good / padded / empty
  frontier_baseline.py       frontier model, single-turn, same prompts + grader
  frontier_agentic.py        frontier model through the agentic harness
  singleturn_geometric_reward.py   verl entry point
  verl_checkpoint_fix.md     the one-line FSDP2 CPU-offload fix (verl #5995)
slides/              ~780 generated decks across runs
audit_fixtures/      decks pinned from hand-audits, each one a past grader bug
blind_study/         earlier blind-comparison sheets and findings
*.html               slide galleries (by run, before/after)
slide-RL-experiment.pdf   full write-up: benchmark, prompt, reward, curve, comparison
```

Not in this repo, by design: `AutoPresent/` (third-party — clone it separately), the blind
answer key, and the live grading instrument.

## Running it

```bash
python reward/grader_tests.py <deck_dir>      # invariants; exit 0 = safe to train
python reward/test_layering.py                # layering regressions
python reward/calibration.py                  # human-deck calibration gate
python reward/frontier_baseline.py --model <id> --n 45
```

Needs `python-pptx`, `Pillow`, `numpy`, `opencv-python`, and LibreOffice + `pdftoppm` for the
render-based metrics.

## Reproducibility notes

- Every held-out evaluation is saved per step as `rollouts_v3/val/{0..30}.jsonl` (raw model
  output plus scores) and can be re-scored under any grader version. Both curves above come
  from re-scoring the *identical* saved generations.
- The grader was corrected mid-project, so the run trained against one version and is reported
  against another. Both are shown rather than silently mixed.
- `aspect` is constant at 1.0 once learned and is kept only as a regression guard. A constant
  metric contributes zero GRPO gradient — the invariant suite warns about it deliberately.
