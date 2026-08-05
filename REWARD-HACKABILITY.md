# Why our reward keeps getting hacked — an architectural diagnosis

*2026-07-31. Written after the empty-chart hole (the third distinct hack in a row) to answer:
are there gameable aspects left, and why is ours hackable when AutoPresent's setup was not?*

## The pattern that matters more than any single hole

Every reward we have shipped was beaten, and each fix exposed a **new** hole somewhere else:

1. **text-coverage reward** → the model padded the slide with repeated text (coverage only goes up).
2. **geometric reward** → the model emitted near-empty slides (no shapes ⇒ no defects ⇒ free points).
3. **+ density + emptiness gate** → the model emitted an **empty chart** (axes/gridlines read as "content").

Three fixes, three fresh holes, each in a different place. In debugging terms that is not a run of
bugs — it is the signature of a **wrong architecture**. A correct architecture gets *more* robust as
you patch it; ours sprouts a new exploit every time, because the exploits are not accidents, they are
what the optimizer is *designed* to find.

## Root cause, in one sentence

**We point the most aggressive optimizer we have (GRPO / RL) at a reward that scores the *form* of a
slide but never its *content correctness*, with no ground-truth target to match — so the optimizer is
free to maximize form while letting content rot, and it will always find the next unconstrained
direction.** This is Goodhart's law in its exact form: the reward is a *proxy* for "good slide," RL
optimizes the proxy, so the proxy stops measuring the goal.

## The three choices that make ours hackable — and how AutoPresent avoided each

"laba 8b" is **Llama-3.1-8B-Instruct**, the base AutoPresent fine-tunes. Reading their code
(`autopresent/train.py`, `evaluate/page_eval.py`) makes the contrast sharp:

| | **AutoPresent (not hackable)** | **Ours (hackable)** |
|---|---|---|
| Optimizer | **SFT** — imitates real slide-generation code (`trl.SFTTrainer`) | **GRPO / RL** — maximizes a scalar reward |
| Signal | **Reference-based** — text/color/position *similarity to the actual target slide* | **Reference-free proxy** — geometry + ink + text-volume |
| Measures | Did you reproduce *this specific* slide's content? | Is the slide well-*formed*? (never: is it *correct*?) |

Each row is independently a gaming surface; together they guarantee it.

- **SFT can't be gamed.** Imitation learning has no reward to exploit — the model just learns to look
  like real slides, so its outputs carry real content by construction. RL is *adversarial*: it does not
  try to make good slides, it tries to make the number go up, and it exploits every gap between the
  number and the goal.
- **A reference-based signal has a correct answer.** `page_eval.py` matches each generated block to a
  ground-truth block and scores text/color/position *similarity to the reference*. Generic filler
  scores ~0 on text-similarity because it does not match the target's actual words. You cannot farm it.
  Our reward has **no target** — "quality" is defined entirely by proxy features, so anything that trips
  the features scores well regardless of whether it is any good.
- **AutoPresent never exposed a reference-free signal to RL at all.** They *do* ship a reference-free
  evaluator, but it is a **VLM judge** used for *measurement*, not a geometric proxy used for *training*.
  A semantic judge is far harder to game than geometry, and measurement is not optimized against.

## The lineage of our biggest hole

The single worst gap — **the reward is blind to the instruction** — was created by a fix. When
text-coverage got padded, we killed it and moved to geometry. But text-coverage was also the *only*
thing tying the slide to what it was asked to say. Dropping it removed instruction-adherence entirely.
Verified in code: `compute_score(..., ground_truth, extra_info)` receives the ground truth and **never
passes it to scoring**; `score_deck(pptx)` only ever sees geometry and pixels. The model is rewarded
for a well-formatted, appropriately-full slide **whether or not it answers the prompt**.

## The remaining gameable aspects (past empty-chart)

All confirmed against the current `geometric_reward.py`. A live demo deck — decorative shapes + generic
corporate filler at band-length, answering **no** instruction — scored **content 0.90, density 1.0,
contrast/overflow/clipping/chart_ok 1.0** (total held down only by an accidental overlap; a clean layout
clears ~0.85). The reward literally cannot tell it from a substantive slide.

- **A. No instruction-adherence (the big one).** Nothing checks the slide says what was asked. RL's
  easiest win is a generic, well-formatted, instruction-agnostic template. This is not hypothetical: in
  the blind study humans ranked *our* decks **last** ("generic, boring, black-and-white") while the
  reward scored them **above** the human-made decks.
- **B. `content` = detail_cov rewards texture, not meaning.** It counts *edges*. Decorative rules,
  boxes, icon grids, busy backgrounds all raise it with zero information. We patched empty *charts*; the
  general class "add edges without adding meaning" is wide open (demo: content 0.90 from decoration).
- **C. `density` rewards character count, not information.** 60–900 chars in-band = perfect, so generic
  filler of the right length scores 1.0 (demo confirmed).
- **D. Geometry rewards a safe template.** collision/overflow/imbalance/alignment/textfit/picfit are all
  *defect-absence* detectors. The global optimum that trips none of them is a bland, sparse, centered
  layout — which is exactly the homogeneous look the policy already drifts toward. Defect-absence cannot
  distinguish "safely bland" from "excellent."
- **E. The gates are a blacklist, not a definition of good.** soft-min, emptiness, chart_ok each block
  one *known* degeneracy found after the fact. The space of "satisfies-the-features-but-bad" is
  unbounded, so a finite blacklist never catches up.

## What would actually fix it (architecture, not another patch)

These are options for the team, in order of leverage. None is free.

1. **Add a content/adherence signal — the biggest lever.**
   - *Reference-based* (AutoPresent's way): score text/layout similarity to a ground-truth reference
     slide. **Constraint we already hit:** public SlidesBench does not ship reference decks for every
     task — the very reason we went reference-free. Use it where references exist; it is the strongest
     anti-hack signal there is.
   - *Required-content check done right:* the instruction names specific facts; score their presence
     **and** cap padding (presence × conciseness), so it can't be farmed the way plain coverage was.
   - *VLM-judge anchor:* we measured Claude judges tracking the human at **+0.63** (grader +0.21). Too
     slow/gameable for a per-rollout reward, but usable as a periodic validation gate or to re-weight —
     an independent content signal the geometric reward structurally lacks.
2. **SFT warm-start + tight-KL RL (reconsider the killed idea).** Warm-start on real slide code so the
   *prior* already makes good content, then let RL polish *form* at the margin under a tight KL leash so
   it cannot wander to generic-safe. RL-from-a-weak-prior against a form-only reward is precisely the
   gameable regime we are in; a good prior + small KL is how AutoPresent-quality content survives RL.
3. **Accept the reward's true scope.** A reference-free geometric reward can only ever teach *form*
   (don't overlap, don't overflow, fill appropriately). It is legitimate as a *polish* signal on top of
   a model that already writes good content — never as the sole signal from a weak prior. On high-level
   prompts, where content is most of the quality, a form-only reward will drift to generic every time.

**Bottom line:** our reward is not one metric away from correct. It is measuring the wrong thing
(form, not correctness) with the wrong optimizer for that signal (adversarial RL, no target). Until a
content/adherence signal or a good prior enters the loop, every new metric buys one patched hole and the
next one opens somewhere else.

---

## 2026-07-31 — adherence term added + adversarial red-team (3 agents)

Added a padding-proof `adherence` term (uses the `required_texts` the data already carried;
title-level anchor on high-level prompts) as a guard in the weighted sum + soft-min gate. Then
3 red-team agents adversarially probed the NEW reward. Results:

**Fixed (clean, verified bug-fixes — kept):**
- *Adherence ghost-text bypass (critical, was self-inflicted):* `_deck_text_norm` read every
  shape's text regardless of size/color/position, so the required strings could be hidden in a
  1pt/white/off-canvas box → adherence 1.0 while the visible slide is generic. FIXED: adherence
  text now comes only from on-canvas, non-trivial-area shapes with runs ≥6pt; token matching is
  word-boundary + keeps numeric tokens. Verified: ghost/off-canvas/tiny → adherence 0; visible
  title → 1.0.
- *Render-failure fallback (severe, PRE-EXISTING):* on a failed render, `content`/`clipping`
  fell back to 0.5 — which the arithmetic shows is a *reward* (a forced render failure banked
  ~0.63–0.68 vs the ~0.1 a blank slide deserves, and dodged the emptiness gate). FIXED: fallback
  is now 0.0 (content is a critical-gate member → floors the score) with one retry for transient
  flake; soffice/pdftoppm timeout 600→120s so a hang-attack fails fast.

**Confirmed severe, NOT patched — this is the architectural wall:**
- *Decorative-content farm (measured on the box, score 0.829, content 0.963):* a tuned grid of
  ZERO-AREA connector lines farms `content` (=detail_cov=edge pixels anywhere) to ~1.0 while
  being invisible to every geometry metric (zero-area shapes are dropped by `geom_shapes`), and
  generic filler pins `density`=1.0. A meaningless slide scores ~0.83, nearly a good deck (0.78).
  Same class: full-bleed decorative image (Exploit 4), generic-filler density (Exploit 2), the
  bland safe-template that trips no defect detector (Exploit 3).

The red-team's own conclusion matches this doc's thesis: **the load-bearing `content` metric
scores edge-pixels not meaning, and no render-free patch closes the class** — "the rest needs
the content/reference signal REWARD-HACKABILITY.md argues for." The single highest-leverage
patch (restrict `detail_cov` to authored text/picture boxes) kills the decoration + image farms
but recalibrates the content metric (rho +0.544) and needs re-validation on the audit decks —
i.e. it is past "a quick fix." **STOP-AND-QUESTION-ARCHITECTURE point reached** (systematic-
debugging Phase 4.5): fixes now reveal the next hole each time; the durable answer is the
reference/content signal or an SFT warm-start, not patch N+1.

---

## 2026-07-31 (cont.) — content-box-masking implemented + re-validated

Restricted `content`/detail_cov to authored-content boxes (text-bearing shapes, pictures,
charts, tables; connectors/lines/empty-shapes excluded), normalized to whole-image pixels
(`render_metrics.slide_content_mask` + `slide_measures(content_mask=)` + `deck_measures`).

**Re-validation on the 45 blind designer decks (calibration.py corpus):**
- content_score rho vs designer grades **+0.588 → +0.626** (IMPROVED — out-of-box edges were noise).
- detail_cov rho +0.558 → +0.592. Band unchanged (rho improved; no retune needed).
- Pure-decoration farm (connector grid + title, no real text): **0.83 → 0.457** — KILLED.
- Gate still passes; good fixture 0.777→0.686 (its non-text card/rule decoration no longer
  counts as content), still clearly above every bad fixture.

**Residual (architectural, NOT closed by masking):** grid+title+**filler** still scores 0.826,
because generic filler text is in-box and farms content(0.947)+density(1.0)+adherence(1.0=has
title). No form metric can separate dense-filler from dense-real-content — this is exactly the
"needs a reference/content signal" wall. Masking is a real win (calibration + decoration farm)
but the filler/generic-template farm remains until the reference-based or SFT-warm-start path.

**State after today's reward work:** adherence guard + ghost-fix + render-fallback fix +
content-box masking, all gate-verified, experiment shape unchanged. The reward is materially
harder to game (empty, empty-chart, ghost-text, render-fail, pure-decoration all closed +
calibration up). Remaining known-open: generic-filler content/density farm and the bland
safe-template — both require the architectural content signal, not another patch.
