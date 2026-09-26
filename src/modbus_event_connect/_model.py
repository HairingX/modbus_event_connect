"""What a device is: an identity, sections of points, and the scan steps. Resolving a model
against one device's identity produces the points that device actually has."""
from __future__ import annotations

import dataclasses
import math
from collections import Counter
from collections.abc import Awaitable, Callable, Hashable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Protocol

from ._device import Identity, ProtocolOptions
from ._errors import ModelError
from ._point import DEFAULT_INTERVALS, Labels, Point, PollRate, Selector
from ._value import DataValue

# ============================================================================= sections


@dataclass(frozen=True)
class Section:
    """A fixed group of points, included whenever `when` allows it (`None` means always)."""
    points: tuple[Point[Any], ...]
    when: Callable[[Identity], bool] | None = None

    def __init__(self, points: Sequence[Point[Any]], when: Callable[[Identity], bool] | None = None) -> None:
        object.__setattr__(self, "points", tuple(points))
        object.__setattr__(self, "when", when)


@dataclass(frozen=True)
class RepeatedSection:
    """A section repeated once per number: `factory(n)` gives its points, each labelled `{label: n}`.

    `scan(scan, n)` finds what instance `n` has, and may mark only instance `n`'s points. It runs
    for every instance at connect, and again for one instance when what it read has changed.
    """
    factory: Callable[[int], Sequence[Point[Any]]]
    numbers: tuple[int, ...]
    label: str
    when: Callable[[Identity], bool] | None = None
    scan: InstanceScanStep | None = None

    def __init__(self, factory: Callable[[int], Sequence[Point[Any]]], numbers: Iterable[int], label: str,
                 when: Callable[[Identity], bool] | None = None, scan: InstanceScanStep | None = None) -> None:
        object.__setattr__(self, "factory", factory)
        object.__setattr__(self, "numbers", tuple(numbers))
        object.__setattr__(self, "label", label)
        object.__setattr__(self, "when", when)
        object.__setattr__(self, "scan", scan)


# ================================================================================== scan


class Scan(Protocol):
    """What a scan step may do: read to find out what is there, and mark parts unavailable.

    What a scan reads is read again at `PollRate.SCAN` to find out whether the unit has changed,
    so a scan reads only what decides what the unit has.
    """

    @property
    def identity(self) -> Identity: ...

    async def read(self, targets: Selector | Sequence[str]) -> Mapping[str, DataValue[Any]]: ...

    def set_available(self, targets: Selector | Sequence[str], available: bool, *,
                      reason: str = "") -> None: ...


ScanStep = Callable[[Scan], Awaitable[None]]
"""One step of the scan of the whole unit, run in the model's own order.

When what the steps read has changed, the whole unit is scanned again and its model chosen afresh.
"""

InstanceScanStep = Callable[[Scan, int], Awaitable[None]]
"""The scan of one instance of a repeated section, given its number."""


# =============================================================================== model


@dataclass(frozen=True)
class Model:
    """A device, as data: sections of points, identity points, scan steps and protocol options."""
    name: str
    manufacturer: str
    sections: tuple[Section | RepeatedSection, ...]
    options: ProtocolOptions
    """How the device is reached, in its protocol's terms."""
    read_back_after: float
    """Seconds from a write until a read shows the written value, on this device."""
    identity_points: tuple[Point[Any], ...] = ()
    """Read before the model resolves, to add to what the handshake says about the device."""
    scan_steps: tuple[ScanStep, ...] = ()
    poll_intervals: Mapping[PollRate, float | None] = DEFAULT_INTERVALS
    min_poll_interval: float = 0.0

    def __init__(self, name: str, manufacturer: str, sections: Sequence[Section | RepeatedSection], *,
                 options: ProtocolOptions, read_back_after: float,
                 identity_points: Sequence[Point[Any]] = (), scan_steps: Sequence[ScanStep] = (),
                 poll_intervals: Mapping[PollRate, float | None] = DEFAULT_INTERVALS,
                 min_poll_interval: float = 0.0) -> None:
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "manufacturer", manufacturer)
        object.__setattr__(self, "sections", tuple(sections))
        object.__setattr__(self, "identity_points", tuple(identity_points))
        object.__setattr__(self, "scan_steps", tuple(scan_steps))
        object.__setattr__(self, "options", options)
        object.__setattr__(self, "read_back_after", read_back_after)
        object.__setattr__(self, "poll_intervals", poll_intervals)
        object.__setattr__(self, "min_poll_interval", min_poll_interval)

    def read_back_delay(self, point: Point[Any]) -> float:
        """Seconds after writing `point` before it, and what its write disturbs, are read."""
        if point.on_write is not None and point.on_write.after is not None:
            return point.on_write.after
        return point.read_back_after if point.read_back_after is not None else self.read_back_after


ModelSelector = Callable[[Identity], "Model | None"]
"""Picks a model from an identity - e.g. a decision table over handshake values."""


# ======================================================================= resolved model


@dataclass(frozen=True)
class ResolvedModel:
    """A model resolved against one identity: matched sections, expanded, checked, ready to use."""
    model: Model
    identity: Identity
    points: Mapping[str, Point[Any]]
    """Every resolved point, keyed by `key`, in declaration order."""
    instances: Mapping[str, tuple[int, ...]]
    """label -> the instance numbers resolved for it (failed factories excluded)."""

    def select(self, targets: Selector) -> tuple[Point[Any], ...]:
        """Every point matching `targets`, in declaration order. May be empty for `Labels`."""
        if isinstance(targets, Labels):
            return tuple(point for point in self.points.values() if targets.matches(point.labels))
        unknown = [key for key in targets if key not in self.points]
        if unknown:
            raise KeyError("unknown key(s): " + ", ".join(repr(key) for key in unknown))
        return tuple(self.points[key] for key in targets)

    def point(self, key: str) -> Point[Any]:
        """The point for `key`. Raises `KeyError` naming the key if this model has none."""
        try:
            return self.points[key]
        except KeyError:
            raise KeyError(f"no such point: {key!r}") from None


# =================================================================== resolution internals


@dataclass
class _Resolution:
    """What resolving one model against one identity produced, problems included."""
    points: dict[str, Point[Any]]
    instances: dict[str, tuple[int, ...]]
    problems: list[str]
    """Parallel to `model.sections`: whether each section's `when` applied for this identity."""


def _section_label(index: int, section: Section | RepeatedSection) -> str:
    """Names a section in a problem message - there is no other identifier for one."""
    if isinstance(section, RepeatedSection):
        return f"sections[{index}] (RepeatedSection label={section.label!r})"
    return f"sections[{index}] (Section)"


def _space_name(space: Hashable) -> str:
    name = getattr(space, "__name__", None)
    return name if isinstance(name, str) else str(space)


def _selection_problem(points: Mapping[str, Point[Any]], targets: Selector) -> str | None:
    """What is wrong with a selector used inside a model (`on_write` / `on_change`): it must
    select at least one point of *this* resolved model. None means it is fine."""
    if isinstance(targets, Labels):
        if not any(targets.matches(point.labels) for point in points.values()):
            return f"{targets!r} selects no point"
        return None
    unknown = [key for key in targets if key not in points]
    if unknown:
        return "unknown key(s) " + ", ".join(repr(key) for key in unknown)
    return None


def _overlap_problems(all_points: Sequence[Point[Any]], options: ProtocolOptions) -> list[str]:
    """Partial overlaps of addresses within one access space, read and write sides separately."""
    found: list[str] = []
    for side in ("read", "write"):
        by_space: dict[Hashable, list[tuple[str, int, int]]] = {}
        for point in all_points:
            access = point.read if side == "read" else point.write
            if access is None or access.bits:
                continue                                    # bit spaces: same address is fine
            try:
                start = options.address(access)
            except ValueError:
                continue                                    # the options report it
            end = start + point.registers
            by_space.setdefault(access.space, []).append((point.key, start, end))
        for space, items in by_space.items():
            for i in range(len(items)):
                key_i, start_i, end_i = items[i]
                for j in range(i + 1, len(items)):
                    key_j, start_j, end_j = items[j]
                    intersects = start_i < end_j and start_j < end_i
                    identical = start_i == start_j and end_i == end_j
                    if intersects and not identical:
                        found.append(
                            f"the {side} sides of {key_i!r} [{start_i}, {end_i}) and {key_j!r} "
                            f"[{start_j}, {end_j}) overlap in {_space_name(space)}")
    return found


def _resolve(model: Model, identity: Identity) -> _Resolution:
    """Everything `resolve()` and `problems()` share: expand the model against `identity` and
    check every validation rule, without raising."""
    problems = _model_problems(model)
    all_points: list[Point[Any]] = []
    instances: dict[str, tuple[int, ...]] = {}

    for point in model.identity_points:
        all_points.append(point)
        if point.read is None:
            problems.append(f"identity point {point.key!r} has no read side")

    for index, section in enumerate(model.sections):
        label = _section_label(index, section)
        applies = _applies(section, identity, label, problems)
        if not applies:
            continue
        if isinstance(section, RepeatedSection):
            produced, numbers = _expand_repeated(section, label, problems)
            all_points.extend(produced)
            if section.label in instances:
                problems.append(f"{label}: another repeated section already has the label {section.label!r}")
            if section.label:
                instances[section.label] = numbers
        else:
            all_points.extend(section.points)

    points: dict[str, Point[Any]] = {point.key: point for point in all_points}
    problems.extend(_cross_problems(model, all_points, points))
    return _Resolution(points=points, instances=instances, problems=problems)


def _model_problems(model: Model) -> list[str]:
    problems: list[str] = []
    if not model.name:
        problems.append("the model name must not be empty")
    if not model.manufacturer:
        problems.append("the model manufacturer must not be empty")
    if not (math.isfinite(model.read_back_after) and model.read_back_after >= 0):
        problems.append(f"read_back_after must be a finite number >= 0, got {model.read_back_after}")
    if model.min_poll_interval < 0:
        problems.append(f"min_poll_interval must be >= 0, got {model.min_poll_interval}")
    for poll_rate, interval in model.poll_intervals.items():
        if interval is None:
            continue
        if not interval > 0:
            problems.append(f"the interval for {poll_rate.name} must be positive or None, got {interval}")
        elif interval < model.min_poll_interval:
            problems.append(f"the interval for {poll_rate.name} ({interval}) is below min_poll_interval "
                            f"({model.min_poll_interval})")
    return problems


def _applies(section: Section | RepeatedSection, identity: Identity, label: str, problems: list[str]) -> bool:
    try:
        return bool(section.when is None or section.when(identity))
    except Exception as err:
        problems.append(f"{label}: when(identity) raised {err!r}")
        return False


def _expand_repeated(section: RepeatedSection, label: str, problems: list[str]) -> tuple[list[Point[Any]], tuple[int, ...]]:
    """Every instance's points, labeled with its number, and the numbers whose factory succeeded."""
    if not section.label:
        problems.append(f"{label}: the label must not be empty")
    for number, count in Counter(section.numbers).items():
        if count > 1:
            problems.append(f"{label}: instance number {number} appears {count} times")
    produced_points: list[Point[Any]] = []
    numbers: list[int] = []
    for number in section.numbers:
        try:
            produced = list(section.factory(number))
        except Exception as err:
            problems.append(f"{label} instance {number}: factory raised {err!r}")
            continue
        numbers.append(number)
        for point in produced:
            if section.label:
                existing = point.labels.get(section.label)
                if existing is not None and existing != number:
                    problems.append(f"{point.key!r}: already labeled {section.label}={existing!r}, "
                                    f"{label} would set it to {number!r}")
                point = dataclasses.replace(point, labels={**point.labels, section.label: number})
            produced_points.append(point)
    return produced_points, tuple(numbers)


def _cross_problems(model: Model, all_points: Sequence[Point[Any]], points: Mapping[str, Point[Any]]) -> list[str]:
    """What is wrong between points: duplicates, overlaps, targets, protocol limits."""
    problems = [f"duplicate key {key!r} ({count} times)"
                for key, count in Counter(point.key for point in all_points).items() if count > 1]
    problems.extend(_overlap_problems(all_points, model.options))
    for point in all_points:
        for name, refresh in (("on_write", point.on_write), ("on_change", point.on_change)):
            issue = _selection_problem(points, refresh.targets) if refresh is not None else None
            if issue:
                problems.append(f"{point.key!r}: {name} target {issue}")
        if point.on_write is not None and point.on_write.until_stable is not None \
                and not model.read_back_delay(point) > 0:
            problems.append(f"{point.key!r}: on_write re-reads until stable, which needs a positive "
                            f"read-back delay to re-read at")
    problems.extend(f"{point.key!r}: {issue}" for point in all_points for issue in model.options.problems(point))
    return problems


# ==================================================================================== api


def resolve(model: Model, identity: Identity) -> ResolvedModel:
    """Resolve `model` against `identity`, raising `ModelError` listing every problem found."""
    result = _resolve(model, identity)
    if result.problems:
        listed = "\n".join(f"  - {problem}" for problem in result.problems)
        raise ModelError(f"model {model.name!r} does not resolve against this identity:\n{listed}")
    return ResolvedModel(
        model=model,
        identity=identity,
        points=MappingProxyType(result.points),
        instances=MappingProxyType(result.instances),
    )


def problems(model: Model, identity: Identity) -> list[str]:
    """The same checks as `resolve()`, without raising - every problem `model` has against
    `identity`, or an empty list when it has none."""
    return _resolve(model, identity).problems
