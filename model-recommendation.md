# Which open model do we train? — decision memo

**Faisal · updated July 16, 2026 · for ratification at the July 17 checkpoint**

## The decision (July 16)

- **We train Qwen3.6-27B.** It reports near-flagship agent scores, meets our memory requirement, and fits on a single GPU — so training attempts are ~10× cheaper and one 8-GPU machine can generate 8 attempts in parallel. The alternative (Qwen3-Coder-480B) would need **~16 GPUs — two full machines — just to train**, for a capability edge we can't even use if it saturates our checks.
- **We still evaluate both.** The 480B becomes our **measuring stick**: same benchmark, same tasks, no training. Its scores give us the comparison column for the report and tell us honestly how much capability we gave up by training small.
- **Safety net:** if the 27B can't be trained or fails everything, we switch to **Kimi K2.7-Code trained by Fireworks** — their machines, our credits, our grader as the scoring function. GPU count stops being our problem.
- **The main risk just shrank.** The open question was whether the free training software (verl) supports the 27B's new architecture. Evidence now says likely yes: verl lists the Qwen3.5 architecture family (same design), and NVIDIA's training bridge lists Qwen 3.5/3.6 by name. I'll confirm hands-on with a tiny training run before we commit — that's the first task after ratification.

## Quick glossary — terms used below, in plain words

- **Context window** — how much text the model can hold in its head at once, in tokens (a token ≈ ¾ of a word). Our agent re-reads files and re-checks its deck 14–21 times per task, so conversations get huge. Michael set the floor at 256k tokens.
- **Dense vs. "MoE"** — a dense model uses all its parameters on every word. An MoE ("mixture of experts") is like a big team of specialists where only a few work on each word — you need GPUs to hold all of it, but it runs at the speed of the small active part.
- **GRPO** — our training method. The model attempts a task several times; our grader scores each attempt; the model is nudged toward what scored higher. It needs many attempts, so **cost per attempt is the whole game** — this is why the 27B decision makes sense.
- **verl** — free, open-source software that runs GRPO training.
- **Fireworks RFT** — a paid service where Fireworks runs the RL training on their hardware. You give them an agent task plus a scoring function that returns 0 to 1 — our grader already is that scoring function.

## The lineup

| Model | Role | Size | Context | Agent evidence | License |
|---|---|---|---|---|---|
| **Qwen3.6-27B** | **TRAIN** | 27B dense — 1 GPU | 256k native, ~1M | 77.2 SWE-bench Verified · 59.3 Terminal-Bench 2.0 (maker-reported) | Apache 2.0 |
| **Qwen3-Coder-480B** | **EVAL ONLY** (measuring stick) | 480B MoE — 8 GPUs to serve, ~16 to train | 256k native, ~1M | Best-established open agentic coder; ~Claude Sonnet 4 level | Apache 2.0 |
| **Kimi K2.7-Code** | **FALLBACK** (via Fireworks) | 1T MoE — not self-hostable | 256k | Strongest open agent scores (K2.6: 80.2 SWE-bench Verified) | MIT-with-a-condition (moot at our size) |
| MiniMax-M2 | benched | 230B MoE | only proven at 128k | 69.4 SWE-bench Verified | fine for M2; M2.7 is non-commercial ⚠ |
| GLM-4.6 | eliminated | 357B | 200k — below our floor | — | MIT |

## Why train the 27B — the reasoning in full

1. **GRPO rewards whoever can afford the most attempts.** Each training step needs a batch of complete rollouts (multi-step agent runs). On the 27B, one 8-GPU machine runs 8 model copies generating rollouts in parallel. On the 480B, the same machine runs *one* copy — and training needs a second machine on top (~16 GPUs total). Same budget, roughly 10× more learning per dollar on the small model.
2. **Its reported scores justify the shot.** 77.2 SWE-bench Verified / 59.3 Terminal-Bench 2.0 — within a few points of the 1-trillion-parameter Kimi, and nominally *above* the 480B. These are maker-reported, so we verify on our own tasks before trusting them (see the eval plan).
3. **It can see.** The pptx skill's quality loop is render → *look at the slide* → fix. The 27B accepts image inputs, so it can run that loop the way Claude does. The 480B is text-only and would run it blind. For a slide-making task specifically, this may matter more than raw size.
4. **It meets the memory bar the same way the 480B does** (256k native, ~1M stretch), and its new attention design makes very long conversations cheaper — useful when every rollout is a 100k+-token transcript.
5. **Same clean Apache 2.0 license** — zero asterisks in a commercial pitch.
6. **A stronger demo story if it works.** "We took a 27B open model and taught it enterprise template-conformance" is a better product argument than "we nudged a 480B" — it shows the environment produces signal, which is the whole point of Phase 2.

**Two gates before we're committed (both resolve by July 18):**
- **Gate 1 — trainability:** run a tiny verl training step on the 27B. Evidence says it'll pass (architecture family is listed as supported); if it fails, we don't fight it — fallback fires.
- **Gate 2 — band entry:** in the powerbench smoke test (2–3 of Michael's tasks × k=2), the 27B must land in the 1–3-failures band. If it fails everything, it's too weak and the fallback fires.

## Why the 480B stays in — as the measuring stick

- **The comparison column.** The baseline report is much stronger with a second open model: "the failure pattern holds across a 27B and a 480B" beats "we tested one open model."
- **It prices the trade-off.** If the 480B massively outscores the 27B on our tasks, we know what we gave up and can revisit. If it doesn't, the training decision is vindicated before training even starts.
- **Serving it for evals is cheap** — one 8-GPU machine, inference only, no training cluster. We were standing up this endpoint anyway.

## The fallback: Kimi K2.7-Code via Fireworks RFT

**Fires if:** the 27B fails Gate 1 (can't be trained) or Gate 2 (fails everything on the smoke test) — switch within 24 hours, no re-debating.
**Why it's the right fallback now:** with self-hosted training ruled out for giant models, the fallback should be one where GPU count is someone else's problem. Fireworks lists Kimi K2 by name as trainable, their default method is GRPO, and their required input — an agent task plus a 0-to-1 scorer — is exactly what our grader produces. Three questions to ask them before we'd commit: training style at that scale, max conversation length, and price (their free tier only covers small models).

## The eval we run on both models: SlidesBench, with honesty checks

**What:** a public benchmark of "generate this slide as code" tasks — 585 test tasks, scored by a program (not an AI judge), so results can't be argued with and the setup mirrors our own grader.

**Why this one:** it's in-domain (slide generation!), it's fair to the text-only 480B (no images in the input), it's small enough to run in a day on both endpoints, and it doubles as the *starting score* for our hill-climb — so this run is a required step, not a detour.

**The contamination problem, handled:** SlidesBench has been public since January 2025 — both models could have seen it in training (the 27B had 15 months of exposure, the 480B six). We can't know, so we design around it:
1. **Memorization probe** (~30 min): feed each model half-finished test instructions; verbatim completions or absurdly-above-published-record scores = contaminated.
2. **Perturbed test set** (~half a day): ~50 test tasks with programmatically changed targets (colors, titles, positions). Same scorer, but memorization is useless. This is our real eval.
3. **Report both numbers** — official test set (with the caveat disclosed) and perturbed set. If they agree, solid; if they diverge, the perturbed number is the truth. Same "auditable, hack-resistant" register as the rest of our pitch.

**Reading the result:** 480B clearly ahead → we know the capability price we paid, proceed anyway (training economics still win). 27B matches or beats it → decision fully vindicated. Both execute well but score mediocre → plenty of hill-climb headroom, which is exactly what the proof-of-life needs.

## Soft spots (known, not hidden)

- The 27B's scores are maker-reported until our own runs confirm them — Gates 1–2 exist for this reason.
- verl support is "likely, per documentation" until the hands-on test passes.
- Whether K2.7 kept K2.6's 256k window — verify on its model page before any fallback switch.
- Exact AWS pricing — real quotes go in the approval request.

## What I need

- **Michael:** GitHub invite to `powerbench` (my account 404s), bot access to #pptx-benchmarks, and ratify this at the checkpoint.
- **July 17 serving plan:** 27B endpoint (1 GPU) + 480B FP8 endpoint (one 8×H200 machine, inference only) — both OpenAI-compatible for the harness.

---
*Sources: Qwen3.6-27B model card + Qwen blog + vLLM recipe · Qwen3-Coder model card + config.json · Kimi-K2.6/K2.7-Code cards + license + launch coverage · MiniMax-M2 card + license · GLM-4.6 card · verl README/PyPI + release notes + GRPO docs · NVIDIA Megatron Bridge (Qwen 3.5/3.6) · Fireworks RFT blog + docs · AutoPresent/SlidesBench (arXiv 2501.00912) · UniPPTBench (arXiv 2605.17356) · Anthropic's skills repo (pptx skill, measured locally). Research: 24 sources fetched + follow-ups; claims adversarially verified where possible; maker-reported numbers labeled as such.*
