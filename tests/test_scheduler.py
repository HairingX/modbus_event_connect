"""The scheduler: which keys are due, and when - a pure decision. Every test drives a `FakeClock`
by hand; nothing here sleeps."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

import logging

import pytest

from src.modbus_event_connect._key import Key
from src.modbus_event_connect._point import DEFAULT_INTERVALS, Point, PollRate
from src.modbus_event_connect._scheduler import Scheduler
from src.modbus_event_connect._value import DataValue, Quality
from src.modbus_event_connect.modbus._access import HoldingRegister
from src.modbus_event_connect.testing._clock import FakeClock

# ================================================================================== helpers


def _point(key: str, poll_rate: PollRate = PollRate.SLOW, *, address: int = 1, readable: bool = True,
           writable: bool = False) -> Point[Any]:
    """A minimal point - only what scheduling cares about: key, poll rate and which sides it has."""
    return Point(Key(key, int), read=HoldingRegister(address) if readable else None,
                 write=HoldingRegister(address) if writable else None, poll_rate=poll_rate)


def _dv(value: object, quality: Quality = Quality.GOOD,
        ts: datetime = datetime(2026, 1, 1, tzinfo=timezone.utc)) -> DataValue[Any]:
    return DataValue(value, quality, ts)  # type: ignore[arg-type]


def _activate(scheduler: Scheduler, *keys: str) -> None:
    for key in keys:
        scheduler.set_polled(key, True)


def _tick(scheduler: Scheduler, clock: FakeClock, advance: float,
          outcomes: Mapping[str, tuple[object, bool]]) -> list[str]:
    """Advances the clock, then records the scripted `(value, success)` outcome for each due key."""
    if advance:
        clock.advance(advance)
    due = scheduler.due()
    for key in due:
        value, success = outcomes[key]
        scheduler.record(key, _dv(value) if success else None, success=success)
    return due


# =============================================================================== construction


def test_model_default_falls_back_to_DEFAULT_INTERVALS_per_group() -> None:
    scheduler = Scheduler(FakeClock(), poll_intervals={PollRate.FAST: 5.0})
    scheduler.set_points([_point("a", PollRate.FAST), _point("b", PollRate.SLOW),
                        _point("c", PollRate.RARE), _point("d", PollRate.STATIC)])
    assert scheduler.interval("a") == 5.0
    assert scheduler.interval("b") == DEFAULT_INTERVALS[PollRate.SLOW]
    assert scheduler.interval("c") == DEFAULT_INTERVALS[PollRate.RARE]
    assert scheduler.interval("d") is None


def test_negative_min_interval_rejected() -> None:
    with pytest.raises(ValueError):
        Scheduler(FakeClock(), min_poll_interval=-1.0)


def test_model_default_below_floor_is_a_construction_error() -> None:
    with pytest.raises(ValueError):
        Scheduler(FakeClock(), poll_intervals={PollRate.FAST: 3.0}, min_poll_interval=5.0)


@pytest.mark.parametrize("bad", [0.0, -1.0])
def test_model_default_must_be_positive_or_none(bad: float) -> None:
    with pytest.raises(ValueError):
        Scheduler(FakeClock(), poll_intervals={PollRate.FAST: bad})


# =============================================================================== set_points


def test_set_points_keeps_state_for_keys_that_remain() -> None:
    clock = FakeClock()
    scheduler = Scheduler(clock)
    p = _point("a", PollRate.FAST)
    scheduler.set_points([p])
    scheduler.set_polled("a", True)
    scheduler.set_poll_interval("a", 3.0)
    _tick(scheduler, clock, 0.0, {"a": (1, True)})

    scheduler.set_points([p])  # same model handed back, e.g. after a rescan
    assert scheduler.is_polled("a") is True
    assert scheduler.interval("a") == 3.0
    assert scheduler.due() == []  # last_attempt survived, so it is not due yet


def test_set_points_drops_state_for_keys_that_are_gone() -> None:
    clock = FakeClock()
    scheduler = Scheduler(clock)
    scheduler.set_points([_point("a", PollRate.FAST)])
    scheduler.set_polled("a", True)
    scheduler.set_poll_interval("a", 3.0)

    scheduler.set_points([_point("b", PollRate.FAST)])
    with pytest.raises(KeyError):
        scheduler.is_polled("a")

    scheduler.set_points([_point("a", PollRate.FAST)])  # re-added: a fresh key, not the old state
    assert scheduler.is_polled("a") is False
    assert scheduler.interval("a") == DEFAULT_INTERVALS[PollRate.FAST]


def test_set_points_new_keys_start_unpolled_and_unread() -> None:
    scheduler = Scheduler(FakeClock())
    scheduler.set_points([_point("a", PollRate.FAST)])
    assert scheduler.is_polled("a") is False
    assert scheduler.due() == []


def test_set_points_ignores_points_without_a_read_side() -> None:
    scheduler = Scheduler(FakeClock())
    scheduler.set_points([_point("a", PollRate.FAST),
                        _point("w", readable=False, writable=True, address=2)])
    with pytest.raises(KeyError):
        scheduler.is_polled("w")


def test_group_overrides_persist_across_set_points() -> None:
    scheduler = Scheduler(FakeClock())
    scheduler.set_points([_point("a", PollRate.FAST)])
    scheduler.set_poll_interval(PollRate.FAST, 3.0)

    scheduler.set_points([_point("a", PollRate.FAST), _point("b", PollRate.FAST, address=2)])
    assert scheduler.interval("a") == 3.0
    assert scheduler.interval("b") == 3.0


# =================================================================================== polled


def test_polled_round_trip() -> None:
    scheduler = Scheduler(FakeClock())
    scheduler.set_points([_point("a")])
    assert scheduler.is_polled("a") is False
    scheduler.set_polled("a", True)
    assert scheduler.is_polled("a") is True


def test_set_polled_unknown_key_raises() -> None:
    scheduler = Scheduler(FakeClock())
    with pytest.raises(KeyError):
        scheduler.set_polled("nope", True)


def test_is_polled_unknown_key_raises() -> None:
    scheduler = Scheduler(FakeClock())
    with pytest.raises(KeyError):
        scheduler.is_polled("nope")


def test_unpolled_key_is_never_due_on_a_timer() -> None:
    clock = FakeClock()
    scheduler = Scheduler(clock, poll_intervals={PollRate.FAST: 1.0})
    scheduler.set_points([_point("a", PollRate.FAST)])
    clock.advance(100)
    assert scheduler.due() == []


def test_unpolled_key_ignored_by_unforced_refresh() -> None:
    clock = FakeClock()
    scheduler = Scheduler(clock)
    scheduler.set_points([_point("a")])
    scheduler.refresh(["a"])
    assert scheduler.due() == []
    assert scheduler.next_due() is None


def test_unpolled_key_read_by_forced_refresh() -> None:
    clock = FakeClock()
    scheduler = Scheduler(clock)
    scheduler.set_points([_point("a")])
    scheduler.refresh(["a"], force=True)
    assert scheduler.due() == ["a"]


# ================================================================================= intervals


def test_effective_interval_precedence() -> None:
    scheduler = Scheduler(FakeClock())
    scheduler.set_points([_point("a", PollRate.FAST), _point("b", PollRate.FAST, address=2)])
    assert scheduler.interval("a") == DEFAULT_INTERVALS[PollRate.FAST]

    scheduler.set_poll_interval(PollRate.FAST, 5.0)
    assert scheduler.interval("a") == 5.0 and scheduler.interval("b") == 5.0

    scheduler.set_poll_interval("a", 1.0)
    assert scheduler.interval("a") == 1.0 and scheduler.interval("b") == 5.0

    scheduler.set_poll_interval("a", None)  # removes the key override, falls back to the poll rate's
    assert scheduler.interval("a") == 5.0


def test_set_interval_rejects_non_positive() -> None:
    scheduler = Scheduler(FakeClock())
    scheduler.set_points([_point("a")])
    with pytest.raises(ValueError):
        scheduler.set_poll_interval("a", 0.0)
    with pytest.raises(ValueError):
        scheduler.set_poll_interval(PollRate.FAST, -1.0)


def test_set_interval_unknown_key_raises() -> None:
    scheduler = Scheduler(FakeClock())
    with pytest.raises(KeyError):
        scheduler.set_poll_interval("nope", 1.0)


def test_interval_unknown_key_raises() -> None:
    scheduler = Scheduler(FakeClock())
    with pytest.raises(KeyError):
        scheduler.interval("nope")


def test_set_interval_clamps_to_floor_and_warns_once() -> None:
    # `-p no:logging` disables the caplog fixture, so a plain logging.Handler captures the warning.
    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    logger = logging.getLogger("src.modbus_event_connect._scheduler")
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    try:
        scheduler = Scheduler(FakeClock(), min_poll_interval=5.0)
        scheduler.set_points([_point("a", PollRate.FAST)])

        assert scheduler.set_poll_interval("a", 1.0) == 5.0
        assert scheduler.set_poll_interval("a", 1.0) == 5.0  # same target again: no new warning
        assert scheduler.interval("a") == 5.0
        assert len(records) == 1

        assert scheduler.set_poll_interval(PollRate.RARE, 2.0) == 5.0  # a different target warns once too
        assert len(records) == 2
    finally:
        logger.removeHandler(handler)


# ==================================================================================== due


def test_group_defaults_drive_due_times() -> None:
    clock = FakeClock()
    scheduler = Scheduler(clock, poll_intervals={PollRate.FAST: 10.0, PollRate.RARE: 100.0})
    scheduler.set_points([_point("fast", PollRate.FAST), _point("slow", PollRate.RARE, address=2)])
    _activate(scheduler, "fast", "slow")

    _tick(scheduler, clock, 0.0, {"fast": (1, True), "slow": (1, True)})  # both never-attempted
    assert scheduler.due() == []

    clock.advance(10.0)
    assert scheduler.due() == ["fast"]
    scheduler.record("fast", _dv(1), success=True)

    clock.advance(90.0)  # fast has ticked 9 more times worth, slow is now also due
    assert set(scheduler.due()) == {"fast", "slow"}


def test_static_point_is_read_once_then_only_by_refresh() -> None:
    clock = FakeClock()
    scheduler = Scheduler(clock)
    scheduler.set_points([_point("a", PollRate.STATIC)])
    _activate(scheduler, "a")

    assert scheduler.due() == ["a"]
    scheduler.record("a", _dv(1), success=True)

    clock.advance(10_000)
    assert scheduler.due() == []

    scheduler.refresh(["a"])
    assert scheduler.due() == ["a"]


def test_failure_uses_attempt_time_not_a_retry_every_tick() -> None:
    clock = FakeClock()
    scheduler = Scheduler(clock, poll_intervals={PollRate.FAST: 10.0})
    scheduler.set_points([_point("a", PollRate.FAST)])
    _activate(scheduler, "a")

    assert scheduler.due() == ["a"]
    scheduler.record("a", None, success=False)

    clock.advance(1.0)
    assert scheduler.due() == []  # not retried on every tick

    clock.advance(9.0)  # 10s since the failed attempt
    assert scheduler.due() == ["a"]


# ================================================================================= refresh


def test_refresh_schedules_after_a_delay() -> None:
    clock = FakeClock()
    scheduler = Scheduler(clock)
    scheduler.set_points([_point("a")])
    _activate(scheduler, "a")
    scheduler.record("a", _dv(1), success=True)  # so it is not due for an unrelated reason

    scheduler.refresh(["a"], after=5.0)
    clock.advance(4.99)
    assert scheduler.due() == []
    clock.advance(0.02)
    assert scheduler.due() == ["a"]


def test_refresh_merges_keeping_earliest_not_before_and_latest_deadline() -> None:
    clock = FakeClock()
    scheduler = Scheduler(clock)
    scheduler.set_points([_point("a")])
    _activate(scheduler, "a")

    scheduler.refresh(["a"], after=10.0, until_stable=20.0)   # not_before=10, deadline=20
    scheduler.refresh(["a"], after=2.0, until_stable=100.0)   # not_before=2,  deadline=100 (later wins)

    clock.advance(2.0)
    assert scheduler.due() == ["a"]  # the earlier not_before won
    scheduler.record("a", _dv(1), success=True)

    # following should now run to the later deadline (100), not the earlier one (20)
    clock.advance(1.98)  # t=3.98, next follow read at t=2+after(2.0)=4.0
    assert scheduler.due() == []
    clock.advance(0.02)
    assert scheduler.due() == ["a"]


def test_refresh_after_must_not_be_negative() -> None:
    scheduler = Scheduler(FakeClock())
    scheduler.set_points([_point("a")])
    with pytest.raises(ValueError):
        scheduler.refresh(["a"], after=-1.0)


def test_refresh_until_stable_must_be_positive() -> None:
    scheduler = Scheduler(FakeClock())
    scheduler.set_points([_point("a")])
    with pytest.raises(ValueError):
        scheduler.refresh(["a"], after=1.0, until_stable=0.0)


def test_refresh_until_stable_requires_a_positive_after() -> None:
    scheduler = Scheduler(FakeClock())
    scheduler.set_points([_point("a")])
    with pytest.raises(ValueError):
        scheduler.refresh(["a"], after=0.0, until_stable=5.0)


def test_refresh_unknown_key_raises_and_schedules_nothing() -> None:
    clock = FakeClock()
    scheduler = Scheduler(clock, poll_intervals={PollRate.SLOW: 60.0})
    scheduler.set_points([_point("a")])
    _activate(scheduler, "a")
    scheduler.record("a", _dv(1), success=True)  # not due on its own, so a stray schedule would show

    with pytest.raises(KeyError):
        scheduler.refresh(["a", "nope"])
    assert scheduler.due() == []  # "a" was not scheduled either - fails fast, not partway
    assert scheduler.next_due() == clock.monotonic() + 60.0


# ================================================================================= following


def test_following_keeps_going_while_ramping_then_two_equal_reads_stop_it() -> None:
    clock = FakeClock()
    scheduler = Scheduler(clock)
    scheduler.set_points([_point("a")])
    _activate(scheduler, "a")

    scheduler.refresh(["a"], after=2.0, until_stable=100.0)
    clock.advance(2.0)
    assert scheduler.due() == ["a"]
    scheduler.record("a", _dv(10), success=True)   # baseline

    clock.advance(2.0)
    assert scheduler.due() == ["a"]
    scheduler.record("a", _dv(20), success=True)   # changed: keep following

    clock.advance(2.0)
    assert scheduler.due() == ["a"]
    scheduler.record("a", _dv(20), success=True)   # equal to the previous read: stable, stop

    clock.advance(2.0)
    assert scheduler.due() == []  # no third follow read scheduled


def test_following_window_expiring_stops_it() -> None:
    clock = FakeClock()
    scheduler = Scheduler(clock, poll_intervals={PollRate.FAST: 1_000.0})
    scheduler.set_points([_point("a", PollRate.FAST)])
    _activate(scheduler, "a")

    scheduler.refresh(["a"], after=5.0, until_stable=12.0)  # deadline at t=12
    clock.advance(5.0)
    scheduler.record("a", _dv(1), success=True)       # t=5: baseline, next at t=10 (< deadline)

    clock.advance(5.0)
    assert scheduler.due() == ["a"]
    scheduler.record("a", _dv(2), success=True)       # t=10: still changing, next at t=15 scheduled

    clock.advance(5.0)                               # t=15: past the deadline (12)
    assert scheduler.due() == ["a"]                    # the already-scheduled read still happens
    scheduler.record("a", _dv(3), success=True)        # changed again, but time is up: no more

    clock.advance(999.0)                              # t=1014: just short of the FAST interval
    assert scheduler.due() == []                       # only the poll rate's schedule can bring it back now
    clock.advance(1.0)                                # t=1015 = last attempt (15) + FAST (1000)
    assert scheduler.due() == ["a"]


def test_following_failed_read_reschedules_and_keeps_baseline() -> None:
    clock = FakeClock()
    scheduler = Scheduler(clock)
    scheduler.set_points([_point("a")])
    _activate(scheduler, "a")

    scheduler.refresh(["a"], after=3.0, until_stable=20.0)
    clock.advance(3.0)
    scheduler.record("a", _dv(10), success=True)   # baseline at t=3, next scheduled t=6

    clock.advance(3.0)
    assert scheduler.due() == ["a"]
    scheduler.record("a", None, success=False)      # t=6: failed, reschedule at t=9 (time remains)

    clock.advance(2.99)
    assert scheduler.due() == []
    clock.advance(0.01)
    assert scheduler.due() == ["a"]

    scheduler.record("a", _dv(10), success=True)    # t=9: equal to baseline(10) -> stable, stop
    clock.advance(20.0)
    assert scheduler.due() == []


def test_following_falls_back_to_regular_schedule_afterwards() -> None:
    clock = FakeClock()
    scheduler = Scheduler(clock, poll_intervals={PollRate.FAST: 10.0})
    scheduler.set_points([_point("a", PollRate.FAST)])
    _activate(scheduler, "a")
    scheduler.record("a", _dv(0), success=True)  # not due on the regular schedule yet

    scheduler.refresh(["a"], after=1.0, until_stable=1.0)
    clock.advance(1.0)
    assert scheduler.due() == ["a"]
    scheduler.record("a", _dv(99), success=True)  # only read in the following; window closes now

    clock.advance(9.99)  # 9.99s since the last attempt (t=1001): still short of the FAST interval
    assert scheduler.due() == []
    clock.advance(0.01)  # 10.0s since the last attempt
    assert scheduler.due() == ["a"]


# ================================================================== Nilan scenario, end-to-end


def test_a_write_effect_is_followed_while_it_ramps_and_stops_when_two_reads_agree() -> None:
    clock = FakeClock()
    scheduler = Scheduler(clock, poll_intervals={PollRate.FAST: 10.0})
    affected = ["fan_inlet_pct", "fan_outlet_pct"]
    scheduler.set_points([_point(key, PollRate.FAST, address=i) for i, key in enumerate(affected)])
    _activate(scheduler, *affected)
    for key in affected:
        scheduler.record(key, _dv(40), success=True)  # steady state before the write

    # t=0: the write happens; the client calls refresh with settle=2, until_stable=30
    scheduler.refresh(affected, after=2.0, until_stable=30.0)
    assert scheduler.due() == []  # nothing due before the delay

    ramp = {"fan_inlet_pct": [55, 70, 70], "fan_outlet_pct": [56, 71, 71]}
    for tick, offset in enumerate([2.0, 2.0, 2.0]):
        due = _tick(scheduler, clock, offset, {key: (ramp[key][tick], True) for key in affected})
        assert set(due) == set(affected)  # a fast neighbour at the next address shares the read

    clock.advance(4.0)  # t=10: both values were stable at t=4 and t=6, so no follow read at t=8
    assert scheduler.due() == []  # only the FAST interval (10s from the t=6 read) applies
    clock.advance(6.0)  # t=16 = 6s attempt + 10s FAST interval
    assert set(scheduler.due()) == set(affected)


# ===================================================================================== reset


def test_reset_makes_keys_due_now() -> None:
    clock = FakeClock()
    scheduler = Scheduler(clock, poll_intervals={PollRate.FAST: 1_000.0})
    scheduler.set_points([_point("a", PollRate.FAST)])
    _activate(scheduler, "a")
    scheduler.record("a", _dv(1), success=True)
    assert scheduler.due() == []

    scheduler.reset(["a"])
    assert scheduler.due() == ["a"]


def test_reset_all_keys_when_none_given() -> None:
    clock = FakeClock()
    scheduler = Scheduler(clock, poll_intervals={PollRate.FAST: 1_000.0})
    scheduler.set_points([_point("a", PollRate.FAST), _point("b", PollRate.FAST, address=2)])
    _activate(scheduler, "a", "b")
    scheduler.record("a", _dv(1), success=True)
    scheduler.record("b", _dv(1), success=True)

    scheduler.reset()
    assert set(scheduler.due()) == {"a", "b"}


def test_reset_clears_pending_and_following() -> None:
    clock = FakeClock()
    scheduler = Scheduler(clock)
    scheduler.set_points([_point("a")])
    _activate(scheduler, "a")
    scheduler.record("a", _dv(1), success=True)
    scheduler.refresh(["a"], after=5.0, until_stable=50.0)

    scheduler.reset(["a"])
    assert scheduler.due() == ["a"]        # due now, because reset - not because of the refresh
    scheduler.record("a", _dv(2), success=True)
    clock.advance(5.0)
    assert scheduler.due() == []           # the pending refresh/following was cleared by reset


def test_reset_unknown_key_raises_without_partial_effect() -> None:
    clock = FakeClock()
    scheduler = Scheduler(clock, poll_intervals={PollRate.FAST: 1_000.0})
    scheduler.set_points([_point("a", PollRate.FAST)])
    _activate(scheduler, "a")
    scheduler.record("a", _dv(1), success=True)

    with pytest.raises(KeyError):
        scheduler.reset(["a", "nope"])
    assert scheduler.due() == []  # "a" was not reset


# ============================================================================ due: priority


def test_pending_and_regular_due_collapse_to_one_entry() -> None:
    clock = FakeClock()
    scheduler = Scheduler(clock, poll_intervals={PollRate.FAST: 5.0})
    scheduler.set_points([_point("a", PollRate.FAST)])
    _activate(scheduler, "a")
    scheduler.record("a", _dv(1), success=True)

    clock.advance(5.0)  # regularly due
    scheduler.refresh(["a"])  # also pending
    assert scheduler.due() == ["a"]


def test_never_attempted_sorts_before_overdue() -> None:
    clock = FakeClock()
    scheduler = Scheduler(clock, poll_intervals={PollRate.FAST: 5.0})
    scheduler.set_points([_point("overdue", PollRate.FAST), _point("fresh", PollRate.FAST, address=2)])
    _activate(scheduler, "overdue", "fresh")
    scheduler.record("overdue", _dv(1), success=True)

    clock.advance(5.0)  # "overdue" is now due; "fresh" has never been attempted
    assert scheduler.due() == ["fresh", "overdue"]


def test_most_overdue_first() -> None:
    clock = FakeClock()
    scheduler = Scheduler(clock, poll_intervals={PollRate.FAST: 5.0})
    scheduler.set_points([_point("a", PollRate.FAST), _point("b", PollRate.FAST, address=2)])
    _activate(scheduler, "a", "b")
    scheduler.record("a", _dv(1), success=True)
    clock.advance(1.0)
    scheduler.record("b", _dv(1), success=True)

    clock.advance(20.0)  # a: overdue by 16s, b: overdue by 15s
    assert scheduler.due() == ["a", "b"]


def test_ties_broken_by_point_order() -> None:
    clock = FakeClock()
    scheduler = Scheduler(clock, poll_intervals={PollRate.FAST: 5.0})
    scheduler.set_points([_point("first", PollRate.FAST), _point("second", PollRate.FAST, address=2)])
    _activate(scheduler, "second", "first")  # activation order must not matter
    assert scheduler.due() == ["first", "second"]  # never-attempted, tied: point order

    scheduler.record("first", _dv(1), success=True)
    scheduler.record("second", _dv(1), success=True)
    clock.advance(5.0)  # both overdue by exactly 0s: tied again
    assert scheduler.due() == ["first", "second"]

    scheduler.refresh(["second", "first"], after=1.0)  # scheduled with equal not_before
    clock.advance(1.0)
    assert scheduler.due() == ["first", "second"]


# ================================================================================= next_due


def test_next_due_none_with_no_polled_or_pending_keys() -> None:
    scheduler = Scheduler(FakeClock())
    scheduler.set_points([_point("a")])
    assert scheduler.next_due() is None


def test_next_due_now_for_never_attempted_polled_key() -> None:
    clock = FakeClock()
    scheduler = Scheduler(clock)
    scheduler.set_points([_point("a")])
    _activate(scheduler, "a")
    assert scheduler.next_due() == clock.monotonic()


def test_next_due_none_once_a_static_point_has_been_read() -> None:
    clock = FakeClock()
    scheduler = Scheduler(clock)
    scheduler.set_points([_point("a", PollRate.STATIC)])
    _activate(scheduler, "a")
    scheduler.record("a", _dv(1), success=True)
    assert scheduler.next_due() is None


def test_next_due_is_the_earliest_of_regular_and_pending() -> None:
    clock = FakeClock()
    scheduler = Scheduler(clock, poll_intervals={PollRate.FAST: 100.0})
    scheduler.set_points([_point("a", PollRate.FAST)])
    _activate(scheduler, "a")
    scheduler.record("a", _dv(1), success=True)
    regular_due = clock.monotonic() + 100.0
    assert scheduler.next_due() == regular_due

    scheduler.refresh(["a"], after=5.0)
    assert scheduler.next_due() == clock.monotonic() + 5.0

    clock.advance(5.0)
    next_due = scheduler.next_due()
    assert next_due is not None and next_due <= clock.monotonic()  # due already


def test_next_due_unaffected_by_a_wall_clock_jump() -> None:
    clock = FakeClock()
    scheduler = Scheduler(clock, poll_intervals={PollRate.FAST: 50.0})
    scheduler.set_points([_point("a", PollRate.FAST)])
    _activate(scheduler, "a")
    scheduler.record("a", _dv(1), success=True)
    before = scheduler.next_due()
    before_due = scheduler.due()

    clock.jump_wall(10_000_000.0)  # an NTP correction, or a battery-less boot syncing forward

    assert scheduler.next_due() == before
    assert scheduler.due() == before_due
