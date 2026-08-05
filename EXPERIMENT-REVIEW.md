# Is the experiment as good as it can be? — a whole-experiment review

*2026-07-31. Written after days spent hardening the reward, stepping back to ask the bigger
question: will this run produce a result anyone can trust?*

## Verdict

**The reward is now as good as it can be for what it measures. The experiment is not — and the
gap is not in the reward. It is in the EVALUATION, the SCOPE of the claim, and the deferred
validity controls.** We have polished the training signal to a high shine while the thing that
decides whether the result is believable — the eval — has barely been touched.

## What is genuinely good (don't throw this away)

- **The ops path is battle-tested.** Every failure that cost us days — OOM, vLLM wake-OOM,
  zombie vLLM, GDN compile stalls, the FSDP2 checkpoint crash, prefix-caching deadlock, box
  death — is diagnosed and fixed. That infrastructure is real and hard-won.
- **The method is field-validated.** AeSlides (arXiv 2604.22840) ran GRPO on a ~30B open model
  with deterministic *layout* rewards from the rendered slide and it WORKED: aspect 36→85%,
  collisions −31%, imbalance −28%, render errors →0%, and it beat Claude-Sonnet-4.5 on human
  layout quality. That is almost exactly our setup. So the approach is not speculative.
- **Single-turn was the right call** — multi-turn + thinking + GRPO is the most collapse-prone
  config documented, and we have the dead runs (grpo5–8) to prove it.
- **The reward, post-hardening, measures FORM well** — content_score rho +0.626 vs blind
  designer grades, gates against every known degeneracy.

## The core problem: the experiment grades itself with the same proxy it trains on

The staged run trains on the geometric reward and **evaluates on the geometric reward** (val
uses the same `compute_score`). We already proved that proxy is **~0.00 correlated with human
judgment on our own model's decks** (blind study: grader rho +0.21 overall, ~0.00 within our
decks; AI judges +0.63). So a rising held-out curve **cannot distinguish learning from
reward-hacking** — the one thing the whole exercise needs it to do. Our single cleanest piece
of "real learning" evidence (geo2: 0.579→0.666 with response length halving) is itself measured
on the proxy; the length-drop is reassuring but it is not an independent quality signal.

This is the deepest issue and it is invisible if you only look at the reward: **you can make the
reward perfect and still learn nothing, and the circular eval will happily show a rising line.**

## What we deferred and never did (validity controls a pitch needs)

- **The real task's grader was never built.** Everything trained so far uses a SlidesBench-
  derived geometric reward as a STAND-IN for the powerbench Goldilocks 8-criteria conformance
  grader (TODO 2026-07-21: "powerbench grader NOT done yet → verifying pipeline with SlidesBench
  as stand-in"). We are training on a proxy of the real objective.
- **The k=8 baseline and the "1–3/8 Goldilocks band" go/no-go gate were never run** (blocked on
  API key). We do not actually know the task sits in the intended difficulty band on the real
  grader.
- **The memorization / contamination probe was planned and never executed.** SlidesBench is
  public and predates the model. Without the perturbed-vs-official check, "improvement" could be
  recall, and that is the first thing a skeptic at the pitch will ask.
- **The held-out eval is under-powered.** 45 tasks × n=1 → SE ≈ 0.05–0.07; the expected 2–3%
  effect sits inside the noise (flagged 2026-07-21, never fixed).

## What the failure history actually tells us

Sorting every run we have had: the failures were **~90% ops** (OOM/vLLM/box) and **~10%
reward-hacking** (length runaway grpo5–7, collapse grpo8, empty/decoration hacks). The training
METHOD itself has produced exactly **one** clean hill-climb (geo2). Everything else either died
in ops or hacked the reward. So we have strong evidence the machinery runs and the reward is
hard to game — and thin evidence that the method moves the true objective, because we never
measured the true objective independently.

## Recommendations, in order of leverage (none is another reward patch)

1. **Add an independent eval — highest leverage, cheap, already built.** Score the baseline and
   trained checkpoints' held-out decks with the cross-vendor VLM-judge panel (we measured +0.63
   to humans; harness = `analyze_agents.py` + the agent-dispatch pattern). Report the run's
   success as *judge-scored quality before vs after*, not the training reward. If judge-quality
   rises while training on the geometric reward → a credible, hack-proof result. If it doesn't →
   we caught reward-hacking that the circular eval would have hidden. This is the single change
   that makes the Aug 8 number believable.
2. **Scope the claim to LAYOUT/FORM, exactly like AeSlides.** Claim "GRPO on a deterministic
   layout reward measurably improves layout quality (collisions, overflow, balance, aspect) on a
   27B open model, validated by an independent judge." That is honest (it is what the reward
   teaches), it is publishable (AeSlides beat Sonnet on precisely this), and it removes the
   attack surface of overclaiming content quality — which the blind study shows we cannot back.
3. **Close the deferred validity controls before the pitch:** the memorization/perturbed probe
   (cheap, pitch-critical), and either run the real Goldilocks grader / k=8 baseline or state
   plainly that this is a SlidesBench proof-of-life, not the powerbench result.
4. **Fix eval power:** n≥4 samples/task (or a larger eval set) so a real effect clears the noise.

## Bottom line

We spent the reward budget well, but we spent it on the part that was never going to decide the
outcome. The reward decides *what the model learns*; the **eval decides whether we can believe
we learned it**, and right now the eval is the same proxy we already know disagrees with humans.
The experiment becomes "as good as it can be" the moment the success metric is independent of the
training reward and the claim is scoped to what the reward actually teaches — both of which we
can do with tools already in hand, without changing the training run at all.
