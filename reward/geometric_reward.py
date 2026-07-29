"""
Geometric PPTX reward — the ungameable replacement for the text-coverage proxy.
RENDER-FREE version: everything is computed from python-pptx, no soffice/pixels.

WHY RENDER-FREE
---------------
The pixel-based whitespace metric required rendering every rollout with soffice. At 64
rollouts/step that spawned 64 concurrent soffice processes, which saturated the box CPU:
~83 min/step and the machine became unresponsive. The content-density signal we actually
need (is the slide appropriately full — not empty, not crammed) can be estimated directly
from the deck's text volume and shape coverage, with no rendering. ~50x faster and it does
not fork a subprocess storm.

WHY THIS REWARD RESISTS GAMING
------------------------------
Every metric is computed from slide GEOMETRY / text VOLUME, never from text presence:
  collision  overlapping shapes                -> padding overlaps -> worse
  overflow   shapes spilling off the canvas    -> padding spills   -> worse
  imbalance  visual centroid off-centre        -> padding unbalances -> worse
  density    text volume in a healthy band     -> padding = too much text -> worse
                                                -> empty  = too little     -> worse
Padding a slide moves EVERY metric the wrong way. That is what "hard to game" means.

DESIGN RULES (AeSlides arXiv 2604.22840 + our failure analysis)
  1. Invalid output scores 0.0 — NEVER negative. A negative floor makes every rollout in a
     group identical -> zero relative advantage -> zero gradient (arXiv 2601.03525). That is
     how the previous run died.
  2. Every metric is on a common [0,1] scale (1.0 = good).
  3. density is weighted 2x the shape metrics: the shape metrics only measure ABSENCE OF
     DEFECTS, which a near-empty slide satisfies for free; density is the one metric that
     requires real, appropriately-sized content, so it separates a real slide from a lazy one.
  4. Weighted SUM, not product: a product sends most decks to ~0, collapsing group variance
     (the gradient GRPO needs). Smooth partial credit keeps the spread.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# NOTE on "aspect": its variance is ~0 *because the model currently complies* (the system
# prompt states the 16:9 canvas), NOT because it is useless. It is kept at a small weight as a
# REGRESSION GUARD: training from a fresh checkpoint, a different base model, or dropping the
# prompt hint all make it vary again, and it is the only metric that catches a drift back to
# 4:3. A constant-because-satisfied metric is not the same as a constant-because-uninformative
# one -- only the latter should be dropped.
WEIGHTS = {
    # --- v2 (2026-07-29) --------------------------------------------------------
    # Re-weighted against the 45 blind-graded audit decks. rho = Spearman vs a
    # designer's grade. The v1 grader scored rho -0.139 overall: WORSE than chance.
    #
    # "content" is the single strongest signal found (rho +0.544, AUC 0.826) and is the
    # term that fixes the structural failure: every other metric is a DEFECT detector,
    # and a near-empty slide has no defects, so v1 paid the model to delete content.
    # Measured consequence of that: over 30 GRPO steps text/slide fell 41% and font size
    # 54% while the score rose 95%.
    #
    # collision (rho -0.534) and textfit (rho -0.431) are the two most ANTI-correlated
    # metrics with designer judgement, so both are demoted.
    "content":   0.20,   # NEW, render-based: real ink coverage, band-limited both sides
    "collision": 0.12,   # was 0.19 -- anti-correlated, demoted
    "overflow":  0.10,   # was 0.15
    "density":   0.10,   # was 0.13 -- partly superseded by content
    "imbalance": 0.09,   # was 0.11
    "picfit":    0.09,   # was 0.10
    "textfit":   0.08,   # was 0.13 -- anti-correlated, demoted
    "contrast":  0.08,   # was 0.09
    "clipping":  0.07,   # NEW, render-based: ink cut off at the canvas edge.
                         # Deliberately SMALL -- it is another defect detector, and defect
                         # detectors are what caused the sparseness bias. It exists to catch
                         # what geometry provably cannot see (a text box legally on-canvas
                         # whose rendered text is not), not to carry the calibration.
    "alignment": 0.04,   # was 0.05
    "aspect":    0.03,   # was 0.05 -- constant-because-satisfied regression guard
}

# --- aspect-ratio target (16:9 widescreen) -----------------------------------
# The model calls Presentation() and inherits python-pptx's 4:3 default (10x7.5in)
# instead of setting 16:9 (13.333x7.5in). We reward closeness to 16:9 on a log-ratio
# curve: 16:9 -> 1.0, 16:10 -> ~0.80, 4:3 -> ~0.19, square -> ~0.
ASPECT_TARGET = 16.0 / 9.0
ASPECT_K = 20.0

INVALID_REWARD = 0.0

# Worst-critical-metric multiplier floor: min_critical=0 -> score x0.55; =1 -> unchanged.
SOFT_FLOOR = 0.55

# Emptiness gate: a near-empty deck gets every geometric metric for free, so density alone
# cannot punish it enough. Continuous at the knee (dens=KNEE -> multiplier 1.0, no cliff).
EMPTY_KNEE = 0.25
EMPTY_FLOOR = 0.30

# --- content-density band (characters of text per slide) --------------------
# Tuned against the fixtures: good deck ~232 chars/slide (healthy), padded ~4900
# chars/slide (crammed), empty 0, a one-word "Q3" deck ~2. We want good high, the rest low.
DENS_EMPTY = 0.0      # <= this -> score 0 (blank)
DENS_LO = 60.0        # ramp 0->1 between EMPTY and LO
DENS_HI = 900.0       # full score through the [LO, HI] band
DENS_CRAM = 2600.0    # >= this -> score 0 (crammed / padded)


def _smoothstep(edge0, edge1, x):
    if edge1 == edge0:
        return 1.0 if x >= edge1 else 0.0
    t = (x - edge0) / (edge1 - edge0)
    t = max(0.0, min(1.0, t))
    return t * t * (3.0 - 2.0 * t)


def _slide_chars(slide):
    n = 0
    for sh in slide.shapes:
        if sh.has_text_frame:
            for para in sh.text_frame.paragraphs:
                for run in para.runs:
                    n += len(run.text or "")
    return n


def score_density(pptx_path):
    """Render-free content density: is each slide appropriately full?

    Returns a float in [0,1], 1.0 = good. Both extremes (empty and crammed) score low, so
    it punishes BOTH the padding exploit and the empty-slide exploit.
    """
    from pptx import Presentation
    prs = Presentation(pptx_path)
    slides = list(prs.slides)
    if not slides:
        return 0.0
    per = []
    for slide in slides:
        c = _slide_chars(slide)
        low = _smoothstep(DENS_EMPTY, DENS_LO, c)       # 0 when empty, 1 once past LO
        high = 1.0 - _smoothstep(DENS_HI, DENS_CRAM, c)  # 1 in-band, 0 once crammed
        per.append(max(0.0, min(1.0, low * high)))
    return sum(per) / len(per)


def score_aspect(pptx_path):
    """Render-free 16:9 compliance from the deck's slide dimensions. [0,1], 1.0 = 16:9."""
    import math
    from pptx import Presentation
    prs = Presentation(pptx_path)
    w, h = prs.slide_width, prs.slide_height
    if not w or not h or h <= 0:
        return 0.0
    ratio = float(w) / float(h)
    d = abs(math.log(ratio / ASPECT_TARGET))
    return max(0.0, min(1.0, math.exp(-ASPECT_K * d * d)))


def score_deck(pptx_path, png_paths=None):
    """Score a single deck geometrically. png_paths is accepted but ignored (render-free).

    Returns {'score', 'metrics', 'valid', 'reason'}. An unopenable deck scores 0.0.
    """
    if not pptx_path or not os.path.isfile(pptx_path):
        return {"score": INVALID_REWARD, "metrics": {}, "valid": False,
                "reason": "no pptx produced"}

    try:
        from geom_shapes import score_shapes
        shape_scores = score_shapes(pptx_path)
    except Exception as e:
        return {"score": INVALID_REWARD, "metrics": {}, "valid": False,
                "reason": "pptx unopenable: %s" % e}

    metrics = {}
    for k in ("collision", "overflow", "imbalance", "textfit", "contrast", "picfit", "alignment"):
        metrics[k] = float(shape_scores.get(k, 0.0))
    try:
        metrics["density"] = float(score_density(pptx_path))
    except Exception:
        metrics["density"] = 0.0
    try:
        metrics["aspect"] = float(score_aspect(pptx_path))
    except Exception:
        metrics["aspect"] = 0.0

    # ---- render-based metrics (v2) --------------------------------------------
    # Geometry reads the shape BOX; a viewer sees the rendered INK. That gap is not a
    # detail -- a deck whose third bullet is visibly cut off at the slide edge scored
    # collision=1.000, overflow=1.000, textfit=1.000, total 0.9204, because its text box
    # was legally inside the canvas. Only pixels can see that.
    # Cost: 0.34 s/deck batched = ~22 s on a 64-rollout step = 0.8% of a 44-min step.
    # The "rendering is too slow for RL" assumption was never measured and was wrong.
    # If rendering fails we fall back to neutral 1.0 rather than crashing: a reward
    # exception kills a training run.
    try:
        import render_metrics as _rm
        _r = _rm.score_render(pptx_path)
    except Exception:
        _r = None
    if _r:
        metrics["content"] = float(_r["content"])
        metrics["clipping"] = float(_r["clipping"])
        render_ok = True
    else:
        # A render failure must NEVER be rewarded. Returning 1.0 here made a failed render
        # score BETTER than a successful one, which is both a hack surface and the reason
        # a concurrency race showed up as reward drift. 0.5 is deliberately neutral-poor:
        # we could not assess the pixels, so we neither credit nor fully condemn the deck.
        metrics["content"] = 0.5
        metrics["clipping"] = 0.5
        render_ok = False

    total = wsum = 0.0
    for k, w in WEIGHTS.items():
        if k in metrics:
            total += w * max(0.0, min(1.0, metrics[k]))
            wsum += w
    score = (total / wsum) if wsum else INVALID_REWARD

    # SOFT-MIN GATE (from hand-audit): a weighted mean lets ONE catastrophic defect be
    # averaged away by five good metrics -- content fully off-screen (overflow=0.00) still
    # scored 0.65. We multiply by a factor driven by the WORST critical metric, so a
    # catastrophe roughly halves the score. Kept multiplicative-but-smooth (never a cliff,
    # never zero) so group variance -- the GRPO gradient -- survives.
    # `clipping` joins the critical set in v2: rendered content cut off at the canvas edge
    # is information the viewer never sees, which is exactly the catastrophe this gate is
    # for. Without it the falsifying deck only fell 0.9204 -> 0.8649, because clipping
    # carries just 0.07 of the weighted sum.
    # v2 critical set. Two changes, both measured:
    #  + content  -- a slide that says nothing is the catastrophe this reward missed for 30
    #                steps. On food/slide_22 the model replaced real quiz answers with
    #                "Option A/B/C"; content correctly reads 0.000 there vs 0.480 before.
    #  - textfit  -- REMOVED. It is anti-correlated with designer judgement (rho -0.431) and
    #                is the metric the policy games: shrinking type makes it perfect. It was
    #                gating the GOOD slide (textfit 0.240 -> factor 0.658) while the gutted
    #                one sailed through at 1.000. A gate driven by the exploited metric
    #                rewards the exploit.
    crit = [metrics.get(k, 1.0) for k in ("collision", "overflow", "clipping", "content")
            if k in metrics]
    if crit:
        score *= SOFT_FLOOR + (1.0 - SOFT_FLOOR) * min(crit)

    # ---- emptiness gate -------------------------------------------------------
    # An almost-empty slide trivially satisfies EVERY geometric constraint: nothing can
    # collide, overflow, misfit, or clash if nothing is on the canvas. Before this gate a
    # 1-shape deck scored 0.85 (density 0.07 x 0.13 weight is only a 0.12 penalty, and the
    # critical gate above sees collision/overflow/textfit all at a free 1.00).
    # That is a degenerate optimum, and GRPO finds those.
    # Found by hand-audit: technology/slide_12, "lowkey empty", scored 0.85.
    # Only bites below the knee -- the corpus has a clean gap at 0.15-0.20 and 86% of real
    # decks sit above 0.70, so normal slides are untouched.
    dens = metrics.get("density", 1.0)
    if dens < EMPTY_KNEE:
        score *= EMPTY_FLOOR + (1.0 - EMPTY_FLOOR) * (dens / EMPTY_KNEE)

    return {"score": round(score, 4),
            "render_ok": render_ok,
            "metrics": {k: round(v, 4) for k, v in metrics.items()},
            "valid": True, "reason": "ok"}


def compute_score(data_source, solution_str, ground_truth, extra_info=None, **kwargs):
    """Direct verl hook (unused by the single-turn path, which extracts+runs code first)."""
    extra_info = extra_info or {}
    result = score_deck(extra_info.get("pptx_path"))
    out = {"score": result["score"], "valid": 1.0 if result["valid"] else 0.0}
    for k in ("collision", "overflow", "imbalance", "density"):
        out["m_" + k] = float(result["metrics"].get(k, 0.0))
    return out
