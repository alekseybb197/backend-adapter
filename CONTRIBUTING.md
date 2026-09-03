# Contributing to backend-adapter

Thank you for your interest in contributing! This document outlines the workflow, code standards, and review process.

## Development Workflow

We use a **feature-branch workflow** with pull requests.

### 1. Create an Issue First

Before starting work, [open an issue](https://github.com/alekseybb197/backend-adapter/issues) describing:
- The bug or feature
- Expected behavior
- Steps to reproduce (for bugs)

This prevents duplicate work and allows discussion before implementation.

### 2. Branch Naming

Create a branch from `main` with a descriptive name (for ongoing work, you can
also keep a long-lived feature branch like `feature/v0.7.1` and commit to it
directly — see the note in the project `CLAUDE.md`):

```bash
git checkout main
git pull origin main
git checkout -b feature/42-add-healthcheck-endpoint
# or
git checkout -b fix/56-memory-leak-in-tracer
# or
git checkout -b docs/update-install-guide
```

Prefix conventions:
- `feature/` — new functionality
- `fix/` — bug fixes
- `docs/` — documentation changes
- `refactor/` — code refactoring without behavior change
- `build/` — build system, packaging, CI
- `test/` — adding or fixing tests

Include the issue number when applicable.

### 3. Make Changes

- Write clean, documented code
- Follow existing code style (enforced by `ruff`)
- Add type hints (enforced by `mypy --strict`)
- Add or update tests for any new behavior

### 4. Local Quality Checks

Before pushing, run:

```bash
# Install dev dependencies
pip install -r requirements-dev.txt

# Linting and formatting
ruff check backend_adapter/ backend-adapter.py
ruff format --check backend_adapter/ backend-adapter.py

# Type checking
mypy --strict --ignore-missing-imports backend_adapter/ backend-adapter.py

# Tests
pytest

# Full CI simulation (optional)
act -j lint-and-typecheck
act -j test
```

### 5. Commit Messages

Follow [Conventional Commits](https://www.conventionalcommits.org/):

```
feat: add multi-backend YAML validation
fix: resolve BrokenPipeError on SSE streaming
docs: update environment variable reference
refactor: extract config parsing to separate module
test: add unit tests for convert.py
build: add pyproject.toml for pip distribution
```

### 6. Open a Pull Request

Push your branch and open a PR against `main`:

```bash
git push -u origin feature/42-add-healthcheck-endpoint
```

**PR Requirements:**
- [ ] Description explains what and why
- [ ] All CI checks pass (lint, typecheck, tests)
- [ ] Tests added/updated for new code
- [ ] Documentation updated if behavior changed
- [ ] CHANGELOG.md updated for user-visible changes

### 7. Review Process

- PRs require **1 approving review** before merge
- Currently, the maintainer (`@alekseybb197`) is the sole approver
- All CI checks must pass (enforced by branch protection)
- Squash-merge is preferred for clean history

## Code Standards

### Python Style
- PEP 8 compliant (enforced by `ruff`)
- Maximum line length: 100 characters
- Double quotes for strings
- Type hints on all public functions

### Type Checking
- `mypy --strict` must pass with zero errors
- Use `# type: ignore[error-code]` sparingly and only with comment explaining why

### Testing
- Unit tests in `tests/`
- Name tests descriptively: `test_<function>_<scenario>`
- Use pytest fixtures for shared setup
- Aim for >80% coverage on new code

## Getting Help

- Open a [Discussion](https://github.com/alekseybb197/backend-adapter/discussions) for questions
- Comment on the relevant issue for clarification
- Tag `@alekseybb197` in PRs if stuck
