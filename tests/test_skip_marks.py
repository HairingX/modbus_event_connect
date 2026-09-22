"""The mechanism that keeps live tests off a machine without hardware.

A CI runner has no mysecrets.py, so these marks are the only thing between it and a suite that
tries to reach devices that are not there. Testing that by hiding the real secrets file would
be worse than not testing it at all: an interrupted run leaves the developer's credentials
under a name .gitignore does not match. So the logic is tested directly instead.
"""
from conftest import live_or_skip


def test_live_or_skip_skips_when_a_setting_is_missing():
    """
    The skip mechanism itself, tested without touching any file.

    A CI runner has no mysecrets.py, so these marks are the only thing standing between it and
    a suite that tries to reach hardware that is not there. Testing it by hiding the real file
    would be worse than not testing it: an interrupted run leaves the developer's secrets under
    a name .gitignore does not match.
    """
    marks = live_or_skip("Modbus TCP", MODBUS_TCP_HOST=None, MODBUS_TCP_PORT="502")
    skipif = next(m for m in marks if m.name == "skipif")
    assert skipif.args[0] is True
    assert "MODBUS_TCP_HOST" in skipif.kwargs["reason"]
    assert "MODBUS_TCP_PORT" not in skipif.kwargs["reason"], "only the missing ones are named"
    assert any(m.name == "live" for m in marks)


def test_live_or_skip_runs_when_everything_is_set():
    marks = live_or_skip("Modbus TCP", MODBUS_TCP_HOST="<device-ip>")
    skipif = next(m for m in marks if m.name == "skipif")
    assert skipif.args[0] is False
