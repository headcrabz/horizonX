# Example: `kernel_optimization` — CUDA / Triton kernel optimization

Long-horizon optimization of a GPU kernel. Agent rewrites a Triton kernel iteratively, the harness measures throughput on a fixed benchmark, keeps improvements.

**Why this is long-horizon**: kernel tuning is an enormous search space (block sizes, fusion choices, memory layout, instruction selection). Each compile + benchmark cycle is ~30s–2min. To explore 100+ variants takes hours. Most variants are no improvement; correctness must be guarded at every step (`torch.allclose` against a reference).

## Strategy: Ralph loop

Same shape as autoresearch — fixed time budget, mutable surface is the kernel file, metric is throughput, baseline is the existing kernel.

## Critical guard

Unlike autoresearch's val_bpb, **correctness is non-negotiable here**. Every iteration runs `torch.allclose(out, reference, rtol=1e-3, atol=1e-3)` first. A faster but wrong kernel is rejected, no exceptions.

## Validators

| Validator | What | If fails |
|---|---|---|
| `correctness` | `python verify.py` checks output matches reference | discard iteration |
| `throughput` | `python bench.py` reports GFLOPS | metric for keep/discard |
| `compiles` | kernel compiles cleanly | discard iteration |

## Running

```bash
horizonx run examples/kernel_optimization/
```
