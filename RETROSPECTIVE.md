# Retrospective — what went wrong in my process, and the fixes

Written 2026-07-28, after ~6 training runs and three rounds of human hand-auditing.

## The errors, and what actually caught each one

| # | error | consequence | how it was caught |
|---|---|---|---|
| 1 | Shipped a text-coverage reward that was 100% presence-based | model learned to pad; ~4 wasted runs | adversarial fixture (built *after* the failure) |
| 2 | Set invalid output to **−0.5** | identical scores across a group → zero relative advantage → zero gradient → collapse | reading the GRPO advantage formula, post-mortem |
| 3 | Killed geo5b/geo6 as "hangs" | ~2 wasted hours | they were in a legitimate 15–25 min Triton/GDN compile |
| 4 | Declared held-out "flat" at step 6 and blamed the eval | nearly stopped a working run | the climb arrived at step 9 |
| 5 | Declared checkpointing "not viable" after 4 memory attempts | hours lost; nearly shipped a run with no weights | comparing two functions in the same file — it was a 1-line missing guard |
| 6 | Reported picfit + alignment as "wired in" | 15% of reward weight silently renormalised away; they never affected a score | grepping for call sites |
| 7 | Reported blank-table-rows as "folded into density" | never called at all | same |
| 8 | Attributed a 37-slide score drop to picfit | wrong causal story in the write-up | re-running the comparison — it was the `aspect` removal |
| 9 | Two wrong hypotheses for the step-18 drop (verbosity, then truncation) | two wasted analyses | **executing the failing rollouts** — it was hallucinated python-pptx APIs |
| 10 | Added `aspect` without checking whether it varies | 6% of reward with zero gradient | measuring sd across rollouts |
| 11 | Let local and box copies of the grader diverge | one run trained on a **degraded reward** (density silently 0.0) | comparing the deployed file to the local one |

## The single pattern

**Every error came from trusting my mental model instead of instrumenting the system. Every one
was eventually caught by executing something** — running the failing code, grepping for call
sites, measuring variance, diffing two files.

"Be more careful" does not fix this. Executable checks do.

## Process changes (adopted)

**1. `grader_tests.py` — invariant suite, run before every training launch.**
Each test exists because a specific bug got past me:
- **T1 wiring** — every `WEIGHTS` key must actually produce a metric (catches #6, #7)
- **T2 variance** — every weighted metric must vary across real rollouts (catches #10)
- **T3 adversarial** — padded < good, empty < good, invalid == 0, nothing negative (catches #1, #2)
- **T4 regressions** — every deck a human flagged by eye stays in its expected score band (catches recurrence of #6, and any future silent regression)
- **T5 sanity** — finite, in-range, deterministic, fast enough for 64 rollouts/step

**2. Hand-audit findings become permanent fixtures.** The 8 flagged decks are pinned in
`agentic/audit_decks/` and mirrored locally. Human review is the only thing that caught
several of these bugs; pinning the cases converts a one-off review into a permanent test.

**3. A skipped test must never report PASS.** The first version of T4 printed `[PASS]` for
fixtures it could not find — the same false-assurance failure the suite exists to remove.
Missing fixture now reports loudly.

**4. Execute before hypothesising.** For any "why did X regress", run the failing artefacts
and read the actual errors before proposing a cause. Two wrong step-18 hypotheses came from
reasoning about logs instead of running code; running 10 rollouts settled it in one pass.

**5. Diagnose before rearranging.** Four memory reconfigurations could not fix a device-placement
bug. If ≥2 attempts of the same *kind* fail, the class of fix is probably wrong — go read the
code path instead.

**6. Calibrate patience to the actual phase.** GDN/Triton compile is 15–25 min of silence with
GPUs pinned; that is indistinguishable from a hang without knowing the phase. Freeze thresholds
must exceed the longest legitimate silent phase.

**7. One source of truth.** The box is authoritative. Always pull before editing, push after,
and verify the deployed file — never edit a local copy and assume it is live.

**8. Check variance before adding any metric; distinguish two kinds of constant.**
- constant because *uninformative* → drop it (it only dilutes)
- constant because *satisfied* → keep it cheap as a regression guard (Faisal's correction on
  `aspect`: it is flat only because the prompt fixed it, and it is the only thing that would
  catch a fresh model drifting back to 4:3)

## What I would do differently from the start

1. Write the adversarial gate **before** the first reward, not after the first failure.
2. Never use a negative reward floor in a group-relative algorithm.
3. Build `grader_tests.py` on day one — most of the wasted time was silent grader bugs, not
   training problems.
4. Fix eval power (45 tasks x 1 sample, SE ≈ 0.045) before running experiments whose target
   effect is smaller than the measurement error. I flagged this early and then ignored it for
   four runs.
