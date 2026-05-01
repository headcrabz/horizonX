# Example: `autoresearch` — Karpathy autoresearch wrapped in HorizonX

Wraps [karpathy/autoresearch](https://github.com/karpathy/autoresearch) — autonomous overnight LLM hyperparameter exploration — with HorizonX's **Ralph loop** strategy.

**Why this is long-horizon**: a single run is overnight (~10h). The agent makes ~120 sequential decisions. Each iteration's metric (val_bpb) feeds the next. A crash at iteration 80 should not lose iterations 1–79. The mutable surface (`train.py`) is exactly one file; everything else must remain unchanged. Spin detection catches the agent when it stops making real metric progress.

## What HorizonX provides on top of plain autoresearch

| Plain autoresearch | + HorizonX |
|---|---|
| Single bash loop, fragile to crashes | Durable run state; resume from last successful iteration |
| Agent could touch any file | `mutable_paths: ["train.py"]` enforced via `git diff` post-iteration |
| No early stopping | `metric_plateau` predicate halts when val_bpb flatlines |
| No spin detection | Detects same-tool-call repetition + score plateau |
| No HITL | Pauses for operator on metric regression beyond threshold |
| No audit trail | Every decision in `decisions.jsonl`; full trajectory in DB |

## Directory layout

```
autoresearch/
├── task.yaml          # HorizonX task spec
├── prepare.py         # (from Karpathy) data prep / tokenizer / eval — IMMUTABLE
├── train.py           # (from Karpathy) MUTABLE — the agent edits this
├── program.md         # (from Karpathy) agent instructions
├── pyproject.toml     # uv-managed deps
└── README.md          # this file
```

## Getting started

```bash
# Clone Karpathy's autoresearch into this folder
git clone https://github.com/karpathy/autoresearch.git src/

# One-time data prep (per Karpathy's instructions)
cd src && uv sync && uv run prepare.py && cd ..

# Run via HorizonX
horizonx run examples/autoresearch/

# Watch live (separate terminal)
horizonx list
horizonx watch <run_id>
```

## Expected outcome

After ~10h overnight run:
- 80–120 iterations attempted
- Best val_bpb improves from baseline by ~20–40%
- `decisions.jsonl` shows the agent's hypothesis history
- `progress.md` is the experiment log
- Foreign-file changes were rejected automatically
