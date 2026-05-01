# Example: `coding` — Build a feature in an existing app (OAuth 2.0)

The classic long-horizon coding task. Implement OAuth 2.0 Authorization Code Flow with PKCE in an existing FastAPI app. Tests, build, security scan all gate progress.

**Why this is long-horizon**: 4–12 hours. ~50 atomic sub-goals (auth endpoint, PKCE handler, token store, refresh, revocation, scope middleware, test for each, docs, integration test, …). Anthropic's two-agent pattern is the natural fit: an Initializer creates the goal graph; per-sub-goal sessions implement and test.

## Strategy: Sequential sub-goals (Anthropic pattern)

The Initializer reads the prompt and produces a `goals.json` with 40–80 leaves like:

```
g.root → Implement OAuth 2.0
  ├── g.auth → Authorization endpoint with PKCE
  │     ├── g.auth.handler
  │     ├── g.auth.pkce
  │     └── g.auth.test
  ├── g.token → Token exchange + refresh + revocation
  │     ├── g.token.exchange
  │     ├── g.token.refresh
  │     ├── g.token.revoke
  │     └── g.token.test
  ├── g.middleware → Scope validation
  ├── g.tests → Integration tests
  └── g.docs → API documentation
```

Each leaf is one bounded session.

## Validators

| Validator | When | What |
|---|---|---|
| `tests_pass` | after every session | `pytest tests/ -k oauth --tb=short` |
| `min_test_count` | after every session | ≥ 1 test file in `tests/` |
| `build_works` | after every session | `uvicorn app.main:app` boots |
| `security_scan` | every 5 sessions | `bandit` finds 0 critical |

## Running

```bash
horizonx run examples/coding/
```
