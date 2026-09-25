"""Shared test setup.

Live tests talk to real hardware on the developer's network. They must be invisible anywhere
that hardware is not - a CI runner above all, where a missing device is not a failure but an
absence. So they are opt-in: configure a device and they run, leave it unconfigured and they
skip.

Two independent mechanisms, because a green CI run that silently skipped everything is worse
than a red one:

  - `live_or_skip(...)` skips a module when its settings are absent.
  - the `live` marker lets a run exclude them outright: `pytest -m "not live"`.
"""
import os

import pytest


def live_setting(name: str) -> str | None:
    """
    A live test's setting: environment first, then mysecrets.py.

    One file, because every name already says which transport it belongs to -
    MODBUS_TCP_HOST cannot be mistaken for MICRO_NABTO_HOST. Splitting the file as well would
    be a second answer to a question the naming has already settled. mysecrets.py is gitignored;
    a real address must never reach a tracked file, not even in a comment.
    """
    value = os.environ.get(name)
    if value:
        return value
    try:
        import mysecrets  # type: ignore
    except Exception:
        return None
    found = getattr(mysecrets, name, None)
    return str(found) if found else None


def live_or_skip(what: str, **settings: str | None):
    """
    Marks for a module of live tests: the `live` marker, plus a skip when anything is missing.

    Use it as `pytestmark = live_or_skip("Modbus TCP", MODBUS_TCP_HOST=HOST)`. Naming the
    missing settings in the reason matters: a skip whose cause is not obvious gets ignored, and
    then the test may as well not exist.
    """
    missing = [name for name, value in settings.items() if not value]
    return [
        pytest.mark.live,
        pytest.mark.skipif(
            bool(missing),
            reason=f"No {what} device configured. Set {', '.join(missing)} as environment "
                   f"variables or in a gitignored mysecrets file.",
        ),
    ]
