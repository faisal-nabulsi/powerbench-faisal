#!/usr/bin/env python3
"""calibration.py — the gate that would have caught the grader failure on day one.

WHAT THIS IS FOR
----------------
grader_tests.py answers "is the grader WIRED correctly?" (every weight reaches a metric,
every metric varies, adversarial fixtures rank right, nothing negative).  Every one of those
checks PASSED while the grader was anti-correlated with human judgement on real decks.
Wiring is not validity.

This file answers the different question: "does the grader AGREE WITH A DESIGNER'S EYE?"
It is a CALIBRATION gate, not a unit test.  It never opens a deck's internals; it only ever
asks the grader for a number and compares those numbers against three external references:

  A HUMAN-VS-MODEL   designer-authored decks must, on average, outscore our generated decks.
                     The v1 grader scored our model 0.95+ and human decks 0.55.
  B DESIGNER RANK    Spearman/Kendall vs the 45 blind designer grades in
                     /home/ubuntu/audit/my_grades.json (graded by eye BEFORE unblinding).
                     The v1 grader scored -0.139 here.  This is THE headline number.
  C WITHIN-GROUP     GRPO only ever compares rollouts of the SAME prompt, so ordering inside
                     a prompt group is what the training signal actually consumes.  This can
                     be healthy while B is broken (it was: +0.683 vs -0.139), which is why
                     both are reported and neither substitutes for the other.
  D SEPARATION       what fraction of our generated decks outscore the median human deck.
                     Lower is better.  This is the "did the model learn to farm the metric
                     instead of making good slides" number.

DESIGN RULES
------------
 1. A SKIPPED CHECK IS A FAILED CHECK.  If a reference file is missing or will not join, the
    check reports MISSING and the overall verdict is FAIL.  We shipped a harness once that
    silently skipped a check and printed PASS; that is how bad numbers reach a training run.
 2. The grader is imported LAZILY, inside the scoring call, and is selectable with --grader.
    So this file can be re-run after every fix, and can be pointed at v2 without editing.
 3. This file MODIFIES NOTHING.  It reads decks and reference JSON, and writes only the
    baseline file you explicitly ask for (--baseline).
 4. Every reported number is deterministic: fixed iteration order, fixed permutation seed.

USAGE
-----
  python calibration.py                      # score everything, diff against the baseline
  python calibration.py --baseline           # score everything, WRITE /tmp/calib_baseline.json
  python calibration.py --grader mod:fn      # calibrate a different grader
  python calibration.py --jobs 8             # parallel (use for render-based graders)
  python calibration.py --max-per-pool 40    # quick smoke run (marks numbers NOT baselineable)

Exit code 0 = every check PASSED.  Non-zero = at least one check FAILED or was MISSING.
"""

import argparse
import ast
import glob
import json
import math
import os
import random
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# ---------------------------------------------------------------------------
# Configuration — thresholds live here so a reviewer can see the bar in one place.
# ---------------------------------------------------------------------------

DEFAULT_GRADER = "geometric_reward:score_deck"
BASELINE_PATH = "/tmp/calib_baseline.json"
AUDIT_DIR = "/home/ubuntu/audit"

# A: human mean must be at least the pooled model mean.  No slack: the whole premise of the
# corpus is that designer-authored decks are the good ones.
THRESH_A_MARGIN = 0.0

# B: Spearman vs the blind designer grades.  +0.4 is a deliberately modest bar — it is the
# level at which the ordering is useful for evaluation, not the level at which it is good.
THRESH_B_SPEARMAN = 0.40

# C: same bar, applied to the size-weighted pooled within-group correlation.
THRESH_C_SPEARMAN = 0.40
MIN_GROUP = 4  # a prompt group needs this many graded rollouts to carry a correlation

# D: if our decks were drawn from the same quality distribution as the human decks, half of
# them would sit above the human median.  More than half means the grader prefers our decks.
THRESH_D_FRAC = 0.50

# Invariant 6 from the project rules: total grading cost under ~2 s/deck including render.
THRESH_SEC_PER_DECK = 2.0

PERM_TRIALS = 2000   # permutation test for the reported correlations
PERM_SEED = 20260728

# (pool key, glob, human?, description)
CORPORA = [
    ("human", "/home/ubuntu/powerbench/PPTArena/GroundTruth/*.pptx", True,
     "PPTArena GroundTruth — designer-authored, GROUND TRUTH, must rank HIGH"),
    ("gallery", "/home/ubuntu/powerbench/agentic/gallery/deck_*.pptx", False,
     "our GRPO model, step 27/30"),
    ("gallery_snapshot", "/home/ubuntu/powerbench/agentic/gallery_snapshot/deck_*.pptx", False,
     "our GRPO model, snapshot (the audited pool)"),
    ("fable_singleturn", "/home/ubuntu/powerbench/agentic/fable_singleturn/deck_*.pptx", False,
     "fable single-turn baseline"),
    ("fable_agentic", "/home/ubuntu/powerbench/agentic/fable_agentic/deck_*.pptx", False,
     "fable agentic baseline"),
]

BAR = "=" * 88


# ---------------------------------------------------------------------------
# Statistics — implemented here because scipy is not installed on the box, and
# because a calibration gate should not have a dependency that can drift.
# ---------------------------------------------------------------------------

def _ranks(vals):
    """Tie-averaged ranks (1-based)."""
    order = sorted(range(len(vals)), key=lambda i: vals[i])
    out = [0.0] * len(vals)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and vals[order[j + 1]] == vals[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            out[order[k]] = avg
        i = j + 1
    return out


def pearson(x, y):
    n = len(x)
    if n < 3:
        return None
    mx = sum(x) / n
    my = sum(y) / n
    num = sum((a - mx) * (b - my) for a, b in zip(x, y))
    den = math.sqrt(sum((a - mx) ** 2 for a in x) * sum((b - my) ** 2 for b in y))
    if den == 0.0:
        return None  # a constant grader has no ordering at all — caller must treat as FAIL
    return num / den


def spearman(x, y):
    if len(x) < 3:
        return None
    return pearson(_ranks(x), _ranks(y))


def kendall_tau_b(x, y):
    """Kendall tau-b (ties handled).  Reported alongside Spearman because with n=45 and a
    handful of tied grades the two can disagree, and a gate should not hide that."""
    n = len(x)
    if n < 3:
        return None
    conc = disc = 0
    for i in range(n):
        for j in range(i + 1, n):
            a = x[i] - x[j]
            b = y[i] - y[j]
            s = a * b
            if s > 0:
                conc += 1
            elif s < 0:
                disc += 1
    n0 = n * (n - 1) / 2.0
    n1 = _tie_term(x)
    n2 = _tie_term(y)
    den = math.sqrt((n0 - n1) * (n0 - n2))
    if den == 0.0:
        return None
    return (conc - disc) / den


def _tie_term(v):
    counts = {}
    for a in v:
        counts[a] = counts.get(a, 0) + 1
    return sum(c * (c - 1) / 2.0 for c in counts.values())


def concordance(x, y):
    """Fraction of comparable pairs the grader orders the same way as the reference.
    This is the number that maps directly onto 'would GRPO get the sign right'."""
    c = d = 0
    n = len(x)
    for i in range(n):
        for j in range(i + 1, n):
            s = (x[i] - x[j]) * (y[i] - y[j])
            if s > 0:
                c += 1
            elif s < 0:
                d += 1
    tot = c + d
    return (c, d, (c / tot) if tot else None)


def perm_p_spearman(x, y, trials=PERM_TRIALS, seed=PERM_SEED):
    """One-sided permutation p-value for 'rho is greater than chance'.  Deterministic seed,
    so the same inputs always give the same p."""
    obs = spearman(x, y)
    if obs is None:
        return None
    rng = random.Random(seed)
    yy = list(y)
    hits = 0
    for _ in range(trials):
        rng.shuffle(yy)
        r = spearman(x, yy)
        if r is not None and r >= obs:
            hits += 1
    return (hits + 1.0) / (trials + 1.0)


def auc_greater(a, b):
    """P(random element of a > random element of b), ties counted as 0.5.
    For check A this is a far more honest statement of separation than a difference of means,
    because it does not care about the shape of either distribution."""
    if not a or not b:
        return None
    wins = 0.0
    for x in a:
        for y in b:
            if x > y:
                wins += 1.0
            elif x == y:
                wins += 0.5
    return wins / (len(a) * len(b))


def mean(v):
    return sum(v) / len(v) if v else None


def median(v):
    if not v:
        return None
    s = sorted(v)
    n = len(s)
    return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])


def stdev(v):
    if len(v) < 2:
        return 0.0
    m = mean(v)
    return math.sqrt(sum((x - m) ** 2 for x in v) / (len(v) - 1))


def fmt(x, nd=4):
    return "n/a" if x is None else ("%+.*f" % (nd, x) if nd == 3 else "%.*f" % (nd, x))


# ---------------------------------------------------------------------------
# Grader loading — LAZY.  Nothing is imported until a deck is actually scored, so this file
# can be imported, --help'd, and reasoned about even when the grader is mid-surgery.
# ---------------------------------------------------------------------------

def import_grader(spec):
    if ":" in spec:
        modname, fname = spec.split(":", 1)
    else:
        modname, fname = spec, "score_deck"
    mod = __import__(modname, fromlist=[fname])
    return getattr(mod, fname)


def safe_score(fn, path):
    """Call the grader and normalise the result.  Never raises: a grader that explodes on a
    deck is a finding to report, not a reason to lose the other 600 numbers."""
    try:
        r = fn(path)
    except Exception as e:  # noqa: BLE001 — deliberate: report, do not crash the gate
        return {"score": 0.0, "valid": False, "reason": "grader raised: %s: %s"
                % (type(e).__name__, e), "raised": True}
    if isinstance(r, dict):
        score = r.get("score", 0.0)
        valid = bool(r.get("valid", True))
        reason = r.get("reason", "")
    else:
        score, valid, reason = r, True, ""
    try:
        score = float(score)
    except (TypeError, ValueError):
        return {"score": 0.0, "valid": False, "reason": "non-numeric score %r" % (score,),
                "raised": True}
    if score != score:  # NaN
        return {"score": 0.0, "valid": False, "reason": "NaN score", "raised": True}
    return {"score": score, "valid": valid, "reason": reason, "raised": False}


_W = {}


def _winit(spec, here):
    if here not in sys.path:
        sys.path.insert(0, here)
    _W["fn"] = import_grader(spec)


def _wscore(path):
    return path, safe_score(_W["fn"], path)


def score_paths(paths, spec, jobs=1):
    """Score a list of decks, returning {path: result}.  Order-independent by construction."""
    paths = list(paths)
    if jobs > 1 and len(paths) > 1:
        import multiprocessing as mp
        with mp.Pool(jobs, initializer=_winit, initargs=(spec, HERE)) as pool:
            return dict(pool.map(_wscore, paths, chunksize=4))
    fn = import_grader(spec)
    return {p: safe_score(fn, p) for p in paths}


# ---------------------------------------------------------------------------
# Reference loading — the blind designer grades.
# ---------------------------------------------------------------------------

class Missing(Exception):
    """A reference we need is absent or unjoinable.  Raised, caught, reported LOUDLY, and
    turned into a MISSING check — never into a silent skip."""


def load_designer_grades():
    """Load /home/ubuntu/audit/my_grades.json and join it to deck paths.

    SCHEMA (reported by the harness so a reader never has to guess):
      my_grades.json : flat object, {"item_NN": float}.  45 keys, item_01..item_45.
                       Value is the designer's blind 0..1 quality grade for that ITEM, given
                       by eye from a rendered PNG, committed BEFORE unblinding.  The file
                       carries NO deck path — the item id is the only join key.
      manifest.json  : list of 45 objects, each with the join key "id" plus "path", "pool"
                       ("human" | "ours" | "fable_st" | "fable_ag"), "nslides", and the v1
                       grader's own "score"/"metrics" as recorded at audit time.
                       manifest.json is what turns an item id into a deck.

    Returns (rows, notes) where each row is {id, path, pool, grade}.
    """
    notes = []
    gp = os.path.join(AUDIT_DIR, "my_grades.json")
    mp_ = os.path.join(AUDIT_DIR, "manifest.json")
    if not os.path.isfile(gp):
        raise Missing("blind designer grades not found at %s" % gp)
    with open(gp) as fh:
        grades = json.load(fh)
    if not isinstance(grades, dict) or not grades:
        raise Missing("%s is not a non-empty {item_id: grade} object" % gp)

    if not os.path.isfile(mp_):
        raise Missing("grades exist (%d items in %s) but the id->path manifest %s is missing, "
                      "so the grades cannot be joined to any deck" % (len(grades), gp, mp_))
    with open(mp_) as fh:
        manifest = json.load(fh)
    by_id = {}
    for e in manifest:
        if isinstance(e, dict) and "id" in e and "path" in e:
            by_id[e["id"]] = e

    rows, unjoined = [], []
    for item in sorted(grades):
        e = by_id.get(item)
        if e is None:
            unjoined.append(item)
            continue
        p = e["path"]
        if not os.path.isfile(p):
            unjoined.append("%s (path gone: %s)" % (item, p))
            continue
        rows.append({"id": item, "path": p, "pool": e.get("pool", "?"),
                     "grade": float(grades[item])})
    if unjoined:
        notes.append("%d graded items could not be joined to a deck on disk: %s"
                     % (len(unjoined), ", ".join(unjoined[:8])))
    if len(rows) < 3:
        raise Missing("only %d of %d graded items joined to decks on disk — nothing to "
                      "correlate" % (len(rows), len(grades)))
    return rows, notes


def load_group_grades():
    """Blind grades for WITHIN-PROMPT rollout groups (check C).

    Two sources, kept SEPARATE on purpose — they are two different blind sessions and their
    absolute scales are not comparable, only their internal ordering is:

      audit-main : manifest.json rows whose deck filename carries a prompt id
                   (deck_<pNNNNN>_n<NN>_...), graded in my_grades.json.
      audit-grp  : grp_manifest.json (list of {prompt, path, id, score}) whose blind grades
                   live in the MINE dict of grpanalyze.py.  Preferred location is
                   grp_grades.json; if that does not exist the MINE literal is read out of
                   grpanalyze.py with ast.literal_eval (parse only — the file is never
                   executed and never modified).

    Returns (groups, notes); groups is a list of
    {source, prompt, items:[{path, grade, id}]} with >= MIN_GROUP items.
    """
    import re
    notes = []
    groups = []
    seen_paths = {}

    # --- source 1: the main audit manifest, grouped by prompt id in the filename ---------
    try:
        rows, sub = load_designer_grades()
        notes.extend(sub)
        buckets = {}
        for r in rows:
            m = re.search(r"deck_(p\d+)_n\d+", os.path.basename(r["path"]))
            if m:
                buckets.setdefault(m.group(1), []).append(r)
        for prompt in sorted(buckets):
            items = sorted(buckets[prompt], key=lambda r: r["path"])
            if len(items) >= MIN_GROUP:
                groups.append({"source": "audit-main", "prompt": prompt, "items": items})
            for it in items:
                seen_paths.setdefault(it["path"], []).append("audit-main")
    except Missing as e:
        notes.append("audit-main group source unavailable: %s" % e)

    # --- source 2: the dedicated within-group audit --------------------------------------
    gm = os.path.join(AUDIT_DIR, "grp_manifest.json")
    grades, src = _load_grp_grade_dict()
    if not os.path.isfile(gm):
        notes.append("audit-grp group source unavailable: %s missing" % gm)
    elif grades is None:
        notes.append("audit-grp group source unavailable: %s exists but no blind grades found "
                     "(looked for grp_grades.json and the MINE dict in grpanalyze.py)" % gm)
    else:
        with open(gm) as fh:
            man = json.load(fh)
        buckets = {}
        for e in man:
            g = grades.get(e.get("id"))
            if g is None or not os.path.isfile(e.get("path", "")):
                continue
            buckets.setdefault(e.get("prompt", "?"), []).append(
                {"id": e["id"], "path": e["path"], "grade": float(g), "pool": "ours"})
        for prompt in sorted(buckets):
            items = sorted(buckets[prompt], key=lambda r: r["path"])
            if len(items) >= MIN_GROUP:
                groups.append({"source": "audit-grp", "prompt": prompt, "items": items})
            for it in items:
                seen_paths.setdefault(it["path"], []).append("audit-grp")
        notes.append("audit-grp blind grades read from %s" % src)

    dupes = [p for p, s in seen_paths.items() if len(set(s)) > 1]
    if dupes:
        notes.append("%d deck(s) appear in BOTH group sources and are graded twice, in two "
                     "separate blind sessions; the sources are kept separate so the two "
                     "grades are never mixed into one correlation: %s"
                     % (len(dupes), ", ".join(os.path.basename(p) for p in dupes[:4])))
    return groups, notes


def _load_grp_grade_dict():
    p = os.path.join(AUDIT_DIR, "grp_grades.json")
    if os.path.isfile(p):
        with open(p) as fh:
            return json.load(fh), p
    src = os.path.join(AUDIT_DIR, "grpanalyze.py")
    if os.path.isfile(src):
        with open(src) as fh:
            tree = ast.parse(fh.read())
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id == "MINE":
                        try:
                            return ast.literal_eval(node.value), src + " (MINE literal)"
                        except (ValueError, SyntaxError):
                            return None, None
    return None, None


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

class Check(object):
    """A single gate.  status is PASS, FAIL or MISSING; MISSING never counts as PASS."""

    def __init__(self, key, title):
        self.key = key
        self.title = title
        self.status = "MISSING"
        self.detail = "not run"

    @property
    def ok(self):
        return self.status == "PASS"

    def set(self, passed, detail):
        self.status = "PASS" if passed else "FAIL"
        self.detail = detail

    def missing(self, detail):
        self.status = "MISSING"
        self.detail = detail


def check_a(pools, out):
    c = Check("A", "HUMAN-VS-MODEL   designer decks must outscore generated decks")
    human = pools.get("human", {}).get("scores", [])
    model_pools = [(k, v) for k, v in pools.items() if not v["is_human"] and v["scores"]]
    if not human or not model_pools:
        c.missing("need both the human corpus and at least one model corpus; human n=%d, "
                  "model pools=%d" % (len(human), len(model_pools)))
        return c

    print(BAR)
    print("CHECK A — HUMAN-VS-MODEL MEAN SCORE")
    print(BAR)
    print("  %-18s %5s %8s %8s %8s %8s %8s %7s" %
          ("pool", "n", "mean", "median", "sd", "min", "max", "invalid"))
    for key, v in [("human", pools["human"])] + model_pools:
        s = v["scores"]
        print("  %-18s %5d %8.4f %8.4f %8.4f %8.4f %8.4f %7d"
              % (key, len(s), mean(s), median(s), stdev(s), min(s), max(s), v["n_invalid"]))

    all_model = [x for _, v in model_pools for x in v["scores"]]
    hm, mm = mean(human), mean(all_model)
    auc = auc_greater(human, all_model)
    print("\n  human mean            %.4f   (n=%d)" % (hm, len(human)))
    print("  model mean (pooled)   %.4f   (n=%d)" % (mm, len(all_model)))
    print("  gap (human - model)   %+.4f" % (hm - mm))
    print("  P(random human deck > random model deck) = %.3f   [0.5 = no separation, "
          "<0.5 = grader prefers OUR decks]" % auc)
    for key, v in model_pools:
        flag = "  <-- OUTSCORES HUMANS" if mean(v["scores"]) > hm else ""
        print("    vs %-18s gap %+.4f   AUC %.3f%s"
              % (key, hm - mean(v["scores"]), auc_greater(human, v["scores"]), flag))

    out["A.human_mean"] = hm
    out["A.model_mean"] = mm
    out["A.gap"] = hm - mm
    out["A.auc_human_gt_model"] = auc
    for key, v in model_pools:
        out["A.mean." + key] = mean(v["scores"])
    out["A.mean.human"] = hm

    c.set(hm - mm >= THRESH_A_MARGIN,
          "human %.4f vs model %.4f (gap %+.4f, need >= %+.2f); AUC %.3f"
          % (hm, mm, hm - mm, THRESH_A_MARGIN, auc))
    return c


def check_b(spec, jobs, out):
    c = Check("B", "DESIGNER RANK    Spearman vs blind designer grades")
    print("\n" + BAR)
    print("CHECK B — RANK CORRELATION vs THE BLIND DESIGNER GRADES")
    print(BAR)
    try:
        rows, notes = load_designer_grades()
    except Missing as e:
        print("\n  !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
        print("  !!  CHECK B CANNOT RUN: %s" % e)
        print("  !!  This is NOT a pass.  The calibration reference is the only thing that")
        print("  !!  can catch an anti-ordered grader; without it the other checks cannot")
        print("  !!  tell a good grader from the one we just threw away.")
        print("  !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
        print("\n  What IS available:")
        for pool, pat, _, _ in CORPORA:
            print("    %-18s %4d decks   %s" % (pool, len(glob.glob(pat)), pat))
        print("    %s exists: %s" % (os.path.join(AUDIT_DIR, "my_grades.json"),
                                     os.path.isfile(os.path.join(AUDIT_DIR, "my_grades.json"))))
        print("    %s exists: %s" % (os.path.join(AUDIT_DIR, "manifest.json"),
                                     os.path.isfile(os.path.join(AUDIT_DIR, "manifest.json"))))
        c.missing(str(e))
        return c

    with open(os.path.join(AUDIT_DIR, "my_grades.json")) as fh:
        raw_grades = json.load(fh)
    with open(os.path.join(AUDIT_DIR, "manifest.json")) as fh:
        raw_manifest = json.load(fh)
    print("\n  SCHEMA (reported so no reader has to guess)")
    print("    my_grades.json : flat object {\"item_NN\": float}, %d keys, %s..%s."
          % (len(raw_grades), min(raw_grades), max(raw_grades)))
    print("                     Value = the designer's BLIND 0..1 quality grade for that item,")
    print("                     given by eye from a rendered PNG and committed before")
    print("                     unblinding.  Range %.2f..%.2f.  The file carries NO deck path"
          % (min(raw_grades.values()), max(raw_grades.values())))
    print("                     — the item id is the only join key.")
    print("    manifest.json  : list of %d objects keyed by the same \"id\", each carrying"
          % len(raw_manifest))
    print("                     %s." % ", ".join('"%s"' % k for k in sorted(raw_manifest[0])))
    print("                     This is what turns an item id into a deck on disk; \"score\"")
    print("                     and \"metrics\" there are the V1 grader's own numbers as")
    print("                     recorded at audit time and are NOT used as a reference here.")
    for n in notes:
        print("    NOTE: %s" % n)

    scored = score_paths([r["path"] for r in rows], spec, jobs)
    g = [scored[r["path"]]["score"] for r in rows]
    d = [r["grade"] for r in rows]

    rho = spearman(g, d)
    tau = kendall_tau_b(g, d)
    conc, disc, frac = concordance(g, d)
    p = perm_p_spearman(g, d)

    print("\n  joined %d graded decks" % len(rows))
    pools_here = {}
    for r in rows:
        pools_here.setdefault(r["pool"], []).append(r)
    print("    by pool: %s" % ", ".join("%s=%d" % (k, len(v)) for k, v in sorted(pools_here.items())))
    print("\n  Spearman(grader, designer) = %s      [PASS needs >= %+.2f]"
          % (fmt(rho, 3), THRESH_B_SPEARMAN))
    print("  Kendall tau-b              = %s" % fmt(tau, 3))
    print("  pairwise concordance       = %s   (%d concordant / %d discordant of %d pairs)"
          % (fmt(frac, 3) if frac is None else "%.3f" % frac, conc, disc, conc + disc))
    print("  permutation p (one-sided)  = %s   [%d shuffles, seed %d]"
          % ("n/a" if p is None else "%.4f" % p, PERM_TRIALS, PERM_SEED))

    # Per-pool correlation: a grader can look fine overall purely because it separates pools.
    for pool in sorted(pools_here):
        sub = pools_here[pool]
        if len(sub) >= 3:
            r2 = spearman([scored[x["path"]]["score"] for x in sub], [x["grade"] for x in sub])
            print("    within pool %-10s n=%2d  spearman %s" % (pool, len(sub), fmt(r2, 3)))

    # The tails are where the v1 grader's failure was visible to the eye.
    k = max(3, len(rows) // 5)
    order = sorted(range(len(rows)), key=lambda i: g[i])
    lo, hi = order[:k], order[-k:]
    print("\n  grader's %d LOWEST-scored: grader mean %.3f  ->  designer mean %.3f" %
          (k, mean([g[i] for i in lo]), mean([d[i] for i in lo])))
    print("  grader's %d HIGHEST-scored: grader mean %.3f  ->  designer mean %.3f" %
          (k, mean([g[i] for i in hi]), mean([d[i] for i in hi])))
    print("  (if the HIGHEST group's designer mean is not above the LOWEST group's, the "
          "grader is inverted)")
    print("\n  worst disagreements (grader rank minus designer rank):")
    gr, dr = _ranks(g), _ranks(d)
    worst = sorted(range(len(rows)), key=lambda i: -abs(gr[i] - dr[i]))[:6]
    print("    %-9s %-8s %-9s %-8s %-9s %s" %
          ("item", "grader", "g-rank", "designer", "d-rank", "deck"))
    for i in worst:
        print("    %-9s %-8.4f %-9.1f %-8.2f %-9.1f %s"
              % (rows[i]["id"], g[i], gr[i], d[i], dr[i], os.path.basename(rows[i]["path"])))

    out["B.n"] = len(rows)
    out["B.spearman"] = rho
    out["B.kendall"] = tau
    out["B.concordance"] = frac
    out["B.perm_p"] = p

    if rho is None:
        c.set(False, "correlation undefined — the grader returned a CONSTANT score across all "
                     "%d graded decks (zero ordering, and zero GRPO gradient)" % len(rows))
    else:
        c.set(rho >= THRESH_B_SPEARMAN,
              "spearman %+.3f vs designer (need >= %+.2f), n=%d, tau %s, p=%s"
              % (rho, THRESH_B_SPEARMAN, len(rows), fmt(tau, 3),
                 "n/a" if p is None else "%.4f" % p))
    return c


def check_c(spec, jobs, out):
    c = Check("C", "WITHIN-GROUP     ordering inside same-prompt rollout groups")
    print("\n" + BAR)
    print("CHECK C — WITHIN-GROUP ORDERING (this is what GRPO actually consumes)")
    print(BAR)
    groups, notes = load_group_grades()
    for n in notes:
        print("  NOTE: %s" % n)
    groups = [g for g in groups if len(g["items"]) >= MIN_GROUP]
    if not groups:
        print("\n  !!  NO prompt group has >= %d blind-graded rollouts.  Check C cannot run." % MIN_GROUP)
        print("  !!  This is NOT a pass.  To enable it, blind-grade >= %d rollouts of one" % MIN_GROUP)
        print("  !!  prompt and record them as {item_id: grade}.")
        c.missing("no prompt group has >= %d blind-graded rollouts" % MIN_GROUP)
        return c

    allpaths = [it["path"] for g in groups for it in g["items"]]
    scored = score_paths(allpaths, spec, jobs)

    print("\n  %-12s %-8s %4s %10s %10s %s" %
          ("source", "prompt", "n", "spearman", "tau-b", "pairwise concordance"))
    rhos, weights, tc, td = [], [], 0, 0
    for g in groups:
        gs = [scored[it["path"]]["score"] for it in g["items"]]
        ds = [it["grade"] for it in g["items"]]
        r = spearman(gs, ds)
        t = kendall_tau_b(gs, ds)
        cc, dd, fr = concordance(gs, ds)
        tc += cc
        td += dd
        if r is not None:
            rhos.append(r)
            weights.append(len(g["items"]))
        print("  %-12s %-8s %4d %10s %10s  %d/%d = %s"
              % (g["source"], g["prompt"], len(g["items"]), fmt(r, 3), fmt(t, 3), cc, cc + dd,
                 "n/a" if fr is None else "%.2f" % fr))

    pooled = (sum(r * w for r, w in zip(rhos, weights)) / sum(weights)) if rhos else None
    pconc = (tc / (tc + td)) if (tc + td) else None
    print("\n  size-weighted mean within-group spearman = %s   [PASS needs >= %+.2f]"
          % (fmt(pooled, 3), THRESH_C_SPEARMAN))
    print("  pooled within-group pairwise concordance = %s   (%d concordant / %d discordant)"
          % ("n/a" if pconc is None else "%.3f" % pconc, tc, td))
    print("  -> GRPO would get the sign of a within-group comparison right %s of the time"
          % ("n/a" if pconc is None else "%.0f%%" % (100 * pconc)))
    print("\n  Within-group ordering can be HEALTHY while check B is broken (v1: C +0.68,")
    print("  B -0.14).  That combination means the training signal is usable but the")
    print("  EVALUATION metric is invalid — do not read a green C as a green grader.")

    out["C.n_groups"] = len(groups)
    out["C.n_decks"] = len(allpaths)
    out["C.spearman_pooled"] = pooled
    out["C.concordance"] = pconc

    if pooled is None:
        c.set(False, "no group produced a defined correlation (constant grader within groups "
                     "= zero GRPO gradient)")
    else:
        c.set(pooled >= THRESH_C_SPEARMAN,
              "size-weighted within-group spearman %+.3f over %d group(s)/%d decks "
              "(need >= %+.2f), concordance %s"
              % (pooled, len(groups), len(allpaths), THRESH_C_SPEARMAN,
                 "n/a" if pconc is None else "%.2f" % pconc))
    return c


def check_d(pools, out):
    c = Check("D", "SEPARATION       our decks scoring above the human median")
    human = pools.get("human", {}).get("scores", [])
    model_pools = [(k, v) for k, v in pools.items() if not v["is_human"] and v["scores"]]
    if not human or not model_pools:
        c.missing("need both the human corpus and at least one model corpus")
        return c
    hmed = median(human)
    print("\n" + BAR)
    print("CHECK D — SEPARATION: HOW MANY OF OUR DECKS BEAT THE MEDIAN HUMAN DECK")
    print(BAR)
    print("  human median = %.4f   (n=%d).  A grader that cannot tell our decks from a"
          % (hmed, len(human)))
    print("  designer's puts ~50% of them above this line; the v1 grader put nearly all.")
    print("\n  %-18s %5s %10s %9s" % ("pool", "n", "above med", "fraction"))
    tot = above = 0
    for key, v in model_pools:
        s = v["scores"]
        a = sum(1 for x in s if x > hmed)
        tot += len(s)
        above += a
        print("  %-18s %5d %10d %9.3f" % (key, len(s), a, a / len(s)))
        out["D.frac_above." + key] = a / len(s)
    frac = above / tot
    print("  %-18s %5d %10d %9.3f   [PASS needs <= %.2f]" %
          ("ALL MODEL DECKS", tot, above, frac, THRESH_D_FRAC))
    # Also the reverse view: how many human decks fall below the model median.
    all_model = [x for _, v in model_pools for x in v["scores"]]
    mmed = median(all_model)
    hbelow = sum(1 for x in human if x < mmed)
    print("\n  reverse view: %d/%d (%.3f) human decks fall BELOW the model median (%.4f)"
          % (hbelow, len(human), hbelow / len(human), mmed))

    out["D.human_median"] = hmed
    out["D.n_model"] = tot
    out["D.n_above_human_median"] = above
    out["D.frac_above_human_median"] = frac
    out["D.frac_human_below_model_median"] = hbelow / len(human)

    c.set(frac <= THRESH_D_FRAC,
          "%d/%d (%.1f%%) of our decks beat the human median %.4f (need <= %.0f%%)"
          % (above, tot, 100 * frac, hmed, 100 * THRESH_D_FRAC))
    return c


def check_integrity(pools, spec, jobs, out, sec_per_deck):
    """R — the harness's own preconditions.  If these fail, every number above is void.
    Covers project invariants 1 (never negative), 3 (deterministic) and 6 (< 2 s/deck)."""
    c = Check("R", "INTEGRITY        deterministic, in-range, never negative, fast enough")
    print("\n" + BAR)
    print("CHECK R — GRADER INTEGRITY (invariants 1, 3, 6)")
    print(BAR)
    problems = []

    allmap = {}
    for v in pools.values():
        allmap.update(v["results"])
    allres = sorted(allmap.items())
    neg = [(p, r["score"]) for p, r in allres if r["score"] < 0.0]
    hi = [(p, r["score"]) for p, r in allres if r["score"] > 1.0]
    raised = [(p, r["reason"]) for p, r in allres if r.get("raised")]
    invalid = [(p, r["reason"]) for p, r in allres if not r["valid"] and not r.get("raised")]

    print("  scored %d decks in %.1fs  =>  %.3f s/deck   [invariant 6: < %.1f s/deck]"
          % (len(allres), sec_per_deck * len(allres), sec_per_deck, THRESH_SEC_PER_DECK))
    print("  negative scores : %d   [invariant 1: a negative floor zeroes the GRPO gradient]" % len(neg))
    print("  scores > 1.0    : %d" % len(hi))
    print("  grader raised   : %d" % len(raised))
    print("  invalid decks   : %d" % len(invalid))
    for p, why in raised[:5]:
        print("      RAISED %s: %s" % (os.path.basename(p), why))
    for p, why in invalid[:5]:
        print("      invalid %s: %s" % (os.path.basename(p), why))

    if neg:
        problems.append("%d NEGATIVE scores (invariant 1 violated)" % len(neg))
    if hi:
        problems.append("%d scores > 1.0" % len(hi))
    if raised:
        problems.append("%d decks made the grader raise" % len(raised))
    if sec_per_deck > THRESH_SEC_PER_DECK:
        problems.append("%.3f s/deck exceeds the %.1f s budget" % (sec_per_deck, THRESH_SEC_PER_DECK))

    # Determinism: re-score a fixed, deterministic sample and demand identical numbers.
    sample = [p for p, _ in allres][::max(1, len(allres) // 12)][:12]
    again = score_paths(sample, spec, jobs)
    drift = [(p, allmap[p]["score"], again[p]["score"])
             for p in sample if again[p]["score"] != allmap[p]["score"]]
    print("  determinism     : re-scored %d decks, %d differed   [invariant 3]"
          % (len(sample), len(drift)))
    for p, a, b in drift[:5]:
        print("      DRIFT %s: %.6f -> %.6f" % (os.path.basename(p), a, b))
    if drift:
        problems.append("%d decks scored differently on a re-run (invariant 3 violated)" % len(drift))

    out["R.sec_per_deck"] = sec_per_deck
    out["R.n_negative"] = len(neg)
    out["R.n_raised"] = len(raised)
    out["R.n_invalid"] = len(invalid)
    out["R.n_nondeterministic"] = len(drift)

    c.set(not problems, "; ".join(problems) if problems
          else "%d decks, %.3f s/deck, no negatives, no drift" % (len(allres), sec_per_deck))
    return c


# ---------------------------------------------------------------------------
# Baseline
# ---------------------------------------------------------------------------

def write_baseline(path, payload):
    with open(path, "w") as fh:
        json.dump(payload, fh, indent=1, sort_keys=True)
    print("\nbaseline WRITTEN to %s — every later fix is measured against these numbers." % path)


def diff_baseline(path, payload):
    print("\n" + BAR)
    print("DIFF vs BASELINE  %s" % path)
    print(BAR)
    if not os.path.isfile(path):
        print("  no baseline on disk yet.  Run:  python calibration.py --baseline")
        return
    with open(path) as fh:
        base = json.load(fh)
    if base.get("meta", {}).get("grader") != payload["meta"]["grader"]:
        print("  NOTE: baseline was taken with grader %r, this run used %r."
              % (base.get("meta", {}).get("grader"), payload["meta"]["grader"]))
    if base.get("meta", {}).get("sampled") or payload["meta"].get("sampled"):
        print("  NOTE: one side used --max-per-pool sampling; these numbers are NOT comparable.")
    print("  taken %s" % base.get("meta", {}).get("when", "?"))
    bn, nn = base.get("numbers", {}), payload["numbers"]
    keys = sorted(set(bn) | set(nn))
    print("\n  %-38s %12s %12s %12s" % ("metric", "baseline", "now", "delta"))
    for k in keys:
        a, b = bn.get(k), nn.get(k)
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            print("  %-38s %12.4f %12.4f %+12.4f" % (k, a, b, b - a))
        else:
            cell = lambda x: "%12.4f" % x if isinstance(x, (int, float)) else "%12s" % "-"
            print("  %-38s %s %s %12s" % (k, cell(a), cell(b), "-"))
    print("\n  %-38s %12s %12s" % ("check", "baseline", "now"))
    bc, nc = base.get("checks", {}), payload["checks"]
    for k in sorted(set(bc) | set(nc)):
        a, b = bc.get(k, "-"), nc.get(k, "-")
        mark = "" if a == b else ("   <-- IMPROVED" if b == "PASS" else "   <-- REGRESSED")
        print("  %-38s %12s %12s%s" % (k, a, b, mark))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--grader", default=DEFAULT_GRADER,
                    help="module:function, imported lazily (default %s)" % DEFAULT_GRADER)
    ap.add_argument("--baseline", action="store_true",
                    help="write the current numbers to the baseline file instead of diffing")
    ap.add_argument("--baseline-path", default=BASELINE_PATH)
    ap.add_argument("--jobs", type=int, default=1,
                    help="parallel scoring workers (default 1; raise for render-based graders, "
                         "but keep it well under the core count so soffice does not storm)")
    ap.add_argument("--max-per-pool", type=int, default=0,
                    help="score at most N decks per corpus (smoke runs only; the run is then "
                         "marked as sampled and must not be baselined against a full run)")
    args = ap.parse_args(argv)

    print(BAR)
    print("POWERBENCH GRADER CALIBRATION HARNESS")
    print(BAR)
    print("grader : %s   (imported lazily)" % args.grader)
    print("when   : %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    if args.max_per_pool:
        print("SAMPLED: at most %d decks per pool — these numbers are a smoke test, not a "
              "baseline." % args.max_per_pool)

    # ---- score every corpus ------------------------------------------------
    pools = {}
    t0 = time.time()
    n_total = 0
    print("\ncorpora")
    for key, pat, is_human, desc in CORPORA:
        paths = sorted(glob.glob(pat))
        if args.max_per_pool:
            paths = paths[:args.max_per_pool]
        if not paths:
            print("  %-18s   0 decks  MISSING  %s" % (key, pat))
            pools[key] = {"scores": [], "results": {}, "is_human": is_human,
                          "n_invalid": 0, "desc": desc}
            continue
        res = score_paths(paths, args.grader, args.jobs)
        scores = [res[p]["score"] for p in paths]
        pools[key] = {"scores": scores, "results": res, "is_human": is_human,
                      "n_invalid": sum(1 for p in paths if not res[p]["valid"]),
                      "desc": desc}
        n_total += len(paths)
        print("  %-18s %3d decks   %s" % (key, len(paths), desc))
    elapsed = time.time() - t0
    sec_per_deck = elapsed / max(n_total, 1)
    print("\nscored %d decks in %.1fs (%.3f s/deck)" % (n_total, elapsed, sec_per_deck))

    numbers = {}
    checks = [check_a(pools, numbers),
              check_b(args.grader, args.jobs, numbers),
              check_c(args.grader, args.jobs, numbers),
              check_d(pools, numbers),
              check_integrity(pools, args.grader, args.jobs, numbers, sec_per_deck)]

    # ---- verdict -----------------------------------------------------------
    print("\n" + BAR)
    print("VERDICT")
    print(BAR)
    for c in checks:
        print("  [%-7s] %s" % (c.status, c.title))
        print("            %s" % c.detail)
    n_pass = sum(1 for c in checks if c.ok)
    n_missing = sum(1 for c in checks if c.status == "MISSING")
    overall = all(c.ok for c in checks)
    print("\n  %d/%d checks PASS, %d MISSING" % (n_pass, len(checks), n_missing))
    if n_missing:
        print("  A MISSING check is counted as a FAIL. A gate that cannot run has not passed.")
    print("\n  OVERALL: %s" % ("PASS — this grader is calibrated against a designer's eye"
                               if overall else
                               "FAIL — do NOT report this grader's numbers as an evaluation"))
    print(BAR)

    payload = {"meta": {"grader": args.grader,
                        "when": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "sampled": bool(args.max_per_pool),
                        "n_decks": n_total,
                        "pool_sizes": {k: len(v["scores"]) for k, v in pools.items()}},
               "numbers": numbers,
               "checks": {c.key + " " + c.title.split()[0]: c.status for c in checks},
               "overall": "PASS" if overall else "FAIL"}
    if args.baseline:
        write_baseline(args.baseline_path, payload)
    else:
        diff_baseline(args.baseline_path, payload)

    return 0 if overall else 1


if __name__ == "__main__":
    sys.exit(main())
