# Example: `autotrain` — End-to-end ML training pipeline

A long-horizon ML pipeline: data quality check → feature engineering → model selection → hyperparameter tuning → evaluation → deployment smoke test. Each stage is a sub-goal with its own validators.

**Why this is long-horizon**: 4–12 hours of compute and decisions, with strong stage dependencies (you can't tune what you haven't trained, you can't deploy what hasn't passed eval). A failure at hour 6 shouldn't restart from hour 0. Each stage has domain-specific validators (AUC threshold, leakage check, deploy smoke test).

## Strategy: Sequential sub-goals (Anthropic pattern)

Goal graph:
- `g.root` → finished pipeline
  - `g.data_check` — verify data quality, schema, no leakage
  - `g.features` — feature engineering (≥10 features)
  - `g.split` — train/val/test split with stratification
  - `g.candidates` — train 3 candidate models
  - `g.tune` — hyperparameter tune the best candidate
  - `g.eval` — final evaluation (AUC, calibration, fairness)
  - `g.deploy_smoke` — package + smoke test inference

## Validators per stage

| Sub-goal | Validator | Threshold |
|---|---|---|
| `g.data_check` | `python check_data.py` | exit 0 |
| `g.features` | feature count + leakage check | ≥10 features, no leakage |
| `g.candidates` | 3 model pickles exist | files present |
| `g.tune` | AUC on val set | ≥ 0.78 |
| `g.eval` | AUC on test set | ≥ 0.75 |
| `g.deploy_smoke` | `curl /predict` returns 200 | exit 0 |

## Running

```bash
horizonx run examples/autotrain/
```

The initializer session will create the goal graph from the prompt; subsequent sessions handle each sub-goal in order.
