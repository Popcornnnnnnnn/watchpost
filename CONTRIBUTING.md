# Contributing to Watchpost

Thank you for helping improve Watchpost (`watchpost-agent`).

## Development Setup

```bash
git clone https://github.com/Popcornnnnnnnn/watchpost.git
cd watchpost
uv sync --all-extras
```

Run the test suite and linter before submitting changes:

```bash
uvx ruff check --select E9,F src tests
uv run pytest -q
uv build
```

## Architecture & Licensing Rules

1. **Apache-2.0 Core:** Watchpost is licensed under the Apache License, Version 2.0 (`LICENSE`).
2. **External Copyleft Boundary:** Never import copyleft-licensed packages or unofficial desktop bridges directly into `src/watchpost/`. Out-of-process bridges communicate solely over the JSON stdio `watchpost.sdk` protocol (`watchpost plugins scaffold`).
3. **Read-Only Work Channels by Default:** Channel adapters must default to passive read/triage and never send replies to external channels unless explicitly triggered by the owner's local `"go"` flow or copy action.
4. **No Live User Secrets in Repo:** Tests and fixtures must run offline using `MockAgentBackend`, `SimulatorAdapter`, synthetic phone/user fixtures, or `httpx.MockTransport`.

## Pull Requests

- Keep each PR scoped cleanly with tests covering new behaviors or bug fixes.
- Ensure `ruff`, `pytest`, and `uv build` pass cleanly on your branch.
