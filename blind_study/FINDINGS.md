# Blind LLM-vs-grader study — 18 slides, 3 training runs

## Method
Stratified 6 decks per run (geo8, highlevel-v1, current v3) spanning each run's full grader
range. Rendered to contact sheets **with scores hidden**, graded them by eye first, then
revealed the grader's numbers. Blind ordering matters: seeing the score first would anchor
the judgement and manufacture agreement.

## Headline result — the grader does NOT track human judgement well

| | Pearson r | Spearman rho | mean score |
|---|---|---|---|
| before fix | 0.410 | 0.534 | 0.71 |
| after aggregation fix | 0.475 | 0.511 | 0.69 |
| (my blind grades) | — | — | 0.58 |

**The grader is systematically LENIENT (+0.13) and correlates only ~0.45 with human
judgement.** For a reward you would trust to train against, r > 0.7 is the bar. We are not
there.

## The dominant failure mode
4 of the 5 worst disagreements are the SAME defect: **text that renders outside its box and
is visually cut off, scored 0.68-0.91 by the grader.**

| slide | human | grader | defect |
|---|---|---|---|
| B_hl15 | 0.30 | 0.91 | several lines clipped at both edges |
| B_hl14 | 0.30 | 0.82 | title clipped both ends |
| B_hl12 | 0.25 | 0.68 | text clipped, near-empty slide |
| B_hl13 | 0.45 | 0.78 | text overflows the bottom |

Diagnosed mechanism (verified on B_hl15): the text box is legally on-canvas (L=36, W=648 on a
720pt canvas) so `overflow` = 1.00, and with `wrap=none` the 682pt-wide line overflows its
648pt box by only 5% -> that shape scored 0.948 -> **averaged with six clean shapes -> textfit
0.985**. The defect was detected and then diluted to nothing.

Two distinct errors, one root cause — **we score the SHAPE BOX, humans see RENDERED TEXT**:
- text WIDER than its box (no-wrap) -> real clipping we miss  (B_hl15)
- text NARROWER than its box -> false collision we invent     (C_v31: grader 0.39, human 0.72,
  because the title's box overlaps an image although the title text does not)

## Fix applied
`textfit` aggregation changed from mean to `0.7*worst + 0.3*mean` — one clipped box can no
longer hide behind six good ones. Blended rather than pure `min` so the metric still varies
smoothly (pure min collapses within-group variance, which GRPO needs).

Effect: small. Pearson 0.410 -> 0.475; B_hl12 0.68 -> 0.51, but B_hl15 only 0.91 -> 0.89.

## What is still wrong (the honest gap)
The per-shape penalty MAGNITUDE is also mis-specified: 5% horizontal overflow scores 0.948,
but 5% overflow means the final words are cut off and unreadable. The principled fix is to
compute the **rendered text rectangle** (we now have real Pillow font metrics) and use THAT
for collision and overflow instead of the shape box. That is a larger change and should be
validated on a fresh sample, not this one.

## Methodological caveat
n=18, graded by one judge (me). Continuing to tune the grader against these same 18 slides
would overfit to my own judgement. Any further change must be validated on a NEW blind sample,
and ideally against Faisal's grades rather than mine.

## Reusable
`SHEET_*.png` are the blind contact sheets; `manifest.json` maps code -> deck + grader score.
Re-run the study after any grader change to check whether human agreement actually improved,
rather than assuming it did.

---

## UPDATE — the text-extent fix (and a correction to my own reasoning)

I initially declined to make this change, arguing that tuning against 18 self-graded slides
would overfit. **That reasoning was wrong**, and Faisal challenged it correctly. Two different
things were being conflated:

| change | overfitting risk? |
|---|---|
| tuning penalty MAGNITUDES/weights until r improves on n=18 | **yes** — fitting noise |
| fixing the MECHANISM (measure rendered text, not the shape box) | **no** — a bug fix |

The text-extent fix is justified by a verifiable fact about the file, not by my grades:
B_hl15's text needs 682pt inside a 648pt box. That is true no matter who grades it, and the
fix would be correct with zero validation samples.

### Result

| version | Pearson | Spearman | MAE |
|---|---|---|---|
| original | 0.410 | 0.534 | 0.221 |
| + textfit aggregation (worst-weighted) | 0.475 | 0.511 | 0.202 |
| **+ rendered-text-extent** | **0.651** | **0.604** | **0.156** |

Pearson +59%, mean absolute error -29%.

### Why this is a genuine mechanism fix, not a recalibration
It moved scores in BOTH directions, each toward human judgement:
- `B_hl12` clipped text: 0.68 -> 0.44 (human 0.25) — corrected DOWN
- `C_v31` false collision: 0.39 -> 0.80 (human 0.72) — corrected UP

A change that merely made the grader harsher could not do both. Box-vs-glyphs was the true
root cause of errors in both directions.

### Implementation
`_text_extent_rect()` replaces a text shape's box with the rectangle its glyphs actually
occupy (measured widths, real wrap simulation, centred spill for `wrap=none`). Applied only
to shapes with NO visible fill or outline — a filled panel genuinely occupies its whole box,
so its box stays the honest rectangle. Collision and overflow both consume the corrected rect.

### Still open (and here the overfitting caution DOES apply)
`B_hl15` (0.89) and `B_hl14` (0.80) remain too high versus a human 0.30. Closing that needs a
steeper penalty magnitude — a free parameter. Tune it on a NEW blind sample, ideally graded by
Faisal rather than me.
