# powerbench-faisal — RL for PowerPoint generation

GRPO training of **Qwen3.6-27B** to write `python-pptx` code that builds PowerPoint slides,
evaluated on **SlidesBench** (from [AutoPresent](https://github.com/para-lost/AutoPresent)),
with a deterministic reward.

8×A100-80GB, verl 0.8.0 + vLLM 0.25.1, FSDP2 with CPU offload.

---

## Result

**Training works. The gain is failure-recovery, not broad polish — and we can prove which,
because the headline number comes from an independent judge rather than the training reward.**

Final run (2026-08-01, 30 steps, ~13 min/step):

| measure | baseline | step 30 |
|---|---|---|
| **valid decks** (code runs and produces a `.pptx`) | **80%** | **100%** |
| held-out geometric reward | 0.475 | 0.696 (peak 0.727 @ step 25) |
| blind paired judge quality, already-valid decks | 6.95 / 10 | 7.16 / 10 |

n = 45 unseen prompts, greedy decode, paired across checkpoints.

**The valid-deck rate is the real result.** The baseline failed 9 of 45 tasks outright — the
generated program crashed and produced nothing. Step 30 fails none. That number cannot be
gamed: either the code runs or it doesn't.

**The quality gain is small and uneven.** A blind, task-conditioned VLM judge comparing
baseline against step 30 (shuffled, judges shown the instruction so title slides aren't
wrongly docked) scored +0.20/10 on decks that were already valid: 8 improved, 7 regressed,
21 unchanged. Dramatic recoveries on previously-broken tasks (`environment/slide_3` 1.0 → 7.0,
`business/slide_4` 2.3 → 6.7), a few real regressions on marketing slides.

**Response length fell** from ~5440 to ~1540 tokens while the score rose, which rules out
length-hacking — a failure mode an earlier run did exhibit.

Honest scope: this is a **layout/form** result on a SlidesBench stand-in, validated by an
independent judge. It is not a claim about content quality.

---

## The reward, and why it kept getting hacked

The reward is deterministic and mostly render-free, so it is reproducible and cheap enough to
run inside an RL loop (~0.34 s/deck batched; a 64-rollout load test measured 75.5 s, about 3%
of a step).

It also got gamed three times, and the pattern mattered more than any individual hole:

| reward | how the policy beat it |
|---|---|
| text-coverage | padded every slide with filler |
| + geometry (collision/overflow/textfit) | emitted near-empty slides — no elements, no defects |
| + density / content | emitted **empty charts** — gridlines and chrome read as "ink" |

**Three fixes, three new holes is a wrong-architecture signal, not a bug series**
(`REWARD-HACKABILITY.md`). The root cause: GRPO is an adversarial optimizer pointed at a
**reference-free** reward that scores *form* — geometry, ink, character counts — and never
*content correctness*. `compute_score` receives `ground_truth` and `score_deck` ignored it, so
the reward was structurally blind to the instruction. AutoPresent, by contrast, used SFT
(imitation, ungameable) plus reference-**based** evaluation, and never put a reference-free
signal under RL.

### What is in the reward now

- **Geometry** — collision, overflow, textfit, imbalance, picfit, contrast, alignment, aspect.
  Human-validated: overlap-flagged decks score 0.52 vs 0.71 unflagged; clipped 0.61 vs 0.71.
- **`content`** (render-based) — real ink coverage, masked to *authored* text/picture/chart/table
  boxes so decorative scaffolding cannot farm it. Strongest single signal found (ρ +0.626 vs
  blind designer grades).
- **`chart_ok`** (render-free) — reads chart series data, so an empty chart cannot pass on its
  chrome. Closed the hole a human blind study found.
- **`adherence`** — matches `required_texts` from `ground_truth`, visibility-gated (on-canvas,
  ≥6pt, word-boundary) so hidden or 1pt "ghost text" cannot satisfy it.
- **Gates** — invalid deck scores 0.0 and never negative (a negative floor makes every failed
  rollout in a GRPO group identical, zeroing the gradient); a soft-min gate on the critical
  metrics so one catastrophe cannot be averaged away.

### What is still open

- **Generic-filler farm.** On-topic filler text inside real boxes still satisfies content,
  density, and title-level adherence. Not patchable — needs a reference or content signal.
- **Bland safe-template.** The reward has no opinion about a boring but clean slide.
- **The grader cannot rank our own model's output.** In a blind human study, Spearman within
  our decks was **≈ 0.00**, and the human ranked our decks *last* of four sources while the
  grader ranked them above human-authored decks. Fine for measuring form; disqualifying as a
  standalone quality metric.

---

## Evaluation

The training reward is ~0.00 correlated with human judgment on our own decks, so a rising
held-out curve alone cannot separate learning from hacking. Every headline number above
therefore comes from an **independent** evaluation:

- 45 held-out decks generated from each checkpoint (baseline / 10 / 20 / 30), rendered
- a blind, paired, task-conditioned VLM-judge panel (3 judges, shuffled order, key withheld)
- `eval/progression.html` — slide-by-slide progression viewer, baseline → step 30

Raw judge outputs, per-checkpoint decks, and the unblinding key are in `eval/`.

---

## Layout

```
reward/                    the reward, tests, and baselines
  geometric_reward.py        WEIGHTS, score_deck, gates, chart_ok, adherence
  geom_shapes.py             geometric metrics
  render_metrics.py          pixel metrics: content (box-masked), clipping
  text_metrics.py            real font metrics via PIL getlength()
  calibration.py             human-deck calibration gate
  grader_tests.py            invariant suite -- run before spending GPU-hours
  test_layering.py           layering regressions (cards, edge bleed, real defects)
  gate_test.py               adversarial gate: padded / empty / sparse / empty_chart / off-topic
  make_fixtures.py           adversarial fixtures
  frontier_baseline.py       frontier model, single-turn, same prompts + reward
  frontier_agentic.py        frontier model through the agentic harness
  loadtest_render.py         64-concurrent render load test
  run_next_geo.sh            the training launch used for the final run
  verl_checkpoint_fix.md     one-line FSDP2 CPU-offload fix (verl #5995)
eval/                      independent evaluation: per-checkpoint decks, judges, progression UI
slides/                    ~780 generated decks across runs
audit_fixtures/            decks pinned from hand-audits, each one a past reward bug
REWARD-HACKABILITY.md      why the reward kept getting gamed -- architectural diagnosis
EXPERIMENT-REVIEW.md       whole-experiment review: what is missing and what it would take
TODO.md                    operational log, box setup, run configs
```

Not in this repo, by design: `AutoPresent/` (third-party — clone it separately), the blind
answer key and unblinding analysis, and the live grading instrument. Blind-study item IDs are
redacted from `TODO.md` until every rater has finished.

## Running it

```bash
python reward/grader_tests.py <deck_dir>      # invariants; exit 0 = safe to train
python reward/test_layering.py                # layering regressions
python reward/gate_test.py                    # adversarial gate
python reward/calibration.py                  # human-deck calibration gate
python reward/frontier_baseline.py --model <id> --n 45
```

Needs `python-pptx`, `Pillow`, `numpy`, `opencv-python`, and LibreOffice + `pdftoppm`.

## Reproducibility notes

- Every held-out evaluation is saved per step as `rollouts_v3/val/{0..30}.jsonl` (raw model
  output plus scores) and can be re-scored under any reward version.
- The reward changed during the project. Where two numbers exist for one run, both are shown
  rather than silently mixed.
- `aspect` is constant at 1.0 once learned and is kept only as a regression guard. A constant
  metric contributes zero GRPO gradient, and the invariant suite warns about it deliberately.
- `adherence` is currently near-constant (~1.0) on high-level prompts, where `required_texts`
  averages ~1.4 per task — effectively just the title. It should be **gate-only**, not
  weighted, in the next run.
