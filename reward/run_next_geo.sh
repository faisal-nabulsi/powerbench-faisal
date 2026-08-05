#!/usr/bin/env bash
# ==========================================================================================
# THE NEXT GRPO RUN — single-turn geometric hill-climb on Qwen3.6-27B, v2+chart-fix reward.
# Launch-ready 2026-07-30. Launch via ./launch_next.sh (which cleans GPUs first). Do NOT run
# this directly unless the GPUs are already verified free.
# ==========================================================================================
#
# WHAT'S NEW vs the last real run (cd62xxwx, Jul 28, train reward 0.82 / 30 steps):
#   reward   pre-v2 (empty deck + empty CHART both farmed a free ~0.7-0.97)
#            -> v2 + chart fix: content term (detail_cov) catches blank slides; new render-free
#               chart_ok reads chart series data so an all-empty chart can no longer certify
#               content via its axes/gridlines. Gate: empty_chart 0.74->0.33, good unchanged.
#            The Jul-28 0.82 was partly the empty-deck exploit; this run's numbers are honest.
#   logging  content / clipping / chart_ok now logged per step (watch for farming early).
#
# CONFIG RATIONALE (all empirically bracketed — see TODO.md 2026-07-26):
#   prompts  HIGH-LEVEL (data/slidesbench_highlevel) — Faisal's call: the realistic, hard task.
#            Detailed prompts are "easy" (base already ~0.63); high-level baseline ~0.50 has
#            the headroom a training story needs.
#   lr       2e-6  — 1e-6 was FLAT (under-trained, KL~0); 3e-6 LENGTH-HACKED (resp 4.5k->9k,
#                    degenerated). 2e-6 is the bracket midpoint.
#   max_resp 8192  — 14336 ran away; 8192 held the Jul-28 run at clip 7.8%, length stable ~5.4k.
#   KL       0.01, loss_agg token-mean (no length bias), invalid==0 never negative.
#   steps    30, save at 10/20/30 keep last 2 (checkpoint patch verified on box; ~102GB each,
#            19TB free). ~35 min/step => ~17-18h. val_before_train=True for the honest "before".
#
# BOX RULES (learned the hard way — see TODO.md):
#   * unset PYTORCH_CUDA_ALLOC_CONF (expandable_segments breaks vLLM cumem sleep). Moot at
#     free_cache_engine=False but the rule stands.
#   * enable_prefix_caching=False (vLLM EngineCore deadlocks on the Qwen3.6 GDN 'align' path).
#   * free_cache_engine=False + max_model_len=32768 + gpu_mem_util=0.25 => vLLM resident, no
#     sleep/wake cumem conflict.
#   * GDN needs ~15-25 min Triton JIT compile on first forward (GPU pinned, log silent) — that
#     is NOT a hang. Gallery deck count is the real progress signal, not log lines.
set -xeuo pipefail
cd ~/powerbench
export PATH="$HOME/powerbench/.venv/bin:$PATH"
unset PYTORCH_CUDA_ALLOC_CONF

# --- knobs: default to the REAL run; the smoke overrides these via env (one source of truth) --
N_ROLLOUT=${N_ROLLOUT:-8}
TRAIN_BSZ=${TRAIN_BSZ:-8}          # real_train_batch_size = TRAIN_BSZ * N_ROLLOUT must be div by 8
MINI_BSZ=${MINI_BSZ:-8}            # <= TRAIN_BSZ, in PROMPT units
TOTAL_STEPS=${TOTAL_STEPS:-30}
SAVE_FREQ=${SAVE_FREQ:-10}
TEST_FREQ=${TEST_FREQ:-5}
VAL_BEFORE=${VAL_BEFORE:-True}
EXP_NAME=${EXP_NAME:-next-geo-27b-v2chartfix}
CKPT_DIR=${CKPT_DIR:-$HOME/powerbench/ckpt_next}
export GEO_GALLERY=${GEO_GALLERY:-$HOME/powerbench/agentic/gallery_next}
rm -rf "$GEO_GALLERY"; mkdir -p "$GEO_GALLERY"

MODEL_PATH=~/powerbench/models/Qwen3.6-27B
TRAIN_FILE=~/powerbench/data/slidesbench_highlevel/train.parquet
TEST_FILE=~/powerbench/data/slidesbench_highlevel/test.parquet
REWARD=~/powerbench/agentic/singleturn_geometric_reward.py

python3 -m verl.trainer.main_ppo \
  algorithm.adv_estimator=grpo \
  algorithm.use_kl_in_reward=False \
  data.train_files="$TRAIN_FILE" \
  data.val_files="$TEST_FILE" \
  data.train_batch_size="$TRAIN_BSZ" \
  data.val_batch_size=45 \
  data.max_prompt_length=1536 \
  data.max_response_length=8192 \
  data.filter_overlong_prompts=True \
  data.truncation=error \
  data.shuffle=True \
  custom_reward_function.path="$REWARD" \
  custom_reward_function.name=compute_score \
  actor_rollout_ref.model.path="$MODEL_PATH" \
  actor_rollout_ref.model.trust_remote_code=True \
  +actor_rollout_ref.model.override_config.attn_implementation=sdpa \
  actor_rollout_ref.model.use_remove_padding=False \
  actor_rollout_ref.model.enable_gradient_checkpointing=True \
  actor_rollout_ref.actor.optim.lr=2e-6 \
  actor_rollout_ref.actor.ppo_mini_batch_size="$MINI_BSZ" \
  actor_rollout_ref.actor.ppo_micro_batch_size_per_gpu=1 \
  actor_rollout_ref.actor.loss_agg_mode=token-mean \
  actor_rollout_ref.actor.use_kl_loss=True \
  actor_rollout_ref.actor.kl_loss_coef=0.01 \
  actor_rollout_ref.actor.kl_loss_type=low_var_kl \
  actor_rollout_ref.actor.entropy_coeff=0 \
  actor_rollout_ref.actor.use_torch_compile=False \
  actor_rollout_ref.actor.strategy=fsdp2 \
  actor_rollout_ref.actor.use_dynamic_bsz=False \
  actor_rollout_ref.actor.fsdp_config.fsdp_size=8 \
  actor_rollout_ref.actor.fsdp_config.param_offload=True \
  actor_rollout_ref.actor.fsdp_config.optimizer_offload=True \
  actor_rollout_ref.actor.fsdp_config.offload_policy=True \
  actor_rollout_ref.actor.fsdp_config.reshard_after_forward=True \
  actor_rollout_ref.actor.fsdp_config.entropy_checkpointing=True \
  actor_rollout_ref.actor.entropy_from_logits_with_chunking=True \
  actor_rollout_ref.ref.strategy=fsdp2 \
  actor_rollout_ref.ref.log_prob_micro_batch_size_per_gpu=1 \
  actor_rollout_ref.ref.fsdp_config.param_offload=True \
  actor_rollout_ref.ref.fsdp_config.offload_policy=True \
  actor_rollout_ref.ref.fsdp_config.reshard_after_forward=True \
  actor_rollout_ref.ref.entropy_from_logits_with_chunking=True \
  actor_rollout_ref.ref.use_torch_compile=False \
  actor_rollout_ref.rollout.name=vllm \
  actor_rollout_ref.rollout.log_prob_micro_batch_size_per_gpu=1 \
  actor_rollout_ref.rollout.tensor_model_parallel_size=4 \
  actor_rollout_ref.rollout.gpu_memory_utilization=0.25 \
  actor_rollout_ref.rollout.free_cache_engine=False \
  actor_rollout_ref.rollout.enable_prefix_caching=False \
  actor_rollout_ref.rollout.max_model_len=32768 \
  actor_rollout_ref.rollout.n="$N_ROLLOUT" \
  actor_rollout_ref.rollout.val_kwargs.n=1 \
  actor_rollout_ref.rollout.enforce_eager=True \
  trainer.critic_warmup=0 \
  trainer.logger=['console','wandb'] \
  trainer.project_name=powerbench-geometric \
  trainer.experiment_name="$EXP_NAME" \
  trainer.default_local_dir="$CKPT_DIR" \
  trainer.n_gpus_per_node=8 \
  trainer.nnodes=1 \
  trainer.val_before_train="$VAL_BEFORE" \
  trainer.save_freq="$SAVE_FREQ" \
  trainer.max_actor_ckpt_to_keep=2 \
  trainer.test_freq="$TEST_FREQ" \
  trainer.total_epochs=10 \
  trainer.total_training_steps="$TOTAL_STEPS" \
  trainer.resume_mode=disable
echo "NEXT_GEO_EXIT=$?"
