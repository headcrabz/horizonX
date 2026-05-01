# Example: `data_analysis` — Long-horizon data analysis project

Comprehensive data analysis from raw data to executive-ready report. Includes EDA, hypothesis testing, modeling, interpretation, and a written report.

**Why this is long-horizon**: real data analysis projects are 2–8 hours. The agent must explore, form and test hypotheses, iterate on modeling decisions, and write a coherent narrative. Each stage informs the next (you can't test a hypothesis you haven't formed). Strong validators prevent the "agent runs notebook cells but produces no insight" failure mode.

## Strategy: Sequential sub-goals

Goal graph:
- `g.eda` — exploratory data analysis with ≥3 visualizations
- `g.hypotheses` — formulate ≥3 testable hypotheses based on EDA
- `g.tests` — statistical tests of each hypothesis
- `g.model` — predictive model if applicable, with interpretability
- `g.synthesis` — narrative report with findings, caveats, recommendations
- `g.deliverable` — final notebook executes top-to-bottom with no errors

## Validators

| Validator | What |
|---|---|
| `notebook_executes` | `jupyter nbconvert --execute` |
| `min_visualizations` | grep for `plt.show()` count ≥ 3 |
| `report_present` | `report.md` exists with required sections |
| `claims_have_evidence` | LLM-judge: every claim cites data |

## Running

```bash
horizonx run examples/data_analysis/
```
