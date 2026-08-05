# Instruction-Adherence Reward Term — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use `- [ ]` checkboxes.

**Goal:** Close the biggest hole in the geometric reward — it is blind to what the slide was asked to say — by wiring the `required_texts` the dataset ALREADY carries into the reward as a padding-proof adherence signal, without changing the experiment's shape.

**Architecture:** The dataset's `reward_model.ground_truth` already contains `{"required_texts": [...]}` per task (extracted from the instruction). The reward currently receives it and ignores it. We add `score_adherence(pptx, required_texts)` = fraction of required strings present on the slide, wire it into `score_deck`, add it to the weighted sum (small weight) AND the critical soft-min gate (so an off-instruction slide is penalized). It behaves as a **guard** (like `chart_ok`): ~1.0 for normal outputs, biting only when the model drifts to generic/off-instruction content. Padding-proof because it rewards presence of SPECIFIC required content (generic filler lacks it) and the existing density band + emptiness gate already punish padding — the failure mode that killed the old text-coverage reward.

**Scope honesty:** For HIGH-LEVEL prompts the `required_texts` are sparse (mean 1.4/task, usually just the title), so this is a *title-level* anchor: it closes the "ignores the task entirely" attack but does not guarantee rich content. The strong version needs detailed prompts or reference decks (unavailable). This is the right minimal fix for the staged high-level run.

**Tech Stack:** python-pptx, the existing `reward/` modules, verl custom reward hook.

---

## File Structure

- Modify `reward/geometric_reward.py` — add `score_adherence()` + `_deck_text()`/`_norm()` helpers; add `adherence` to metrics, WEIGHTS, and the critical set; thread `required_texts` through `score_deck()` and `compute_score()`.
- Modify `reward/singleturn_geometric_reward.py` — parse `ground_truth` JSON → `required_texts`, pass to `score_solution`→`score_deck`; add `adherence` to `_METRIC_KEYS`.
- Modify `reward/make_fixtures.py` — add `build_ontopic()` and `build_offtopic()` (same good form, one carries required title text, one carries generic title).
- Modify `reward/gate_test.py` — assert an on-topic deck scores strictly higher than an off-topic deck given the same required_texts.

---

### Task 1: `score_adherence` in geometric_reward.py

**Files:** Modify `reward/geometric_reward.py`

- [ ] **Step 1 — Add helpers + scorer** (near `score_density`):

```python
import re as _re_adh

def _norm_text(s):
    return _re_adh.sub(r"[^a-z0-9 ]+", " ", (s or "").lower())

def _deck_text_norm(pptx_path):
    from pptx import Presentation
    prs = Presentation(pptx_path)
    parts = []
    for slide in prs.slides:
        for sh in slide.shapes:
            if getattr(sh, "has_text_frame", False):
                parts.append(sh.text_frame.text or "")
    return " ".join(_norm_text(" ".join(parts)).split())

ADHERENCE_TOKEN_FRAC = 0.8   # a required phrase counts as present if >=80% of its
                             # significant tokens appear (robust to punctuation/wording)

def score_adherence(pptx_path, required_texts):
    """Fraction of the instruction's required strings present on the slide. Ties the slide
    to WHAT IT WAS ASKED TO SAY -- the axis geometry/ink cannot see. No required_texts -> 1.0."""
    if not required_texts:
        return 1.0
    hay = _deck_text_norm(pptx_path)
    hits = 0
    for t in required_texts:
        nt = " ".join(_norm_text(t).split())
        if not nt:
            hits += 1; continue
        toks = [w for w in nt.split() if len(w) >= 3]
        if nt and nt in hay:
            hits += 1
        elif toks and sum(1 for w in toks if w in hay) / len(toks) >= ADHERENCE_TOKEN_FRAC:
            hits += 1
    return hits / len(required_texts)
```

- [ ] **Step 2 — Verify import-level self-test** (one-off on box, python-pptx available):

Run a deck with text "PRONUNCIATION ACTIVITY today" against `required_texts=["PRONUNCIATION ACTIVITY"]` → expect `1.0`; against `["Strategic Overview"]` → expect `0.0`.

---

### Task 2: Wire adherence into `score_deck` + weights + critical gate

**Files:** Modify `reward/geometric_reward.py`

- [ ] **Step 1 — Add weight** (in `WEIGHTS`, then wsum auto-renormalizes):

```python
    "adherence": 0.10,   # NEW: does the slide say what the instruction asked? (title-level
                         # anchor on high-level prompts). Guard against the generic-template
                         # attack that a form-only reward cannot see.
```

- [ ] **Step 2 — Signature + compute** (change `def score_deck(pptx_path, png_paths=None):` to accept `required_texts`, and after the render block set the metric):

```python
def score_deck(pptx_path, png_paths=None, required_texts=None):
    ...
    try:
        metrics["adherence"] = float(score_adherence(pptx_path, required_texts))
    except Exception:
        metrics["adherence"] = 1.0   # never punish on a scorer error
```

- [ ] **Step 3 — Add to critical soft-min set:**

```python
    crit = [metrics.get(k, 1.0) for k in
            ("collision", "overflow", "clipping", "content", "chart_ok", "adherence")
            if k in metrics]
```

- [ ] **Step 4 — Thread through `compute_score`:**

```python
def compute_score(data_source, solution_str, ground_truth, extra_info=None, **kwargs):
    extra_info = extra_info or {}
    req = _parse_required(ground_truth)
    result = score_deck(extra_info.get("pptx_path"), required_texts=req)
    ...
```
with a helper:
```python
def _parse_required(ground_truth):
    import json
    try:
        gt = json.loads(ground_truth) if isinstance(ground_truth, str) else (ground_truth or {})
        return gt.get("required_texts") or None
    except Exception:
        return None
```

---

### Task 3: Parse ground_truth in the single-turn reward + log the metric

**Files:** Modify `reward/singleturn_geometric_reward.py`

- [ ] **Step 1 — Parse + pass required_texts.** In `score_solution`, accept `required_texts` and pass to `score_deck(deck, required_texts=required_texts)`. In `compute_score`, parse `ground_truth` (JSON string `{"required_texts":[...]}`) and pass it down.

- [ ] **Step 2 — Log it.** Add `"adherence"` to `_METRIC_KEYS`; default in `_METRIC_DEFAULT` is `1.0` (like `chart_ok`).

---

### Task 4: On-topic vs off-topic gate fixtures

**Files:** Modify `reward/make_fixtures.py`, `reward/gate_test.py`

- [ ] **Step 1 — Two fixtures, identical good form, differing only in whether the title matches a required string.** `build_ontopic` title = `"Quarterly Platform Review"`; `build_offtopic` title = `"Strategic Overview"` (generic). Both use the good-deck body.

- [ ] **Step 2 — Gate assertion.** With `required_texts=["Quarterly Platform Review"]`, assert `score_deck(ontopic, required_texts=req).score > score_deck(offtopic, required_texts=req).score`, and that the off-topic deck's `adherence == 0.0`. Fail (exit 1) otherwise.

---

### Task 5: Re-gate + regression + hack-demo re-score (verify on box)

- [ ] **Step 1** — Run `gate_test.py`: all prior gates still pass AND on-topic > off-topic.
- [ ] **Step 2** — Re-score 40 gallery decks with the new reward: confirm mean/spread not collapsed (adherence≈1 for real decks that include their title, so scores barely move — proves "not too crazy").
- [ ] **Step 3** — Re-run the content-free hack-demo deck with a task's real `required_texts`: confirm its adherence=0 and total drops materially vs before.

---

## Self-Review

- **Spec coverage:** adherence scorer (T1), wiring+weights+gate (T2), single-turn parse+log (T3), fixtures+gate (T4), verification (T5). Covered.
- **Type consistency:** `score_deck(pptx_path, png_paths=None, required_texts=None)` used identically in T2/T3/T4; `score_adherence(pptx_path, required_texts)` used in T1/T2; `_parse_required` returns `list|None`, consumed by `score_deck` which treats `None`/`[]` as adherence=1.0. Consistent.
- **Placeholders:** none — all code shown.
- **"Not too crazy" invariant:** same GRPO config, same prompts, same data files; only the reward gains one guard term that is ~1.0 on healthy outputs. Verified by T5-Step-2.
