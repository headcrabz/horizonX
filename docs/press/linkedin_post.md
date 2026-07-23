# LinkedIn Post — HorizonX Launch

---

**Hook option A (incident-led):**

---

A team I know burned $47,000 on a runaway 4-agent LangChain job last quarter.

The agents were spinning in loops — making the same failed API calls, writing the same broken code, retrying the same erroring test — for 11 hours. Nobody noticed until AWS sent the bill.

That kind of story shouldn't exist in 2025.

---

Today I'm open-sourcing **HorizonX** — a production-grade meta-harness for Claude Code, Codex, and OpenHands that makes long-horizon agent runs actually safe to run unattended.

Here's what makes it different from every other "agent framework":

**1. 7-layer spin detection** — ExactLoop, BucketedHash, EditRevert, ScorePlateau, ToolThrashing, SemanticProgress, and a CrossSession layer that catches stagnation across multiple sessions. No other framework has this.

**2. Crash-safe dual-write** — Every tool call, observation, and error is persisted to SQLite WAL before the next action runs. Kill the process at any moment. Resume exactly where you left off using native Claude Code / Codex session IDs.

**3. Budget governance that actually works** — Slack alert at 75% spend. Hard stop at 100%. Per-workspace daily limits. Cost velocity detector fires when your $/min rate doubles twice in succession — before it costs real money. I wired the actual `governor.charge()` call that every other framework leaves disconnected.

**4. Cross-run memory** — Agents write facts to `workspace/knowledge/*.md`. After each session, HorizonX indexes them via FTS5. Future runs get the top-K relevant facts injected into their prompt. Your agent remembers that PyJWT 2.8 broke the macOS arm64 build. It doesn't re-discover it session 7.

**5. Structured goal graph** — Tasks decompose into a DAG. Agents *propose* completion. Validators *accept* (or reject). Status is monotonic: PENDING → IN_PROGRESS → DONE. If an operator says "re-decompose this differently," an LLM restructures the pending goals in-flight.

**6. 8 built-in strategies** — single, sequential, pair, self_critique, decomposition, tree, monitor, ralph. Swap strategies without changing your task config.

**7. Pluggable via entry-points** — Ship a pip package, register `horizonx.agents`, `horizonx.strategies`, or `horizonx.validators`. Your agent works everywhere HorizonX runs.

---

🔬 **Technical details for the engineers:**

- SQLite WAL + single-writer ThreadPoolExecutor (no aiosqlite deadlocks)
- FTS5 virtual table with unicode61 tokenizer for cross-run knowledge retrieval
- Real Slack Block Kit cards via `slack_sdk.web.async_client` with 3-attempt webhook retry
- `pending_runs` table for dashboard-launched runs that survive process restarts
- 277 tests, all passing, no mock DB — integration tests hit real SQLite

---

This started as "we need Claude Code to not die on long tasks." It became a proper harness after studying how Hermes Agent, omnigent, Multica, and Gastown each solved pieces of the problem — and building the version that solves all of them in one place.

If you're running agents in production and losing sleep over runaway costs or context loss, this is what I wish existed six months ago.

⭐ GitHub: github.com/your-org/horizonx
📦 `pip install horizonx`
🐳 `docker compose up` (included)

---

What's the worst production agent incident you've seen? Drop it below — I want to know what to build next.

#AI #MachineLearning #AgentAI #ClaudeCode #OpenSource #Python #MLOps #LLM #SoftwareEngineering

---

**Hook option B (technical-first):**

---

Every "production" agent framework I've tried has the same three bugs:

1. `governor.charge()` is never called — budgets don't actually work
2. Context dies between sessions — agents re-discover the same facts every run  
3. Spin loops go undetected — until the $6,500 AWS bill lands

I spent 3 months fixing all three.

→ HorizonX is now open source. Link in comments.

(Thread below for the technical deep-dive ↓)

---

**Visual caption (for the infographic image):**

> "Built this because I watched a 4-agent job burn $47K overnight with no alerts, no checkpoints, and no way to resume from where it died. HorizonX is what should have been running instead."
