# Conventions for this repository

## Comments and docstrings

Based on PEP 8 (Comments), PEP 257 (Docstring Conventions) and the Google Python Style Guide
(3.8 Comments and Docstrings).

**Docstrings describe the unit's own contract, and nothing else.**
- Every public module, class, function and method has one.
- The first line is a one-sentence summary. Add more only when a caller needs it: arguments,
  return value, exceptions raised, side effects, invariants.
- Use Google style sections (`Args:`, `Returns:`, `Raises:`) only when they say something the
  signature and type hints do not. Never repeat types; the hints carry them.
- Describe what the unit does, not who calls it, how other classes use its data, or where its
  result ends up. That knowledge belongs to those other units and goes stale here.

**Comments explain why, never what.**
- Write one only where the code cannot say it: a protocol quirk, a non-obvious constraint, the
  reason for an order of operations.
- No history: no "used to", "previously", "the old code", "this was a bug". Version control
  holds history; a regression test's name holds the reason it exists.
- No references to other files, classes, line numbers or design-document sections. They rot the
  moment the other side changes.
- No commented-out code. No TODO without an issue reference.

**Keep them short.** A comment longer than a few lines is design rationale; it belongs in
`docs/`, where it can cover several modules at once.

**Tests.** The test name states the behaviour. Add a docstring only when the reason is not
obvious from the name - one or two lines, no narrative.

## Other conventions

- pyright strict must report 0 errors (`pyright`); the configuration is in `pyproject.toml`.
- Every problem found gets a test that fails without the fix.
- Test support - simulators, fake clocks, checks that sample behaviour - lives in `testing/`,
  never in the modules that run in production.
- Live tests (`tests/test_live_*.py`) only read, and skip themselves unless a device is
  configured. CI runs `pytest -m "not live"`.
- No real IP addresses, hostnames or email addresses in tracked files; real values live in the
  gitignored `mysecrets.py` or in environment variables.
