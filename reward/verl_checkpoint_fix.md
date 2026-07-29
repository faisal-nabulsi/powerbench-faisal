# verl checkpoint fix — FSDP2 CPUOffloadPolicy save crash

## Symptom
With `actor_rollout_ref.actor.fsdp_config.offload_policy=True` (FSDP2 CPUOffloadPolicy),
`save_checkpoint` dies with:

    RuntimeError: Attempted to set the storage of a tensor on device "cpu"
    to a storage on different device "cuda:0"
    (torch/nn/modules/module.py _save_to_state_dict)

Workaround-by-memory does NOT work: setting `offload_policy=False` makes the save succeed but
raises actor peak memory from ~35 GB to ~68 GB per GPU, which OOMs alongside colocated vLLM.
Confirmed OOM at rollout TP=4 (vLLM ~19 GB) and TP=8 (vLLM ~11 GB) — four separate attempts.
The problem is a device-placement BUG, not a memory budget.

## Root cause
`verl/workers/engine/fsdp/transformer_impl.py :: save_checkpoint` calls
`load_fsdp_model_to_gpu(self.module)` — i.e. `model.to(device)` — right before handing off to
the checkpoint manager. Under FSDP2 CPUOffloadPolicy that leaves the module half-moved, and
the subsequent `state_dict()` raises. verl already guards its *weight-sync* path
(`get_per_tensor_param`) with exactly this condition; the *save* path was missing it.

## Fix (one guard)
```python
# verl/workers/engine/fsdp/transformer_impl.py, in save_checkpoint
if (self._is_offload_param or origin_module_device == "cpu") and not getattr(
    self, "_uses_fsdp2_cpu_offload_policy", False
):
    load_fsdp_model_to_gpu(self.module)
```

This is the **official upstream fix**:
- **PR #6604** "[fsdp] fix: do not manually move model to GPU" — merged to main 2026-06-05,
  commit `a539474772b2ebe1feeafa709b913f2a658de15f`
- **PR #7077** — cherry-pick into `release/v0.8.0`, merged 2026-07-24. Upstream validated it on
  Qwen3.5-35B-A3B with `offload_policy=True, param_offload=True, optimizer_offload=True`.
- **PR #7103** applies the same guard to the VeOmni engine, citing "#5995 / #6604".

**Why we hit it:** the fix landed 4 days AFTER the v0.8.0 tag/PyPI release we are pinned to,
and no v0.8.1 exists. It cannot be obtained by bumping a version — cherry-pick, track
`release/v0.8.0`, or patch locally (what we did).

## Verified on our box
`offload_policy=True` (35 GB actor, no OOM) + `save_freq=1` →
`ckpt/global_step_1/actor/model_world_size_8_rank_{0..7}.pt`, 17 files, 102 GB, save logged
per rank. Both requirements satisfied simultaneously for the first time.

## Getting a usable HuggingFace model
The sharded `.pt` files are not loadable directly. verl ships a merger (it IS present in our
install — an earlier negative check was a bad import test):

```bash
python -m verl.model_merger merge \
  --backend fsdp \
  --local_dir  /home/ubuntu/powerbench/ckpt_v3/global_step_30/actor \
  --target_dir /home/ubuntu/powerbench/hf_export
# and to validate the result:
python -m verl.model_merger test --backend fsdp --local_dir ... --ref_model ...
```

Runs offline on CPU, so it does not compete with training for GPU memory.

## Notes / dead ends
- `checkpoint_engine` (naive/nccl/nixl) is trainer→rollout weight transfer, NOT disk saving.
- `async_save` is implemented for Megatron only, not FSDP.
- Disk: each checkpoint is ~102 GB, so set `trainer.max_actor_ckpt_to_keep` (we use 2).
