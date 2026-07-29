#!/usr/bin/env bash
# Single-turn GRPO hill-climb on Qwen3.6-27B with the GEOMETRIC (ungameable) reward.
#
# WHY SINGLE-TURN: multi-turn + thinking-on + GRPO is the most collapse-prone configuration
# documented (arXiv 2512.17008, EACL 2026), and standard stabilizers barely help. We changed
# the reward; running it in the least-stable setting at the same time would make a failure
# uninterpretable. Prove the reward climbs here first; the agentic harness is built and
# waiting to be switched back on.
#
# WHAT CHANGED vs the runs that failed (grpo5-grpo8):
#   reward      text-coverage (presence-based, padding pays)  ->  geometric (padding is punished)
#   invalid     -0.5 penalty (made every group degenerate)    ->  0.0, never negative
#   lr          5e-6                                          ->  1e-6
#   loss_agg    (default)                                     ->  token-mean, explicit (no length bias)
#   KL          kept                                          ->  kept (AeSlides retains KL to stop collapse)
#
# NOTE: do NOT set PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True — it is incompatible
# with vLLM's sleep-mode cumem allocator. (Moot here since free_cache_engine=False, but the
# rule stands for every run on this box.)
set -xeuo pipefail
cd ~/powerbench
export PATH="$HOME/powerbench/.venv/bin:$PATH"
unset PYTORCH_CUDA_ALLOC_CONF
# Save a sample of real rollout decks + renders + scores here so we can review the slides
# the model actually produced (capped per worker inside the reward fn).
export GEO_GALLERY="$HOME/powerbench/agentic/gallery"
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
  data.train_batch_size=8 \
  data.val_batch_size=45 \
  data.max_prompt_length=1536 \
  data.max_response_length=14336 \
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
  actor_rollout_ref.actor.optim.lr=3e-6 \
  actor_rollout_ref.actor.ppo_mini_batch_size=8 \
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
  actor_rollout_ref.rollout.max_model_len=16384 \
  actor_rollout_ref.rollout.n=8 \
  actor_rollout_ref.rollout.val_kwargs.n=1 \
  actor_rollout_ref.rollout.enforce_eager=True \
  trainer.critic_warmup=0 \
  trainer.logger=['console','wandb'] \
  trainer.project_name=powerbench-geometric \
  trainer.experiment_name=highlevel-geo-27b \
  trainer.n_gpus_per_node=8 \
  trainer.nnodes=1 \
  trainer.val_before_train=True \
  trainer.save_freq=-1 \
  trainer.test_freq=3 \
  trainer.total_epochs=10 \
  trainer.total_training_steps=15 \
  trainer.resume_mode=disable
echo "SINGLETURN_GEO_EXIT=$?"
