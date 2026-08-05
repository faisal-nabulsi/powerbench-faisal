# Faisal — powerbench sprint task list

> **Note:** blind-study item IDs are redacted in the public copy until all raters
> have finished grading. The unblinding analysis lives in the gitignored
> `DO-NOT-SHARE-faisal-vs-grader.md`.
*(compiled 2026-07-15 from the Slack recaps + the Goldilocks workplan PDF in Drive; workplan is the authoritative source for dates)*

## 🔄 STRATEGY PIVOT 2026-07-21 — "use the harnesses; give our model Fable's ppt skills"
Faisal's direction: stop optimizing a proxy grader; make the open model actually competent at the **agentic pptx skill workflow** the way Fable is. Got repo access → read `pptx-baseline/`. Key architectural facts:
- **The powerbench harness runs the model via Anthropic's API with the REAL pptx skill inside Anthropic's server-side code-exec container** (`harness/rollout.py`, `HARNESS_CONTRACT.md`, top-level `harness/generate.py`). The bash/python render→inspect→fix loop happens *on Anthropic's side*. Model = `claude-fable-5`, `max_tokens=64000`, `effort:high`, stream, betas code-execution+skills+files.
- **Our open model CANNOT run inside that harness** (skill license forbids local install; container is Anthropic-hosted). So the harness is the **source of expert demonstrations + the eval target we benchmark against**, NOT where the open model runs.
- The team's documented method (`OSS_TRAINING_DECISIONS.md`, written *for Faisal*): **distill Fable trajectories → (state, tool-call) SFT pairs in OUR clean-room tool schema → SFT warm-start the open model → then GRPO on the conformance env.** "Grader doesn't matter" (Faisal) = imitation/SFT doesn't need the reward; the grader is for the later RL step.
- **6 real Fable trajectories already in the repo** at `pptx-baseline/pilot-data/`: A1(type A, 6/8), B2(type B, **8/8 perfect**), C3(type C, 7/8), 2 rollouts each. Each = `transcript.json.gz` (full block sequence) + `deck.pptx` + `grade.json` + `metadata.json` + `png/`. 21–36 tool calls each, $15–91/rollout. **→ can distill + SFT NOW, no API key.**
- Trajectory block schema: one message, ordered blocks = `thinking`(REDACTED — Fable returns no raw reasoning → behavioral cloning of ACTIONS only) · `server_tool_use`(name=`bash`|`text_editor`, the actual commands) · `bash_/text_editor_code_execution_tool_result` · `text`(brief notes).
- **HARD licensing constraint:** the proprietary pptx `SKILL.md` text appears verbatim in transcripts (Fable `cat`s it) — must be **stripped** from all training data; train on Fable's actions re-expressed in our own clean-room schema, never Anthropic's skill wording.
## 🔁 SECOND PIVOT 2026-07-21 (same day) — Faisal corrected: NOT distillation/SFT
Faisal: *"no bro. the whole thing is literally just training qwen 3.6 27b on the slidesdeck dataset grpo. and measuring how good it gets. using the harnesses on 27b to give it the pptx skill."* → killed the distillation workflow. The real task = **agentic multi-turn GRPO**: the 27B runs the pptx skill loop (write python-pptx → render slides → SEE them → fix → submit) inside a verl tool-agent harness, rewarded by the slide grade, on the slidesdeck (SlidesBench) data. Faisal chose the **full agentic harness** (visual QA loop) over skill-in-prompt.

### verl agentic harness — BUILT 2026-07-21 (all in `scratchpad/agentic/` + box `~/powerbench/agentic/`)
- verl 0.8.0 natively supports this: `experimental/agent_loop/tool_agent_loop.py` (multi-turn tool rollouts) + `tools/` framework. **`ToolResponse` carries `image=[...]` → tool can hand rendered slides back to the VLM** (verl asserts "use a VLM"; the 27B is one). Stays on **vLLM async** (`rollout.mode=async`), NO SGLang switch needed.
- **`pptx_tools.py`** — 3 `BaseTool` subclasses: `run_python` (exec python-pptx in a per-trajectory sandbox, keyed on `agent_data.request_id` since create/execute/release fire every call), `render_slides` (soffice→pdf→pdftoppm→PIL, dpi80/≤768px/≤6 slides to bound vision tokens; returns images), `submit` (grades deck in-sandbox: 0.2 exec + 0.6 text-coverage + 0.2 layout, emits `<<REWARD:x>>` sentinel + tool_reward). Runs model code with `sys.executable` (venv, has python-pptx) + `CUDA_VISIBLE_DEVICES=""` (can't touch GPUs). **Standalone test on box PASSED**: build→render(1 img 768×576)→grade=1.0.
- **`agentic_reward.py`** — `compute_score` parses last `<<REWARD:x>>` from solution_str (verified: naive reward mgr decodes FULL response incl. tool-response tokens, so sentinel is visible). 0.0 if no submit.
- **`build_agentic_data.py`** → `data/slidesbench_agentic/{train,test}.parquet` (141/45): prompt=[cleanroom system, user instruction], `extra_info.tools_kwargs.submit.create_kwargs.ground_truth={required_texts}`, need_tools_kwargs. **`cleanroom_system_prompt.txt`** = our own words (no Anthropic SKILL.md).
- **`tool_config.yaml`** — registers the 3 tools (verl registry loads them OK). **27B tool-call format = `qwen3_coder`** (XML `<tool_call><function=name><parameter=x>…`), verified from its chat template; parser `multi_turn.format=qwen3_coder`. Template opens with `<think>` → reasoning ON.
- **`run_agentic_smoke.sh`** — 1 prompt × n=8 × 1 step. Config: mode=async, multi_turn.enable, tool_config_path, format=qwen3_coder, max_assistant_turns=5, `+rollout.limit_images=16` (the `+` matters — not in base struct), agent.default_agent_loop=tool_agent, gpu_mem_util=0.4, all the 24k-era memory flags (sdpa, chunked logits, FSDP2 offload). Config gotchas hit + fixed: `+limit_images`, real_train_batch_size(=bsz×n) divisible by 8 GPUs, ppo_mini_batch_size≤train_batch_size (it's in PROMPT units). Detached launches kept not sticking → use `setsid nohup ... > fresh.log &`.
- ✅✅✅ **SMOKE PASSED 2026-07-22** (`AGENTIC_SMOKE_EXIT=0`). The full agentic GRPO loop works end-to-end on the 27B:
  - **`num_turns mean 9.75 (8–10)`** — real multi-turn loops; all 3 tools fired; **8/8 rollouts produced output.pptx**. Vision path engaged (`_bilinear_pos_embed_kernel` JIT'd → the model SAW its rendered slides).
  - **Reward live: `critic/score mean 0.5, min 0, max 1`** → GRPO advantages ±0.935, **grad_norm 1.51** (vs ~0.2 single-turn — much stronger signal). No truncation (clip 0.0, resp mean 4788/max 9944 of 10240).
  - **MEMORY: peak 35.6 GB alloc / 57.8 GB reserved on 80 GB — NO OOM, ~22 GB headroom.** Answers Faisal's "more GPUs?" → NOT needed on 8×A100-80GB; images+multi-turn fit. More GPUs would only buy speed/bigger batches. CPU offload 865 GB.
  - **Timing: ~19.6 min/step** (gen 17.7 min dominates — 8 agentic trajectories, long thinking; update_actor 87s, weight-sync 15s).
  - **Deck quality check: all 8 decks = 1 slide, 3/3 required texts, real content.** The reward's 0.0 cases were rollouts that ran OUT OF TURNS before calling `submit` (5-turn cap), NOT bad decks → raised max_assistant_turns 5→6 for the real run.
- 🟢 **HILL-CLIMB 2026-07-22** (`run_agentic_grpo.sh`): train_batch_size=8 × n=8 (64 traj/step), lr 5e-6, max_assistant_turns=6, max_response_length=12288, **val_before_train=True** (45 held-out), test_freq=5, total_training_steps=15, save_freq=10, **W&B rl-intro/powerbench-agentic, run agentic-hillclimb-27b** (first run fsznr5gy).
  - ✅ **BASELINE held-out reward = 0.389** (`val-core/slidesbench/acc/mean@1:0.3888`, num_turns mean 11.5). This is the "before". (Lower than the old single-turn 0.538 because the agentic reward also requires completing the loop + calling submit within the turn budget — harder task, more headroom. Not directly comparable.)
  - ⚠️→✅ **vLLM WAKE-UP OOM ROOT-CAUSED** (`cumem_allocator.cpp:163`, "wake_up method failed") at the val→train / step→step transition. The smoke (1 step) never slept/woke vLLM so never caught it. Lowering gpu_mem_util **0.45→0.35 did NOT fix it** (both wake-OOM'd) → NOT a util problem. **REAL ROOT CAUSE: `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True` is INCOMPATIBLE with vLLM sleep mode (cumem allocator).** vLLM's own `config/vllm.py:858` says so verbatim; crash log shows vLLM's cumem forcibly toggling `expandable_segments:False` (cumem.py:333) while the actor's PyTorch allocator has it True → allocators fight over GPU VMM → cumem can't map at wake → OOM, regardless of util. **THE GOTCHA: expandable_segments was ADDED for the 24k single-turn actor log_softmax OOM, but it BREAKS the colocated vLLM sleep/wake that multi-step agentic GRPO needs.** At 12k budget + chunked logits the actor no longer needs it. **FIX: `unset PYTORCH_CUDA_ALLOC_CONF`**, relaunched grpo3.log (util 0.35). Residual risk: actor OOM in update_actor without expandable (chunked logits should hold; else reduce max_response_length).
  - THIS is Faisal's "measure how good it gets": baseline ~0.40 → does it climb over 15 steps. Thinking is ON throughout (template default, verified).
  - ✅✅ **TRAINING (grpo5, 2026-07-22)** after the wake-OOM saga. **WORKING FIX = two config changes (NO code patch):** `actor_rollout_ref.rollout.free_cache_engine=False` (vLLM never sleeps → no wake → no cumem conflict; stays resident, weights synced in place each step) + `actor_rollout_ref.rollout.max_model_len=32768` (cap the model's 262k native context so vLLM can init its KV pool inside the small resident slice) + `gpu_memory_utilization=0.25` (vLLM ~20GB resident coexists with actor ~36GB alloc). vLLM+actor coexist cleanly (~20GB/GPU), **0 OOM**. ~37 min/step, 15 steps ≈ 8.5h. **W&B run flxq1py4** (rl-intro/powerbench-agentic). Eval every 5 steps (val_before_train + test_freq=5). The 5 failed launches: grpo(0.45 wake-OOM)→grpo2(0.35 wake-OOM)→grpo3(unset env, but verl re-enables expandable via device.py→wake-OOM)→grpo4(free_cache_engine=False but util 0.25 too low for 262k KV init)→grpo5(+max_model_len=32768 → TRAINS).
- ❌ **AGENTIC HILL-CLIMB FAILED (grpo5–grpo8) — reward was the problem, not the harness.** Four runs, no climb. grpo5/6: text-coverage reward pays for text → model rambled → truncation → reward fell. grpo8 (added length + non-submit penalties): oscillated then **COLLAPSED — held-out −0.024 → −0.50** (the floor; model stopped submitting entirely). Killed 2026-07-23.
- 📚 **DEEP RESEARCH (2026-07-23) → [pptx-hillclimb-design.pdf](../../pptx-hillclimb-design.pdf)** (Desktop). 23 primary sources, 8 adversarially-verified claims. Every one of our failures is a documented failure mode:
  - **Presence-based rewards are structurally gameable** (arXiv 2605.12474): rubrics 90.2% presence-weighted → verbosity hacking, conciseness −2.91. Our reward was 100% presence-based.
  - **Negative floor kills the gradient** (arXiv 2601.03525): identical rewards across a group = zero relative advantage = zero gradient. Binary/sparse rewards give degenerate groups 60–70% of the time. **Our −0.5 non-submit penalty made EVERY group degenerate.** Correct recipe: invalid ⇒ **reward 0, never negative**.
  - **GRPO has a built-in length bias** (Dr. GRPO): loss ÷ token count ⇒ longer responses get more gradient. Fix via verl `loss_agg_mode=token-mean`. We had TWO forces pushing verbosity — reward *and* algorithm.
  - **Multi-turn + thinking-on + GRPO is the most collapse-prone config that exists** (arXiv 2512.17008, EACL 2026) and standard fixes barely help. → **run single-turn first** to isolate the reward variable.
  - **BLUEPRINT: AeSlides (arXiv 2604.22840)** — GRPO on a 30B open model, 8×H100, deterministic *layout* rewards from the rendered slide: aspect 36→85%, collisions −31%, imbalance −28%, render errors 0.78%→0.00%, beat Claude-Sonnet-4.5 on human layout quality. Also: keep KL, per-metric decoupled normalization, expect to be gamed and patch the metric.
  - **CORRECTION: PPT-Eval is a GUI/computer-use benchmark** (PowerPoint Online), NOT programmatic editing — do not build against it. PresentBench = instance-specific binary rubrics but LLM-judged ⇒ eval, not training reward. **Keep our SlidesBench task split; change only the reward.**
- ✅✅✅ **GEOMETRIC REWARD BUILT + GATE PASSED 2026-07-23.** Files (backed up locally in `reward/` — box is ephemeral): `geom_shapes.py` (collision/overflow/imbalance via python-pptx), `geom_pixels.py` (whitespace/density from rendered PNGs), `make_fixtures.py` (good/padded/empty decks), `geometric_reward.py` (integrator: equal-weight mean, **invalid ⇒ 0.0 never negative**, per-metric breakdown logged), `gate_test.py`.
  - **GATE RESULT — the money table:**

    | deck | words | NEW geo | OLD coverage | per-metric (new) |
    |---|---|---|---|---|
    | good | 81 | **0.9144** | 1.0000 | coll 1.00 · imba 0.92 · over 1.00 · whit 0.74 |
    | padded | **1618** | **0.6110** | **1.0000** | coll 0.46 · imba 0.75 · over 0.83 · whit 0.40 |
    | empty | 0 | 0.2807 | 0.0000 | coll 0.50 · imba 0.12 · over 0.50 · whit 0.00 |

  - **The old reward scores good and padded IDENTICALLY (1.0000 vs 1.0000)** despite padded having 20× the text, overlapping boxes and off-slide spill — empirical proof of the hole that collapsed grpo8. New reward separates them by **0.30**.
  - Anti-padding verified independently by the pixel scorer: score vs words peaks at ~205 words (1.000) then **falls to 0.178 at 1000 words**. More text ⇒ lower reward, by construction.
  - Blank-deck hole closed: a shapeless slide scores 0.0 (not 1.0) on all shape metrics, so emitting empty decks is not a free win.
- 🟡 **SINGLE-TURN GEOMETRIC RUN (geo1/geo2, 2026-07-24):** reward pipeline works — **geometric held-out BASELINE = 0.579** (val_before_train completed). Bugs fixed en route: (geo1) reward returned inconsistent extra-info keys across rollouts → verl `KeyError: 'm_collision'` in reward-extra aggregation → fixed with a FIXED 6-key schema (score, valid, m_collision/overflow/imbalance/density) returned for every rollout incl. invalid.
  - ⚠️ **geo2 was WAY too slow: ~83 min/step, 26h ETA** (vs 37 min/step multi-turn). Cause: the reward rendered EVERY rollout with soffice → **64 concurrent soffice processes/step saturated the host CPU** (on top of 865 GB CPU offload). The box went **UNRESPONSIVE and could not be reached to even kill the run** (9+ ssh attempts over ~10 min all failed → host almost certainly OOM'd/hung). Lambda ephemeral box, same instability class as before.
  - ✅ **FIX (done + validated locally, ready to redeploy): render-free reward.** Replaced the pixel-whitespace metric with a **content-DENSITY metric computed straight from python-pptx** (text chars/slide, banded: empty=0, good≈1, padded/crammed→0). No soffice, ~50× faster, no subprocess storm. Band validated locally: empty 0.000, minimal 0.003, good(232 ch) 1.000, padded(4900 ch) 0.000, crammed(2000) 0.286. Also lowered code-exec timeout 60s→12s (pathological generated code was eating step time). Files updated in `reward/` (geometric_reward.py render-free, singleturn_geometric_reward.py RENDER=False). Gate passes by construction: padded≈0.41 < good≈0.98.
  - **BLOCKED on box recovery.** Box `<GPU_BOX_IP>` unreachable. Need: reboot it (Lambda console/API — Faisal has the API key) OR a fresh box IP. Recovery is fast: everything backed up in `reward/`; `~/setup_box.sh` rebuilds a fresh box; redeploy 5 files + relaunch ≈ 15 min. **Lesson: never fork a per-rollout subprocess storm (soffice) inside the reward — keep rewards pure-python/in-process.**
- **(superseded) single-turn GRPO with the geometric reward.** lr 1e-6–2e-6 (5e-6 was aggressive), keep KL ~0.01, G=8, `loss_agg_mode=token-mean`, invalid⇒0. **Instrument reward STD** (collapses BEFORE mean — earliest collapse warning, RAGEN/StarPO 2504.20073), degenerate-group rate, grad-norm spikes (once they appear collapse is irreversible). **Success = held-out climbs WITH response length flat or falling** — that second condition is what proves it learned layout, not verbosity.
  - **Speed note:** free_cache_engine=False forces low util (vLLM resident during training), so rollout KV is smaller → slower than sleep-mode would be. If we need faster: non-colocated / dedicated GPUs for vLLM (the clean answer to "more GPUs") fully separates actor & vLLM and restores full util each side. Not needed for correctness; a throughput lever.
- 🔬 **GEO2 RESULT (2026-07-23, pixel-whitespace reward): a REAL hill-climb.** Held-out 0.579(base)→0.666(step9); train reward 0.38→0.58 while **response length HALVED (11,243→4,462)**. Reward↑ + length↓ = learning layout, NOT padding. grad_norm stable. Died step 9 on FSDP2+offload **checkpoint-save** device-mismatch (not a training failure). Saving fix = patch verl to on-load params before state_dict(); interim save_freq=-1.
- 🎨 **UI: [reward-review.pdf](../../reward-review.pdf) (Desktop) + Artifact e0738282.** Verdict-first dashboard (real-vs-hacking), held-out curve, reward-vs-length chart, full reward breakdown, gate, config.
- ⚠️→✅ **VERSION MISMATCH CAUGHT + FIXED (2026-07-24).** A render-free reward rewrite (density-from-text-volume replacing pixel-whitespace — to kill the 64-soffice/step "storm" = 83min/step + box hang) landed LOCAL but only `singleturn` deployed; box `geometric_reward.py` stayed pixel-whitespace. Result: box inconsistent — singleturn asked `density`, geometric_reward gave `whitespace` + no images → **geo3 trained on shape-metrics ONLY, density=0.0** (minimal-slide hole reopened). Gate still passed because gate_test renders fixtures → gate ≠ training reward. **Deployed consistent render-free pair; re-gated padded 0.41 < good 0.98 (margin 0.58 > the 0.31 pixel version).** Killed geo3 → **geo4** = correct reward. Lesson: verify the reward the TRAINER actually runs, not just what the gate runs.
- 🖼 **GALLERY (view slides in UI): WIRED + verified.** `_save_gallery` was defined-but-never-called; fixed. Ray workers don't inherit env → GALLERY_DIR hardcoded. Render-free training → gallery saves `.pptx` + score-in-filename + meta; **render decks→PNG POST-HOC** for the UI. Verified a 0.93 deck saved.
- ⏳ **CHECKPOINTS owed:** save_freq=-1 for now (FSDP2 save bug); patch + one final weight-saving run after the curve confirms.
- ✅ **PIPELINE AUDIT (systematic-debugging, 2026-07-24): reward path VERIFIED ROBUST.** Ran all 7 risky response paths through the real `compute_score` (good / add_picture-missing / placeholder / no-code / no-save / infinite-loop / syntax-error): **every one returns the identical 6 keys and NONE raises** (a raise or key-mismatch is what crashes verl's reward manager). 12s exec timeout catches loops. No run-breaking errors remain. The three bugs that actually bit us (−0.5 floor, inconsistent keys, local/box version split) are all closed.
- 🖼 **IMAGE DEPENDENCY — root-caused + fixed via OPTION B.** ROOT CAUSE: public AutoPresent/SlidesBench ships instructions ONLY (no reference decks, no media) — each task dir has just instruction*.txt; tasks say "insert an image of X" but the image was never released. ~29% of train / 20% of test need a file; ~51-57% mention a background image. Before fix: those crash (`add_picture('x.png')`→FileNotFoundError→reward 0) or draw a placeholder. **FIX (B):** neutral placeholder `agentic/assets/image.png` (labeled "IMAGE") copied into every rollout tempdir under 9 common names (`image.png`,`logo.png`,`background.png`,…) by `_provision_assets()`; system prompt rebuilt to tell the model to use `image.png`; dataset rebuilt (SAME 45 held-out, verified identical). Verified: `add_picture('image.png')`→valid 0.49 (was 0); odd names still graceful-0. Options A(filter)/C(OpenAI-generate, sdk+credits on box) documented but B chosen — cheapest, keeps all tasks, no API cost. Caveat: density counts TEXT only, so image-slides score a bit lower on density (visible in gallery, a tradeoff not a bug).
- 🖼 **GALLERY ENRICHED for human review.** Per sampled rollout (1-in-6, cap 24/worker, spans the run) saves 3 files: `deck_*.pptx`, `code_*.py` (the model's actual script), `meta_*.json` {score, per-metric, valid, resp_tokens_est, code_chars, task, index, seen}. Render decks→PNG post-hoc for the UI.
- ⚠️→✅ **ORPHAN vLLM PROCESSES = the relaunch hang (root-caused 2026-07-24).** geo5 hung 90 min in vLLM init: log frozen, GPUs 0-3 pinned at 100%, no scoring. CAUSE: `pkill -f main_ppo` + `ray stop` (my kill commands) do NOT match vLLM's child workers, which are named `VLLM::Worker_TP` / `VLLM::EngineCore`. Across many relaunches these orphaned + kept holding GPU memory (found procs aged 20,200s = 5.6h, older than the live run). New vLLM deadlocked against the ghosts. **FIX: kill by GPU OWNERSHIP** — `for p in $(nvidia-smi --query-compute-apps=pid --format=csv,noheader); do kill -9 $p; done` in a loop until `memory.used`≈0, THEN relaunch. Use this every relaunch. geo5→geo5b relaunched on clean GPUs (4 MiB/GPU).
- 🔧 **INIT-HANG DEBUG (systematic, 2026-07-25) — box is HEALTHY, likely killing runs too early.** geo5/5b/6 all "hung" at the same point (log frozen right after `set_expandable_segments` device.py:146, all GPUs 100%). Ruled out in order: (1) orphan `VLLM::` procs holding GPU mem — real, cleaned (kill by GPU ownership, not name); (2) stale `/dev/shm/cuda.shm.*` IPC — cleaned, still hung; (3) **MINIMAL 8-GPU NCCL all-reduce WORKS** (`NCCL_ALLREDUCE_OK world=8`), GPUs 0 ECC / no throttle → **box NOT wedged, NO reboot needed**; (4) stale Ray state cleaned. **KEY REFRAME:** qwen3_5 Gated-DeltaNet needs **Triton JIT compile on first forward (~15-25 min, GPU pinned 100%, ZERO log output)** — visually identical to a deadlock. My freeze-detector fired at 8-10 min → I was **killing geo5b/geo6 DURING legit compilation.** Only geo5's 90-min stall was a true hang (orphan-induced). **FIX: patience** — 25-min frozen threshold before calling hang. geo7 relaunched with patient watch. **HARDENED RELAUNCH (use every time):** kill by GPU-owner PID + `rm /dev/shm/cuda.shm.*` + `rm -rf /tmp/ray/*` + verify GPUs≈0 MiB, THEN relaunch; then WAIT ≥25 min for GDN compile before assuming hang.
- ✅✅ **INIT-HANG FIXED (2026-07-25): `enable_prefix_caching=False`.** The real root cause of the geo5b/6/7 init hangs was **vLLM's EngineCore deadlocking on the Mamba/GDN cache-"align" path that prefix caching triggers for Qwen3_5** (log warning `config.py:563 Mamba cache mode 'align' ... when prefix caching enabled` right before every freeze). NOT box state (NCCL all-reduce passed clean), NOT our code. Disabling prefix caching (verl `actor_rollout_ref.rollout.enable_prefix_caching=False`, passed to vLLM at vllm_async_server.py:264) dodges it. geo8 CLEARED INIT and produced a real rollout. **Lesson: for the GDN arch, prefix caching off. Also: gallery deck count — not loglines — is the real progress signal (main log goes silent during baseline generation; my loglines freeze-detector was false-positiving).**
- ✅ **VERIFIED SAMPLE (task #12, geo8 baseline): the model makes GOOD slides.** Real rollout on career/slide_12: 27B generated ~8,230 tokens → 4,333 chars python → deck **scored 0.9632** (collision 1.0, overflow 1.0, imbalance 0.82, density 1.0). Rendered PNG (`scratchpad/sample_slide.png`): a genuinely well-composed dark-theme slide — bold title + 5 gold-header sections with white sub-bullets. Reward and eyeball AGREE → real learning signal, not a hack. NOTE: this is BASELINE (untrained) — base 27B already strong on detailed prompts; individual task 0.96 but held-out AVG baseline ~0.5 (prior runs) so headroom exists on average. Reinforces the "detailed = easy prompt" framing.
- 🟢 **geo5..geo8 = DEFINITIVE RUN (2026-07-24/25):** new dataset (image guidance) + placeholder provisioning + enriched gallery + gate-verified render-free density reward (padded 0.41 < good 0.98). Same run config (lr 1e-6, KL 0.01, token-mean, single-turn, save_freq=-1, 15 steps). NEXT: verification sample (render one real geo5 deck + trace reward) + detailed gallery UI.
  - ⚠️ **grpo5 LENGTH-RUNAWAY (killed by Faisal's call 2026-07-22).** Baseline 0.359 → **step-5 held-out 0.311** (down, though within ±0.07 noise). The unambiguous failure: **response_length exploded 5.4k→6.3k→7.9k→7.3k→10.1k** over steps 1-5, **clip_ratio 1.6%→37.5%** (a third of rollouts truncated before they could `submit` → score 0), grad_norm faded 0.70→0.27, steps slowed 37→74min. Mechanism = model learns to THINK longer per turn (gaming the coverage proxy), overshoots the 12k cap, truncates, fails to submit → reward falls. Train reward per step: 0.455/0.397/0.260/0.362/0.338 (noisy, drifting down).
  - 🟢 **grpo6 (dynamics fix, 2026-07-22):** **kl_loss_coef 0.01→0.05** (anchor policy to base → resist length drift) + **max_assistant_turns 6→5** (cap the loop). Same working memory config (free_cache_engine=False, max_model_len=32768, util 0.25). Watching response_length/clip_ratio EARLY (by step 3) to catch recurrence fast. If it STILL runs away → escalate to reward rework (option 3): penalize truncation/non-submit (−reward) + length, since the SlidesBench coverage proxy is inherently length-gameable (the real powerbench OOXML grader wouldn't be, but it's not ready).
  - ⚠️ **grpo6 STILL ran away** (KL 0.05 + turns 5 slowed but didn't stop it): resp_len 5.2k→6.5k→8.5k, clip 0%→11%→20% over steps 1-3, reward 0.31→0.33→0.17. Turn count flat (~9.6) → model thinks LONGER PER TURN. Confirmed: dynamics-tuning can't fix a gameable reward.
  - 🟢 **grpo7 (REWARD REWORK, 2026-07-22):** `agentic_reward.py` now shapes the reward — **non-submit (truncated/gave up) → −0.5** (strong), **submitted → grade − 0.2·(len/12288)** (mild length penalty). Self-test: concise-good 0.97 > long-good 0.82 > truncated −0.5. Keeps KL 0.05 + turns 5 + working memory config. Started clean (no leftover-vLLM race). Watching resp_len/clip EARLY (flag at clip>10%). This makes "ramble long & truncate" the worst outcome → should finally bound length. If THIS still runs away, the SlidesBench proxy is a dead end for RL and we wait for the real powerbench OOXML grader.
  - **grpo7 step 1 GREAT** (5024 tok, 0% trunc — reward fix looks right on length) then **OOM'd on ZOMBIE vLLM**: killing main_ppo did NOT kill grpo6's vLLM servers → 2 vLLM/GPU (~18GB each) stacked → actor starved → torch.OutOfMemoryError. **GOTCHA: `pkill -f vllm/main_ppo` leaves vLLM HttpServer/EngineCore ray actors alive.** FIX = kill by GPU PID: `nvidia-smi --query-compute-apps=pid | xargs kill -9`, loop until `memory.used ~0` on all 8, THEN relaunch. Always verify GPUs ~0 (not just main_ppo count) before launch. (Note: box has no `bc` — use awk for GPU-mem math.)
  - 🟢 **grpo8 (clean relaunch, reward fix): step 1 = 4852 tok, 0% trunc, baseline −0.024** (new reward scale), single vLLM/GPU (~20GB, no zombie), ~32min/step. Awaiting steps 2-3 to confirm length stays flat (grpo6 betrayed the runaway by step 2-3). grpo8.log.
- **Superseded (kept for reference):** the 24k single-turn SlidesBench RL hill-climb (OOM'd on actor per-seq backward at 24k; 16k was clean) AND the distillation/SFT idea. Both dead. The agentic GRPO harness above is THE approach.
- **Needs API key for scale:** `ANTHROPIC_API_KEY` (Faisal's hackathon credits / Michael) → run Fable (and other strong models — Faisal: "same skills as fable *and other models*") through `harness/run_baseline.py` on all ~10 tasks × k=8 ≈ 80 trajectories ≈ $880 → much bigger SFT set.

## Deadlines (from workplan §4–5)

| Date | Deliverable | Status |
|------|-------------|--------|
| **Jul 15 (today)** | Rollout-orchestration skeleton + agree `run_rollout(task_dir, model, workdir) → {pptx, transcript, metadata}` contract with Michael | ⬜ blocked on repo access (see blockers) — contract can be agreed today anyway |
| **Jul 16 (tomorrow)** | **Open-model recommendation memo** — primary + fallback + switch triggers | ✅ DONE 7/16 → [model-recommendation.md](model-recommendation.md). **Decision: train Qwen3.6-27B only** (480B ≈16 GPUs to train); 480B kept as eval-only measuring stick; Kimi K2.7-Code via Fireworks RFT = fallback |
| Jul 17 | Ratify at checkpoint; serving live: 27B, OpenAI-compatible. **480B dropped 7/17 — too big/expensive, not used even for eval; sanity check = SlidesBench published leaderboard** | 🟡 IN PROGRESS |

### GPU box (as of 2026-07-20)
- **CURRENT box: `ubuntu@<GPU_BOX_IP>`** — 8× **A100-SXM4-80GB** (Lambda Cloud), 19TB disk, fresh. Auth via `id_ed25519`. Rebuilding env (scripted: `~/setup_box.sh`).
- **Lambda infra is unstable / ephemeral:** prior box `192.222.54.237` (8× H100) was serving fine, then **died mid-work and was TERMINATED** (not rebooted) ~2026-07-20 — local disk wiped, lost the env (had to rebuild). Lambda on-demand = ephemeral disk; **ask Michael to attach a persistent filesystem** so a box loss doesn't cost the weights/checkpoints. Faisal now HAS the Lambda API key → can check/restart instances via `curl -u KEY: https://cloud.lambda.ai/api/v1/instances` (saving it would let Claude self-recover next time).
- Dead earlier IPs: `149.118.65.110`, `209.20.157.115`, `192.222.54.237` (all gone). Egress IP `66.234.202.222`.
- **A100 note:** ✅ RESOLVED — flashinfer compiled the SM80 GDN kernel fine; vLLM serves on A100 (torch.compile ~71s startup). The "SymmMem capability 8.0 not supported" line is a benign H100-only-feature fallback, not an error. Gate 1a re-passed on A100 (generated correct python-pptx code).
- **Harness note:** model wraps reasoning in `<think>...</think>` then emits final answer → SlidesBench code extraction must take content AFTER `</think>` (or last ```python block). Uses ~800 tokens even for tiny answers (reasoning).
- env rebuild is scripted: `~/setup_box.sh` (local copy in scratchpad) — full rebuild + model download in one shot if we lose another box.
- ✅ **GATE 1b PRE-CHECK PASSED**: transformers forward+backward on the 27B gives finite/nonzero grads through the Gated-DeltaNet layers (linear_attn.A_log, conv1d, dt_bias). Architecture IS trainable. Fast path wants `flash-linear-attention`+`causal-conv1d` (not installed → torch fallback works but slow; INSTALL for real runs).
- ✅✅ **GATE 1b PROPER — PASSED 2026-07-20.** verl completed a FULL GRPO step on Qwen3.6-27B: `VERL_EXIT_0`, `training/global_step:1`, `actor/grad_norm:0.80`, `actor/loss:0.0`, `critic/score/mean:0.25` (max 1.0 — reward signal live), GRPO advantages ±0.866 computed. Entire path proven: **vLLM rollout → old/ref logprob → reward → GRPO advantage → actor gradient update → weight sync.** Mem: 63.7GB GPU peak (fits A100-80GB), 363GB CPU offload. ~225s/step (gen 76s · logprob 22s · ref 9s · update_actor 95s · weight-sync 23s). The trailing "DataLoader worker killed" line is teardown noise after step 1 — exit was 0.
  - **How the flash_attn blocker was solved:** real flash-attn CUDA-13 build failed (CCCL header/compiler incompat — cu13 headers vs mixed include paths). But verl only needs `flash_attn.bert_padding` (unpad_input/pad_input/index_first_axis — PURE TORCH, no kernels; model runs sdpa, rollout runs vllm). Dropped in a pure-torch shim: `scratchpad/bert_padding.py` → `~/powerbench/.venv/.../flash_attn/bert_padding.py` + `__init__.py`. Works. **For real runs (speed):** sort real flash-attn (isolate cu13 includes / find prebuilt wheel) + install `causal-conv1d`+`flash-linear-attention` for GDN fast path → expect 3-5× speedup over the ~225s/step torch-fallback.
  - **WORKING verl config — the 1-step smoke that passed** (`~/powerbench/run_gate1b_smoke.sh`, local in scratchpad): data=gsm8k text (NOT geo3k — multimodal image tokens >4096 → filter-empties dataset); `data.val_batch_size=8`; `+actor_rollout_ref.model.override_config.attn_implementation=sdpa` (THE clean fix for verl's FA2-default model load); `use_remove_padding=False`; `strategy=fsdp2`; `use_torch_compile=False`; param+optimizer CPU offload; GEN_TP=4; rollout n=4. Reward = gsm8k built-in (swap for our SlidesBench/powerbench grader next).
  - Box state: all 8 GPUs free after run. Also sed-patched verl `utils/model.py`+`engine.py`+`engine/automodel.yaml` FA2→sdpa earlier (redundant vs override_config; harmless).
- 🟡 **(1)(2)(3) IN PROGRESS 2026-07-20:**
  - **(1) SlidesBench reward WIRED + tested.** Finding: public AutoPresent repo ships instructions only, NO reference decks (only food.pptx) → can't run the exact reference-based metric. Built instead a deterministic program-scored reward (`~/powerbench/slidesbench_reward.py`, local copy scratchpad): execute model's python-pptx code in sandbox → score = 0.2·exec-gate + 0.6·text-coverage(required strings from instruction) + 0.2·layout-sanity. Self-test: good=1.0, junk/broken=0.0. Dataset: `~/powerbench/data/slidesbench/` 186 tasks (141 train/45 eval) via `build_slidesbench_data.py`. Needed `pip install python-pptx` in venv. **Caveat: text-coverage is somewhat gameable (blob-dump); fine for proof-of-life, NOT the honest grader — real reference-based metric + Cara's grader come with reference decks / powerbench env.**
  - **(2) Speedup: SOLVED 2026-07-21.** Root cause of the CCCL build conflict = cu13 pip toolkit's bundled headers didn't match its nvcc. Fix: installed the real **CUDA 13.0 toolkit via NVIDIA apt repo** (`cuda-keyring` → `apt install cuda-toolkit-13-0` → `/usr/local/cuda-13.0`), a consistent nvcc+headers matching torch cu130. Then `causal-conv1d` built clean (1.6.2.post1, CCV_EXIT_0) with `CUDA_HOME=/usr/local/cuda-13.0`. Now fla + causal-conv1d BOTH present → **Gated-DeltaNet fast path engages** (was the ~3-5× training bottleneck). Real flash-attn also now buildable this way (still using the pure-torch bert_padding shim, works). Done in PARALLEL with training (CPU build, no GPU contention).
  - **(3) Hill-climb:** `~/powerbench/run_hillclimb.sh` — GRPO 12 steps, batch 8, rollout.n=8, val_before_train=True + test_freq=6 → baseline vs post-train eval reward on 45 held-out tasks = the +2-3% proof-of-life.
    - **First run KILLED (truncation finding):** baseline was 0.04 but response_length maxed at 1536 for 100% of rollouts — the reasoning model burned the whole budget on `<think>` and never emitted code → reward ~0. Not a slide-quality floor, a truncation floor.
    - **⚠️ v1 OOM'd overnight after step 1** (got baseline 0.538 but NO training curve). Cause: I trimmed the memory-saving flags from verl's qwen3_5 recipe → `log_softmax` over 16k-seq × 248k-vocab OOM'd in update_actor. **FIX: re-added `entropy_from_logits_with_chunking=True` (actor+ref), `fsdp_config.offload_policy/reshard_after_forward/entropy_checkpointing=True`, + `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True`.** Relaunched v1 (fixed) + robust queue watcher (`queue_watcher.sh` — launches v2 only on clean exit, aborts on crash). Watch: per-step CPU-offload memory was growing (363→659GB) — monitoring whether it survives all 6 steps.
    - **DIAGNOSIS (2026-07-21): the 6-step run was UNDER-TRAINED, not broken.** Reward signal was healthy (critic/score spread 0.0-1.0, advantages ±2.47 — GRPO had a gradient). But grad_norm ~0.2, **KL from ref ~0.0006** → policy barely moved in 6 steps at lr 1e-6 → held-out unchanged within eval noise (SE~0.05). Not a regression, just no movement. Repo: Faisal joined it 2026-07-21 but **powerbench grader NOT done yet** → verifying pipeline with SlidesBench deterministic reward as stand-in.
    - **24k MEMORY IS TIGHT — 3 failures then fix.** `run_fable.sh` (thinking ON, 24576 tok, lr 5e-6, 100 steps, eval n=4, save_freq=10). Failures at 24k: (1) startup race — relaunched too fast, old run's vLLM still held ~45GB → clean relaunch w/ GPU-free wait fixed it; (2) **vLLM wake-up OOM** (`cumem_allocator.cpp:163` — verl sleeps vLLM during train step, wakes it for next rollout; at 24k the train step's footprint left too little free for vLLM's 0.6 KV alloc) → **fix: gpu_memory_utilization 0.6→0.4**. 16k ran clean at 0.6; 24k needs 0.4. **If 0.4 still OOMs at the first train step, 24k + full 8×8 rollouts genuinely won't fit → must reduce rollouts (n=8→4) or budget.**
    - **FULL RUN config: thinking ON, 24576 tokens, lr 5e-6, 100 steps, eval n=4, test_freq=5, save_freq=10, gpu_mem_util 0.4, wandb hillclimb-fable.** SLOW (~45+min/step at 24k thinking-on → multi-day; watch via wandb). The lr 5e-6 (5× the original) is the key fix so the policy actually MOVES (orig run was under-trained, KL~0.0006). Watching: OOM at 24k (bigger than 16k that ran clean w/ chunking fix — should hold), + the held-out climb over steps. (superseded the fast verify run.)
    - (superseded) 16k run: held-out 0.5605 → step3 0.5208 → step6 0.5161 (all within noise). OOM fix held (0 OOM, ran to step 5/6). BUT: **key stat issue — the eval is UNDER-POWERED.** 45 held-out tasks × n=1 stochastic gen → SE ≈ 0.045-0.07, so a 0.04 move (and the +2-3% target itself!) is WITHIN NOISE. Can't resolve a 2-3% effect with this eval. Also the "kill v1 relaunch 24k" SILENTLY FAILED (v1 never died, v2 never started — was watching empty hillclimb_v2.log). **Next: don't just relaunch bigger — fix the eval power (eval with n≥4 samples/task avg, or more train steps for bigger effect, or larger eval set) + inspect rollouts for reward-hacking before more compute.**
    - **CLEAN BASELINE = 0.538** (held-out, 16384 budget, thinking-on, clip 0.17). Progression as truncation was fixed: 1536→0.04, 4096→0.355, 16384→0.538. Slow though: ~43 min/step (gen 32min dominates — long thinking responses; causal-conv1d fast path only helps training passes, not vLLM gen). **QUEUED (2026-07-21):** v1 (16384, 6 steps, running, wandb run 5ivkfl34) → auto-chains → v2 (`run_hillclimb_v2.sh`, 24576 budget +50% to kill remaining clip, SAME 8×8 rollouts, wandb name hillclimb-27b-24k). Queue watcher + `queue.log` handle handoff. ~10h total overnight. WANDB project https://wandb.ai/rl-intro/powerbench-slidesbench.
    - (earlier) **Relaunched 2026-07-21 thinking-ON + 4096-token budget** (Faisal's call — keep the model's best mode, fair vs Fable, train/deploy match; fix truncation via budget not by disabling thinking). Verified chat template supports `enable_thinking`; verl passes it via `data.apply_chat_template_kwargs`. **W&B LIVE:** https://wandb.ai/rl-intro/powerbench-slidesbench/runs/1rqaeh7j (token in ~/.netrc; Faisal to rotate). Watching step-1 clip_ratio — push to 6k if still truncating. ~5-10 min/step → ~1.5-2h.
  - **(4) NEXT after hill-climb: PPTArena 27B-vs-Fable eval.** Cloned `~/powerbench/PPTArena` (Ofengenden editing bench, dual VLM judge, has `run_claude_parallel.py` for Fable). Needs keys: ANTHROPIC (Fable 5), OPENAI+GEMINI (judges) — Faisal's hackathon credits. Run on the TRAINED checkpoint vs Fable 5. Judge-scored = external gut-check, never a training reward.
- Env: isolated `~/powerbench/.venv` (python 3.10). ✅ **BUILT + VERIFIED 7/19**: vLLM 0.25.1, verl 0.8.0, torch 2.11.0+cu130, transformers 5.14.1 — all coexist, 8 GPUs visible, numpy↔torch↔cuda roundtrip OK. verl install did NOT downgrade vllm/torch (constraints.txt pinned them). Watch: numpy 1.26.4 vs opencv wanting ≥2 (cosmetic so far; re-test at first vision inference since 27B is vision-capable).
- Model: `Qwen/Qwen3.6-27B` (Apache-2.0, ungated). config confirms `model_type: qwen3_5` (matches verl's kernel-patched arch). Downloading ~54GB to `~/powerbench/models/Qwen3.6-27B`.
- ✅ **GATE 1a PASSED 7/20**: Qwen3.6-27B serves on vLLM 0.25.1 (TP=2, port 8000, OpenAI-compatible) and generates coherent pptx code. qwen3_5 arch IS supported; only snag was missing `ninja` for flashinfer's Gated-DeltaNet JIT kernel compile → fixed with `sudo apt install ninja-build`. Server currently RUNNING on GPUs 0,1 (~71GB each), 6 GPUs free.
- **NOTE:** 27B is a reasoning model — emits visible chain-of-thought before the answer. Harness must give large max_tokens + extract final code block (don't grade the thinking).
- **Next:** Gate 1b = tiny verl GRPO step on qwen3_5 (confirm it TRAINS, not just serves) → clone SlidesBench on box + wire reward → baseline eval (this local endpoint) → perturbed set → GRPO proof-of-life. Don't need the hosted model API key anymore — serving locally.
| Jul 17–18 | **Gate 1:** tiny verl training step on 27B (arch support likely per docs — confirm hands-on) · **Gate 2:** 27B lands 1–3/8 in smoke test. Either fails → Fireworks/Kimi within 24h | ⬜ CRITICAL PATH |
| Jul 17–18 | SlidesBench on BOTH models (k=3): memorization probe first, build ~50-task perturbed set, report official + perturbed scores. Doubles as GRPO starting baseline. **Role: training proof-of-life — CONFIRMED by verification pass 7/16** (page_eval.py metrics deterministic; use reference-based family only, exclude CLIP + GPT-4o ref-free). Repo is unmaintained (last push 5/2025, 8 open issues) — budget self-fixes. Train tasks: mint our own via `create_dataset.py` (official 7k download is broken; unofficial 31GB mirror = unknown fidelity). AeSlides DEMOTED to methods reference — train set + code withheld, slime not verl, HTML not pptx | 🟡 setup done, blocked on API key |
| Jul 18–20 | PPTArena (arXiv 2512.03042) subset on BOTH models. **Role: external comparison eval** — in-place deck editing incl. master styles, closest public match to our task; VLM-judged (GPT-5/Gemini — use OpenAI hackathon credits), so never a training reward. Dec 2025 release → contamination-clean for 480B, near-clean for 27B. Cite in Cara's report: Sonnet 4 ≈43% success = external proof the domain is unsaturated | ⬜ |
| Jul 18+ | Take over Michael's agent loop after smoke test; harden it | ⬜ |
| Jul 20 | Q2 decided: grader-JSON → scalar reward mapping (I draft, Cara checks for reward-hacking) | ⬜ |
| Jul 24 | Full baseline complete: all tasks × k=8 on Fable + chosen open model (+ Sonnet if budget) | ⬜ |
| Jul 27 | Phase 1 gate (Cara's report) — go/no-go on Phase 2 | — |
| Jul 28–Aug 4 | Run GRPO; Aug 4 honest training result (real curve > rushed clean one) | ⬜ |
| ongoing | Budget/AWS credits management; ping-able for AWS approvals | ⬜ |

## Parallel build items (start now, domain-agnostic, survive a domain rotation)
- [ ] Orchestration layer against stubbed loop interface: concurrency, cost tracking + budget caps, resume-on-failure, artifact layout in `runs/`
- [ ] GRPO training loop **fresh** (NOT reusing BrickedUp code): rollout collection vs serving endpoint, reward mapping, group-relative advantage, checkpointing + eval cadence
- [ ] Proof-of-life: hill-climb an open model +2–3% on an existing public presentation benchmark before pointing at our env

## Memo criteria (workplan, Faisal workstream)
1. Agentic/coding capability (multi-turn bash + file editing + long procedural skill doc — agentic benchmarks >> chat quality)
2. Land-in-band: too weak = 0/8 (no signal), too strong = saturates (no improvement story)
3. GRPO feasibility on our budget: VRAM, rollout throughput at k=8 group sizes, training-step cost
4. Context: ≥256k, ideally ~1M (Michael's harness measurement — QA loop runs 14–21 code-exec calls/deck)
5. License cleanliness for a commercial demo
6. Legibility to Anthropic (well-regarded model)
7. Pre-declared fallback triggers (e.g., primary scores 0/8 on Jul 24 baseline → switch, don't fight it)

## Blockers to raise with Michael
- [ ] **Repo access**: `github.com/michael-xu25/powerbench` 404s for `faisal-nabulsi` — need collaborator invite
- [ ] **Slack**: the Claude bot can't read #pptx-benchmarks or #faisal (not a member) — invite it, or paste the benchmarks list; needed to pick the hill-climb benchmark from "our" list rather than my reconstruction of public ones

---

## 2026-07-26 — grader hand-audit + lr bracketing (the real lessons)

### Faisal's hand-audits found 5 GRADER BUGS that no unit test caught
Reviewing rendered slides against their scores exposed:
1. **Per-shape dilution** — collision/overflow divided each defect by the *total area of all
   shapes*, so an image covering a text box scored 0.96. Fixed: measure each defect against
   **that shape's own area**.
2. **Paragraph-level fonts missed** — textfit read only `run.font.size`, missing
   `paragraph.font.size`. 64pt text in a 144pt box "fit" because it assumed 18pt. This single
   bug caused the whole "text doesn't fit" category (ID 62e972 etc).
3. **`word_wrap=False` unmodeled** — unwrapped text runs off sideways; never detected.
4. **Metric-level dilution** — the weighted mean let ONE catastrophic defect be averaged away
   by five good metrics (content fully off-screen still scored 0.65). Fixed with a **soft-min
   gate**: score ×= 0.55 + 0.45·min(critical metrics). Smooth, so group variance survives.
5. **No aspect-ratio metric at all** — 24/30 slides were 4:3 (python-pptx default) and scored
   0.89–0.98. Added; 16:9→1.0, 4:3→0.19.
Validation that these were *grader* fixes not per-slide tuning: the clean reference deck stayed
at **0.99 unchanged** while padded fell 0.56→0.18 (gate margin widened 0.31→0.81).

### THE BIG LESSON: a metric only teaches if rollouts VARY on it
Adding `aspect` to the reward did **not** make the model learn 16:9 (8%→0% compliance).
Why: setting 16:9 is one discrete line the model never varies, so all 8 rollouts in a group
score identically on aspect → zero relative advantage → **zero gradient**. It was pure dead
weight, subtracting ~0.15 uniformly and diluting metrics that *do* vary.
**Fix: put 16:9 in the SYSTEM PROMPT** (a stated canvas constraint, not the work) and drop
aspect's weight 0.18→0.06. Result: held-out aspect **0.093 → 0.689**, valid decks 0.489 → 0.689.
Generalizes: before adding any reward term, check it varies across rollouts.

### Learning rate is now BRACKETED empirically
| run | prompts | lr | held-out | verdict |
|---|---|---|---|---|
| geo8 | detailed | 1e-6 | 0.626 → 0.606 → 0.605 | **flat — under-trained** (KL~0, policy barely moved) |
| highlevel1 | high-level | 3e-6 | 0.475 → **0.305** | **DEGENERATED — length hacking** |
| highlevel2 | high-level | **2e-6** | baseline 0.502 | running |
highlevel1's failure mode is textbook and worth keeping: response length **4551→7792→9078**,
clip 1.6%→6.3%, valid decks →49%. The model rambled until output truncated → broken code →
reward 0. Guarded in v2 with `max_response_length` 14336→8192 (watch: clip is now 30%, i.e. the
cap is tight — truncation pressure toward concise code, which is desirable, but it means ~30%
of rollouts score 0 for being cut off rather than for bad layout).

### Ops rules learned the hard way
- **Kill by GPU ownership, not name.** `pkill -f main_ppo`/`ray stop` misses vLLM's
  `VLLM::Worker_TP`/`VLLM::EngineCore` children, which orphan and hold GPU memory (found procs
  5.6h old). Loop `nvidia-smi --query-compute-apps=pid | kill -9` until memory ≈0, plus
  `rm /dev/shm/cuda.shm.* /tmp/ray/*`.
- **`enable_prefix_caching=False`** — vLLM deadlocks in EngineCore init on the Qwen3.6 GDN
  hybrid ("Mamba cache mode 'align'") with prefix caching on. Cost 3 dead runs (geo5/6/7)
  before diagnosis; the minimal 8-GPU NCCL all-reduce PASSED, proving the box was healthy.
- **Console held-out numbers are stdout-buffered** and lag hours; **read W&B
  `scan_history()`** instead (works even when the run shows "crashed" — that's a lost heartbeat).
- **Slides are ephemeral** — 112 decks lived only on the box. Now mirrored to
  `Desktop/startup/powerbench-faisal/slides/{geo8_run,highlevel_run}/` (deck + code + meta each).

### Deliverables
- `slide-gallery.html` / artifact `4e5d27eb` — every rollout: slide, reward, per-metric, code.
- `slide-gallery-by-run.html` / artifact `a2c471dd` — **split by run** with config comparison.
- `reward-review.pdf`, `pptx-hillclimb-design.pdf` on Desktop.
- Grader + reward code mirrored in `reward/` (survives box loss).

---

## 2026-07-30 — blind study rater 1 (Faisal) unblinded vs grader

Faisal graded all 48 items (`~/Desktop/slide-grades-faisal.json`). Full analysis in
`DO-NOT-SHARE-faisal-vs-grader.md` (gitignored — **do NOT share until Michael + Cara grade**;
gitignore broadened to `DO-NOT-SHARE*` since `blind_study/` is tracked in the public repo).

**Headline: grader ranks our own model's decks at Spearman ≈ 0.00 (n=16) — the reward can't
order same-policy rollouts, which is exactly the signal GRPO consumes.** Human source ranking:
Fable-agentic 9.0 > Fable-ST 7.9 > PPTArena human 7.4 > **ours 6.6 (last)**; grader inverts
this (scores ours 0.72 > human decks 0.60). What's validated: collision/overflow (overlap-
flagged 0.52 vs 0.71 unflagged; clipped 0.61 vs 0.71). What's broken: **empty-content hole
confirmed empirically** — human-flagged "empty" items grader-average 0.716 vs 0.690 unflagged
(item_[redacted] empty chart 0.97, item_[redacted] "no info" 0.94, item_[redacted] 0.91; 3 of 4 worst are OUR decks →
the trained policy already exploits it). Aspect underweighted vs human judgment (item_[redacted]:
human 2/10, grader 0.70). PPTArena decks are off-distribution for the grader (item_[redacted] dense
poster 0.13 vs human 8) — never quote the grader cross-corpus.

**Gate before next GRPO run:** (1) content-existence term that sees non-text ink (chart data
series, image/shape fills, near-blank render penalty); (2) new gate fixtures: empty-chart +
monochrome-blank (old gate passed while this hole was open); (3) v2 render-grader still owes
the 64-concurrent-soffice load test. Caveat: n=1 rater, restricted range within ours_s30 —
await Michael/Cara before final calls. Box `<GPU_BOX_IP>` alive + idle (8×A100, 0 MiB).

### 2026-07-30 (cont.) — gate items 1–3 DONE. Discovered geometric_reward is already v2.

**Key discovery:** the repo's `reward/geometric_reward.py` is already **v2 (2026-07-29)** — it
added a render-based `content` term (`detail_cov`, rho +0.544) + `clipping`, built against the
45 blind-graded audit decks. The blind-study answer key I analyzed was **v1 (Jul 28)**, before
that term. So my "empty-deck hole" finding was against v1; v2 was unvalidated on these cases.
Core files (`geometric_reward.py`, `render_metrics.py`, `geom_shapes.py`) are **byte-identical
box↔local**, so no version-split.

**(2) Fixtures added + (1) verified — v2 half-closed the hole, I closed the rest:**
- Added two adversarial fixtures to `make_fixtures.py` + wired as ENFORCED gate checks in
  `gate_test.py`: **`empty_chart`** (title + all-zero-data chart — its axes/gridlines/legend
  are dense edges = high `detail_cov`; faithful to blind item_[redacted] "the chart is empty") and
  **`sparse`** (title + one line, monochrome; faithful to item_[redacted]/35).
- **First gate run (v2 as-is):** `sparse` correctly caught (content 0.00, score 0.386 — a
  truly bare slide has almost no edges, so the low end works). But **`empty_chart` scored
  0.7430 vs good 0.7677 — margin 0.025, essentially TIED**, with **content=0.98**: detail_cov
  is fully fooled by chart chrome. The empty-chart exploit was still a ~0.74 free win → open
  hole, exactly item_[redacted]. `detail_cov` measures rendered EDGES; chrome and bars are both edges,
  so pixels can't see it and geometry can't (chart = one well-placed box). Only the DATA can.
- **FIX (render-free, in `geometric_reward.py`):** new `score_chart_content()` reads
  `shape.chart.plots[].series[].values` via python-pptx; a chart is "empty" if no series has a
  value not in (None, 0). New `chart_ok` = fraction of charts with real data (=1.0 when no
  charts → text slides untouched, zero dead weight). Two hooks: (a) when charts are all empty,
  **cap render `content` to floor 0.15** (chrome cannot certify content); (b) **`chart_ok`
  joins the critical soft-min set**, so an empty chart trips the catastrophe gate.
- **Re-gate: `empty_chart` 0.7430 → 0.3348** (margin below good 0.025 → 0.433; now sits with
  empty 0.06 / padded 0.08 / sparse 0.39). **`good` UNCHANGED at 0.7677** and every other
  fixture identical — zero collateral. `grader_tests.py` all pass (finite, deterministic
  0.9036, 29.8 ms/deck); `compute_score` fixed-key schema intact (no raise with new key).

**(3) 64-concurrent render load test: GO** (`reward/loadtest_render.py`, mirrored local; ran
`/tmp/loadtest.log` on box). Worst case = 64 truly-concurrent soffice renders of real PPTArena
decks. **Wall-clock 75.5s = 3% of a 37-min step; 64/64 succeeded** (per-render private-profile
fix kills the geo2 race); **peak load 37 on 240 cores, peak 37 GB of 1771 GB, worst
responsiveness ping 192 ms** (wedged threshold 1000). The geo2 unreachable-box failure is
closed ON THIS BOX. **Residual caveat (honest):** test ran with GPUs idle / no concurrent
training; real training adds actor CPU-offload pressure. But render footprint is tiny (37
load / 37 GB) against this box's 240c / 1771 GB, so headroom should absorb it — watch host
load on the first real step. Batched path (`score_render_batch`) not even needed; naive
concurrent is already 3%.

**NET: all three pre-train gates cleared. The reward can now tell empty charts, blank slides,
padded walls, and monochrome-sparse decks from a good deck, and the render grader is
host-safe.** Still owed before declaring victory: Michael + Cara's blind grades (turn n=1 →
n=3), then a training run watching held-out + response-length + the new `chart_ok`/content
metrics. Reward analysis (v1-based, keep private): `DO-NOT-SHARE-faisal-vs-grader.md`.

### 2026-07-31 — next run STAGED + smoke-tested; AI-judge experiment

**Launch-ready next run** (`reward/run_next_geo.sh` + `launch_next.sh`, deployed to box
`~/powerbench/`): high-level prompts, lr 2e-6 (1e-6 flat / 3e-6 length-hacked → midpoint),
max_response_length 8192 (validated: Jul-28 run clip 7.8%, len stable), v2+chart-fix reward,
30 steps, save_freq=10/keep=2 (FSDP2 save patch present at transformer_impl.py:768), val_before_
train for the honest baseline. `singleturn_geometric_reward.py` now also logs content/clipping/
chart_ok to W&B. `run_next_geo.sh` is env-parameterized (N_ROLLOUT/TRAIN_BSZ/TOTAL_STEPS/…) so
the smoke shares ONE config source. `launch_next.sh` = hardened one-command launch (kill by
GPU-owner + clear IPC/Ray + verify GPUs free, then setsid). `./launch_next.sh` real; `--smoke` 1-step.

**Smoke test — substantive milestones PASSED, but I botched the clean-exit observation.** The
1-step smoke (n=2, val off, save_freq=1) proved end-to-end: vLLM init cleared the GDN prefix-
caching deadlock, GDN Triton compile completed, **step 1 completed, and the checkpoint SAVED
COMPLETELY** — all 8 ranks wrote model+optim+extra_state (24 files, 306 GB, verified on disk at
03:20:45), which is the exact FSDP2-save path that killed geo2. ⚠️ **Process mistake:** I set an
auto-cleanup watcher that timed out and KILLED the run at 03:38 during post-save teardown, so
there is NO clean `NEXT_GEO_EXIT=0` and the `EngineDeadError`/"Error executing job" in the log
is my kill, not a crash. Unexplained ~18-min gap between save-complete and kill (slow teardown
vs hang — undetermined; happens AFTER the final checkpoint is safely on disk, so the deliverable
survives regardless). For the real run: do NOT auto-kill; watch the final step live. Box cleaned
+ idle after (0 MiB, ckpt_smoke removed, 19T free).

**AI-judge experiment (answered "would 3 agents give good data?").** 3 Claude agents blind-
scored the same 48 slides on the human rubric. Judges track Faisal at Spearman **+0.63** (grader
+0.21), and **+0.56 on our own decks where the grader was ~0.00** — they provide the ranking
signal the grader can't, and are independent of it (judge-vs-grader +0.21–0.32, they catch the
empties). BUT same-family so inter-judge +0.95 (3 Claudes ≈ 1; need cross-vendor for real
diversity), calibration differs from the human, and they can't be the per-rollout RL reward.
Verdict: strong SUPPLEMENT / grader-audit tool at scale, NOT a replacement for the human anchor.
Full write-up appended to `DO-NOT-SHARE-faisal-vs-grader.md`.

**Grading instrument (`GRADE-48-SLIDES.html`) hardened** (systematic-debugging pass): the
`prompt()` name-popup that blanked for Cara → in-page name gate; a SECOND blank-screen cause
found + fixed (unguarded `localStorage` throws on file:// Safari/private mode → aborts script)
via a never-throw storage wrapper + in-memory fallback + storage-off warning; export can no
longer silently lose grades (try/catch → paste-backup overlay). All verified in-browser. Three
review bots in Slack flagged it: one (kathryne) hallucinated ("it's marketing boilerplate" —
false, 48 slides verified); charizard's real find = completion certifies `overall` only
(number keys set overall, subscores click-only) — true but scoped (core signal safe, Faisal's
data 48/48 complete); its "order confounded with source" is moot (sources interleaved, max
run 2). Did NOT let bots auto-patch/PR the live instrument.
