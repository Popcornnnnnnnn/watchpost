# Watchpost Working Agreement

Read [CONTRIBUTING.md](CONTRIBUTING.md) and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) before making substantial changes.

## Standard Verification Gate

Before committing or opening a PR, run Watchpost's linter, test suite, and package build:

```sh
uvx ruff check --select E9,F src tests
uv run pytest -q
uv build
```

## Major-Update Formal State-Machine Check

Before finalizing a major update or any change touching thread states, `pending_go` approval gates, workspace turn locking, or crash recovery (`src/watchpost/engine.py`, `src/watchpost/store.py`, `src/watchpost/backends/`):

1. Run `.agents/skills/verify-thread-lifecycle/SKILL.md` (mirrored at `.claude/skills/verify-thread-lifecycle/SKILL.md`).
2. If covered files changed past the skill's recorded `sync_commit`, update `formal/WatchpostThread.tla` using `maintain-formal-verification-skill` before trusting a pass, and output charts/reports under `/tmp` only.
3. Replay any TLC counterexample against real `WatchpostEngine` + `WatchpostStore` before treating it as a code bug or model drift.

## Licensing & Privacy Boundaries

- Core stays strictly `Apache-2.0` (official APIs, local CLIs, and out-of-process JSON stdio plugins only).
- Keep tests/fixtures offline (`MockAgentBackend`, `SimulatorAdapter`, `httpx.MockTransport`) with zero user secrets or machine-specific paths.
