# Proof-of-life: Qwen3.6-27B hill-climbs on PPTX slide generation

**Result: held-out reward 0.502 → 0.716 (+43%) while response length fell 57%.**
Run `highlevel-v2-27b`, completed 15/15 steps, 2026-07-26.

## The numbers

| eval | step 0 (baseline) | step 3 | step 6 | step 9 | step 12 |
|---|---|---|---|---|---|
| **held-out reward** | 0.502 | 0.556 | 0.489 | **0.716** | **0.716** |

+0.214 over baseline ≈ **4.8 standard errors** (SE ≈ 0.045 at 45 tasks × 1 sample),
and confirmed by two consecutive evals at the same value.

| | start | peak | end |
|---|---|---|---|
| train reward | 0.508 | 0.843 | 0.641 |
| response length | 4,974 | 7,785 | **3,331** |
| truncation (clip) | 0.30 | 0.73 | **0.03** |

**The anti-hacking condition held:** reward rose *while* output shrank. A reward-hacking
policy inflates output; this one got better at slides while writing 57% less code.

## What it actually learned (146 sampled rollouts, first quartile vs last)

| metric | early | late | change |
|---|---|---|---|
| **collision** (no overlapping elements) | 0.64 | 0.89 | **+0.25** |
| textfit (text fits its box) | 0.93 | 0.98 | +0.05 |
| overall slide score | 0.71 | 0.83 | +0.12 |
| tokens per response | 4,996 | 1,850 | −63% |
| density (content amount) | 0.91 | 0.81 | −0.10 |
| imbalance | 0.71 | 0.67 | −0.03 |

The dominant gain is **collision** — the model learned to stop putting images on top of
text and overlapping its text boxes. That is precisely the defect class surfaced by hand-
auditing the slide gallery, which is what prompted the grader fix that made it measurable.

Honest caveat: density and balance drifted slightly down — plausibly a side effect of much
shorter output (less text on the slide). Worth watching, not yet a problem.

## What made it work (four earlier runs failed)

| run | prompts | lr | grader | outcome |
|---|---|---|---|---|
| geo8 | detailed | 1e-6 | pre-audit | flat — model already near ceiling, no headroom |
| highlevel-v1 | high-level | 3e-6 | post-audit | **degenerated** — length spiral, held-out 0.475→0.305 |
| **highlevel-v2** | high-level | **2e-6** | post-audit + rebalance | **+43%** |

Four things had to be true at once:

1. **A grader that actually detects defects.** The original divided each defect by the
   total area of all shapes, so an image covering a text box scored 0.96. Fixed to measure
   per-shape. Also added text-fit (paragraph-level font sizes — the original only read run-
   level and assumed 18pt), and a soft-min gate so one catastrophic defect can't be averaged
   away by five good metrics.
2. **A task with headroom.** Detailed prompts let the model transcribe a spec; it was
   already near-perfect, so there was nothing to learn. High-level one-line prompts force it
   to design the layout.
3. **A learning rate in the usable band.** 1e-6 → no movement; 3e-6 → degeneration;
   2e-6 → climb.
4. **A response-length cap that bites.** 8,192 tokens. The model rambled into heavy
   truncation around step 4 (73% clipped), got punished, and *taught itself to be concise* —
   clip fell to 3%. Without the cap the previous run never escaped that spiral.

## Reward function (all render-free, from python-pptx geometry)

| metric | weight | what it measures |
|---|---|---|
| collision | 0.24 | per-shape overlap; text coverage penalized steeply (unreadable) |
| overflow | 0.22 | per-shape area outside the canvas, with a 2% safe margin |
| textfit | 0.18 | estimated rendered text height vs box height |
| density | 0.18 | content volume in a healthy band (punishes empty *and* crammed) |
| imbalance | 0.12 | area-weighted centroid offset from centre |
| aspect | 0.06 | 16:9 compliance (mostly prompt-solved) |

Invalid output scores **0.0, never negative** — a negative floor makes every rollout in a
GRPO group identical, which is zero relative advantage and therefore zero gradient. That
mistake collapsed the very first agentic run.

**Adversarial gate (run before every training launch):** a padded deck (20× the text,
overlapping, off-canvas) must score strictly below a clean one. Final margin: clean 0.99
vs padded 0.18. The original text-coverage reward scored them **identically at 1.00** —
which is exactly why the model learned to pad.

## Known limits

- Held-out eval is 45 tasks × 1 sample (SE ≈ 0.045). Fine for this +0.21 effect; too weak
  for the +2–3% originally targeted. Use `val_kwargs.n=4` next time.
- **No saved checkpoint from THIS run** — `save_freq=-1` was set to dodge an FSDP2 + CPU-offload
  save bug (`_save_to_state_dict` device mismatch), so the weights from the +43% run are gone;
  only the curve and the sampled slides survive. **This is now SOLVED** — see
  "CHECKPOINTING — SOLVED AND VERIFIED" at the end of this document. The successor run
  (`highlevel-v3-30step`) saves every 5 steps and its checkpoints merge to a loadable
  HuggingFace model.
- Aesthetics beyond geometry ("just ugly") are not captured. Alignment (LayoutGAN-style
  shared edges/centres) is implemented and gate-tested but was not active in this run.
- Single-turn, not the agentic render-inspect-fix loop (that harness is built and idle).

## Artifacts

- `slides/highlevel_v2_FINAL/` — 146 decks, each with the model's code and its scores
- `slide-gallery-before-after.html` — early vs late slides side by side
- `reward/` — the full grader, the adversarial gate, and the run script

## Grader fixes from hand-audit round 3 (post-run, for the next run)

| ID | issue found by eye | outcome |
|---|---|---|
| 9f2013 | full-bleed background image counted as "overlap" | **FIXED 0.40 → 0.94** — shapes covering >85% of the canvas are treated as backgrounds and excluded from the collision test on both sides. This bug was teaching the model to avoid background images (and likely drove the density decline). |
| 8bff93 | content doesn't fit | already correctly scored 0.41 |
| c0e600 | bad fit / balance | already correctly scored 0.38 |
| d12f2f | text renders off screen | **still missed** — a char-count text estimator is too coarse; needs real font metrics (Pillow `FreeTypeFont.getlength()`, per the research) |
| 2fe501 | renders blank | **still missed** — the slide has two text boxes, so this is almost certainly *invisible* text (e.g. white on white). Requires a colour-contrast check, which pure geometry cannot provide. |
| 3c1ba8 | a row of content is missing | **out of scope** — content correctness, not layout geometry |

Regression check after the fix: of 40 slides, 36 unchanged, 2 up, 2 down. Adversarial gate
still passes (clean 0.99 vs padded 0.18).

### Next grader work, in priority order
1. **Colour contrast** — text colour vs its shape fill / slide background. Catches invisible
   text, cheap, render-free.
2. **Real font metrics for text-fit** — Pillow FreeTypeFont measurement instead of the
   0.5×font-size character-width approximation.
3. **Alignment** (LayoutGAN-style shared edges/centres) — implemented and gate-tested but not
   yet active in a training run; addresses the "just looks ugly / not symmetrical" class.

### Audit round 3 — resolution

| ID | issue | outcome |
|---|---|---|
| 9f2013 | full-bleed background image counted as overlap | **FIXED** 0.40 → 0.94 |
| 2fe501 | renders blank (invisible text) | **FIXED** — new contrast metric scores it 0.00; slide 0.83 → 0.75 |
| 3c1ba8 | blank table row | **DETECTED** — row 7 is empty; folded into density (see rule below), not given its own weight |
| 8bff93 / c0e600 | doesn't fit / bad balance | already correctly scored 0.41 / 0.40 |
| d12f2f | text renders off screen | still missed — needs real font metrics (Pillow), on the list |

**Colour contrast metric (weight 0.10).** WCAG relative-luminance ratio between each text
run's explicit RGB and its shape fill / background; 4.5:1 → 1.0. Two deliberate neutral
cases, both to avoid punishing things we cannot actually see render-free:
theme-inherited colour (no explicit RGB) scores 1.0, and text over a full-bleed **photo**
scores 1.0 (its pixel colours are unknowable; judging against an assumed white canvas
wrongly flagged correct white-on-photo design and would push the model away from
background images).

### THE RULE for adding any future metric

**Measure its variance across real rollouts before giving it weight.**
GRPO learns only from differences *within* a group of rollouts. A metric that is constant
contributes exactly zero gradient while diluting the metrics that do teach.

Measured on the 146-slide corpus:

| candidate | coverage | sd | decision |
|---|---|---|---|
| contrast | 54/146 decks | **0.355** | added, weight 0.10 — real signal |
| blank table row | 2/146 decks | 0.062 | **not** weighted — folded into density |
| aspect ratio | all decks 4:3 | ~0 | was dead weight; solved via the prompt instead, weight cut to 0.06 |

Final weights: collision 0.22 · overflow 0.20 · textfit 0.16 · density 0.16 · contrast 0.10 ·
imbalance 0.10 · aspect 0.06.
Regression after both fixes: 146 slides — 21 up, 10 down, 115 unchanged (mean +0.02).
Adversarial gate still passes: clean 0.99 vs padded 0.21.

---

## CHECKPOINTING — SOLVED AND VERIFIED (2026-07-28)

The previous run left no weights. That is now fixed end-to-end.

### Root cause
A missing guard in verl's `save_checkpoint`, not a memory limit. Four attempts to solve it by
rearranging GPU memory all failed (offload off → actor 60-68 GB → OOM at rollout TP=4 and TP=8)
because the problem was device placement, not capacity.

`verl/workers/engine/fsdp/transformer_impl.py` calls `load_fsdp_model_to_gpu(self.module)`
(i.e. `model.to(device)`) before saving. Under FSDP2 CPUOffloadPolicy that half-moves the module
and `state_dict()` raises. verl guards its **weight-sync** path with exactly this condition and
the **save** path was missing it.

### The fix (one guard) — matches upstream exactly
```python
if (self._is_offload_param or origin_module_device == "cpu") and not getattr(
    self, "_uses_fsdp2_cpu_offload_policy", False
):
    load_fsdp_model_to_gpu(self.module)
```
Official upstream: **PR #6604** (main, commit `a539474`) and **PR #7077** (cherry-pick into
`release/v0.8.0`, merged 2026-07-24, validated upstream on Qwen3.5-35B-A3B with
`offload_policy/param_offload/optimizer_offload` all True). The fix landed **4 days after** the
v0.8.0 tag we are pinned to and there is no v0.8.1, so it cannot be obtained by version bump.

### Verified on our box
| stage | result |
|---|---|
| save during training, offload ON | `ckpt_v3/global_step_5/actor/` — 8 shards, 93 GB, run continued |
| merge to HF | `python -m verl.model_merger merge --backend fsdp` → 51 GB, exit 0 |
| model validity | `Qwen3_5ForConditionalGeneration`, 1,184 tensors, 64 layers, lm_head [248320, 5120], tensors readable |

### Commands
```bash
# during training (config)
trainer.save_freq=5
trainer.default_local_dir=/home/ubuntu/powerbench/ckpt_v3
+trainer.max_actor_ckpt_to_keep=2          # ~93 GB per checkpoint
+actor_rollout_ref.actor.checkpoint.save_contents=['model','extra']

# afterwards, CPU-only, safe to run while training continues
python -m verl.model_merger merge --backend fsdp \
  --local_dir  ckpt_v3/global_step_N/actor \
  --target_dir hf_export_stepN
```

### Dead ends (don't repeat)
- `offload_policy=False` — save works but actor needs 60-68 GB; OOMs beside vLLM at any TP.
- vLLM `gpu_memory_utilization` below ~0.25 at TP=4 — cannot even load the weights.
- `checkpoint_engine` (naive/nccl/nixl) is trainer→rollout weight transfer, not disk saving.
- `async_save` is Megatron-only, not FSDP.

---

## Grader review + improvements (2026-07-28)

### Bug found: three metrics were DEAD CODE
`_alignment_score`, `_picture_distortion_score` and `_blank_table_rows` were all defined but
never called from `_score_slide`. Two earlier claims in this document were therefore wrong and
are corrected here: blank-table-rows was *not* "folded into density", and alignment was *not*
active in any training run.

### Which metrics actually earn their weight (measured on 180 live rollouts)
GRPO learns only from **within-group variance**; a metric with sd~0 contributes zero gradient
while consuming weight that could fund one that teaches.

| metric | old weight | sd | verdict |
|---|---|---|---|
| collision | 0.22 | 0.307 | strong |
| contrast | 0.10 | 0.300 | strong |
| density | 0.16 | 0.269 | strong |
| imbalance | 0.10 | 0.229 | strong — and 72% of slides score <0.9 (most headroom) |
| overflow | 0.20 | 0.186 | moderate |
| textfit | 0.16 | 0.144 | moderate |
| **aspect** | **0.06** | **0.000** | **dead weight** — the prompt fix solved it perfectly, so it can no longer teach |

### Candidate metrics, measured before wiring
| candidate | sd | frac <0.9 | decision |
|---|---|---|---|
| **picfit** (image stretched off native aspect) | **0.266** | **52%** | **WIRED IN** — half of all slides affected, and the grader was blind to it |
| **alignment** (shared edges/centres) | 0.164 | 16% | wired in at modest weight |
| blank table rows | 0.000 | — | left out — constant, would repeat the aspect-ratio mistake |

### New weights
collision 0.20 · overflow 0.16 · textfit 0.14 · density 0.14 · imbalance 0.12 ·
picfit 0.10 · contrast 0.09 · alignment 0.05  (aspect dropped to 0)

Verified: adversarial gate still passes (clean 0.988 vs padded 0.202, margin 0.786).
Regression on 180 live slides: 0 up, 37 down, 143 unchanged (mean −0.012) — the drops are
slides whose stretched images / misalignment were previously invisible.

### Still outstanding
1. **textfit misses text that renders off-screen** (audit ID d12f2f). The char-count estimate
   (0.5 x font-size per glyph) is too coarse; needs real font metrics via Pillow
   `FreeTypeFont.getlength()`, which needs a TTF on disk but no rendering.
2. **Content correctness is not measured at all** — the grader scores layout, never what the
   slide says. That is the gap for a SlideBench Track A entry.
3. **Single-slide only** — no deck-level narrative, ordering, or consistency.
