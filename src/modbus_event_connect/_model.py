"""What a device is, and how it is talked to: a model - its identity points, sections of points,
the scan steps, its timing and what its protocol needs to know of it. Resolving a model against
one device's identity produces the points that device actually has."""
from __future__ import annotations

import dataclasses
from abc import ABC, abstractmethod
from collections import Counter
from collections.abc import Awaitable, Callable, Hashable, Iterable, Mapping, Sequence
from dataclasses import KW_ONLY, dataclass, field
from types import MappingProxyType
from typing import Any, Protocol

from event_connect import DataValue, Identity, ModelError, PollRate, Retry, Selector, Timing, select

from ._point import Access, Point

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

    What a scan reads is read again at the model's `scan_check` rate to find out whether the unit
    has changed, so a scan reads only what decides what the unit has.
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

_KEYS_SHOWN = 3
"""Keys a problem names before it counts the rest."""


@dataclass(frozen=True, eq=False)
class Model(ABC):
    """What a device is, and everything about how its unit behaves over one protocol: sections of
    points, identity points, scan steps and timing; a protocol subclasses it with the rest.

    A model is one object: two are never the same model, however alike.

    Raises:
        ModelError: at construction, listing every problem: an empty name or manufacturer, timing
            that is not valid, `poll_intervals` that miss a rate the readable points or the scan
            check use or hold one nothing uses, a `scan_check` missing where the model scans or
            given where it does not, and whatever its protocol finds.
    """
    name: str
    manufacturer: str
    sections: Sequence[Section | RepeatedSection]
    _: KW_ONLY
    poll_intervals: Mapping[PollRate, float]
    """Seconds between reads of each poll rate its readable points and its scan check use, and of
    no other; `PollRate.STATIC` has none."""
    read_back_after: float
    """Seconds from a write until a read shows it."""
    write_retry: Retry
    """When a write the unit answered busy is sent again, counting its sendings and the time since
    the first; `NO_RETRY` sends every write once."""
    scan_check: PollRate | None
    """The poll rate at which what its scans read is read again, to find out whether the unit has
    changed; None for a model that does not scan."""
    identity_points: Sequence[Point[Any]]
    """Read before the model resolves, to add to what the handshake says about the device."""
    scan_steps: Sequence[ScanStep]
    timing: Timing = field(init=False, repr=False)
    """Its `poll_intervals` and `read_back_after`, as a scheduler plans by them."""
    keys: frozenset[str] = field(init=False, repr=False)
    """Every key a unit of this model may have: of its identity points, and of every section and
    every instance, whatever the identity."""

    def __post_init__(self) -> None:
        object.__setattr__(self, "sections", tuple(self.sections))
        object.__setattr__(self, "identity_points", tuple(self.identity_points))
        object.__setattr__(self, "scan_steps", tuple(self.scan_steps))
        object.__setattr__(self, "poll_intervals", MappingProxyType(dict(self.poll_intervals)))
        declared = _declared_points(self)
        object.__setattr__(self, "keys", frozenset(point.key for point in declared))
        found: list[str] = []
        if not self.name:
            found.append("the model name must not be empty")
        if not self.manufacturer:
            found.append("the model manufacturer must not be empty")
        try:
            object.__setattr__(self, "timing", Timing(poll_intervals=self.poll_intervals,
                                                      read_back_after=self.read_back_after))
        except ValueError as err:
            found.append(f"timing: {err}")
        found += _scan_check_problems(self) + _interval_problems(self, declared)
        found += self.declaration_problems(declared)
        if found:
            listed = "\n".join(f"  - {problem}" for problem in found)
            raise ModelError(f"model {self.name!r} is not valid:\n{listed}")

    @abstractmethod
    def problems(self, point: Point[Any]) -> list[str]:
        """What is wrong with `point` over this model's protocol. Empty when nothing is."""

    @abstractmethod
    def address(self, access: Access) -> int:
        """The address sent for `access`.

        Raises:
            ValueError: it has none.
        """

    @abstractmethod
    def declaration_problems(self, points: Sequence[Point[Any]]) -> list[str]:
        """What is wrong with the model over its protocol, given every point it declares. Empty when
        nothing is."""


def _scans(model: Model) -> bool:
    """Whether `model` scans: it has scan steps, or a repeated section with a scan."""
    return bool(model.scan_steps) or any(isinstance(section, RepeatedSection) and section.scan is not None
                                         for section in model.sections)


def _declared_points(model: Model) -> list[Point[Any]]:
    """Every point `model` declares, whatever the identity; an instance whose factory raises is left
    out, as resolving reports it."""
    points = list(model.identity_points)
    for section in model.sections:
        if isinstance(section, RepeatedSection):
            for number in section.numbers:
                try:
                    points.extend(section.factory(number))
                except Exception:
                    continue
        else:
            points.extend(section.points)
    return points


def _scan_check_problems(model: Model) -> list[str]:
    scans = _scans(model)
    if scans and model.scan_check is None:
        return ["it scans, so it needs a scan_check: the poll rate what its scans read is read again at"]
    if not scans and model.scan_check is not None:
        return [f"scan_check is {model.scan_check.name}, but the model has no scan to check"]
    return []


def _interval_problems(model: Model, declared: Sequence[Point[Any]]) -> list[str]:
    """The rates the readable points or the scan check use that have no interval, and the intervals
    of rates nothing uses."""
    users: dict[PollRate, list[str]] = {}
    for point in declared:
        if point.readable and point.poll_rate is not None and point.poll_rate is not PollRate.STATIC:
            users.setdefault(point.poll_rate, []).append(repr(str(point.key)))
    if model.scan_check is not None and model.scan_check is not PollRate.STATIC:
        users.setdefault(model.scan_check, []).append("the scan check")
    problems = [f"poll_intervals has no interval for {rate.name}, at which {_named(who)} "
                f"{'is' if len(set(who)) == 1 else 'are'} read"
                for rate, who in users.items() if rate not in model.poll_intervals]
    problems += [f"poll_intervals gives {rate.name} an interval, but nothing is read at it"
                 for rate in model.poll_intervals if rate not in users and rate is not PollRate.STATIC]
    return problems


def _named(who: Sequence[str]) -> str:
    """`who`, the first few by name and the rest counted."""
    shown = list(dict.fromkeys(who))
    if len(shown) <= _KEYS_SHOWN:
        return ", ".join(shown)
    return f"{', '.join(shown[:_KEYS_SHOWN])} and {len(shown) - _KEYS_SHOWN} more"


ModelSelector = Callable[[Identity], "Model | None"]
"""Picks a model from an identity - e.g. a decision table over handshake values."""


def checked_models[M: Model](models: Sequence[M]) -> tuple[M, ...]:
    """`models`, as a tuple, once it is known that there is one and that none is given twice.

    Raises:
        ModelError: there is none, or one is given twice.
    """
    if not models:
        raise ModelError("a device needs every model it may meet, and was given none")
    for index, model in enumerate(models):
        if any(other is model for other in models[:index]):
            raise ModelError(f"the model {model.name!r} is given twice")
    return tuple(models)


def model_among[M: Model](models: Sequence[M], model: Model) -> M:
    """`model`, as one of `models`.

    Raises:
        ModelError: it is not one of them.
    """
    for candidate in models:
        if candidate is model:
            return candidate
    raise ModelError(f"the model {model.name!r} is not one the device was given: give the device every "
                     f"model it may meet")


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
        """The points `targets` selects: for keys, in the order given; for labels, every point
        carrying them, in declaration order, which may be none.

        Raises:
            KeyError: naming every key the model does not have.
        """
        return select(tuple(self.points.values()), targets)

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
    """Everything wrong with the model against this identity; empty when nothing is."""


def section_label(index: int, section: Section | RepeatedSection) -> str:
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
    try:
        selected = select(tuple(points.values()), targets)
    except KeyError as err:
        return str(err.args[0])
    return None if selected else f"{targets!r} selects no point"


def _overlap_problems(all_points: Sequence[Point[Any]], model: Model) -> list[str]:
    """Partial overlaps of addresses within one access space, read and write sides separately."""
    found: list[str] = []
    for side in ("read", "write"):
        by_space: dict[Hashable, list[tuple[str, int, int]]] = {}
        for point in all_points:
            access = point.read if side == "read" else point.write
            if access is None or access.bits:
                continue                                    # bit spaces: same address is fine
            try:
                start = model.address(access)
            except ValueError:
                continue                                    # the model's problems report it
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
    """Everything `resolve()` and `problems()` share: expand `model` against `identity` and check
    every validation rule, without raising."""
    problems: list[str] = []
    all_points: list[Point[Any]] = []
    instances: dict[str, tuple[int, ...]] = {}

    for point in model.identity_points:
        all_points.append(point)
        if point.read is None:
            problems.append(f"identity point {point.key!r} has no read side")

    for index, section in enumerate(model.sections):
        label = section_label(index, section)
        if not applies(section, identity, label, problems):
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


def applies(section: Section | RepeatedSection, identity: Identity, label: str, problems: list[str]) -> bool:
    """Whether `section` is part of the unit `identity` names; a `when` that raises is a problem,
    and the section is left out."""
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


def _cross_problems(model: Model, all_points: Sequence[Point[Any]],
                    points: Mapping[str, Point[Any]]) -> list[str]:
    """What is wrong between points: duplicates, overlaps, targets, protocol limits."""
    timing = model.timing
    problems = [f"duplicate key {key!r} ({count} times)"
                for key, count in Counter(point.key for point in all_points).items() if count > 1]
    problems.extend(_overlap_problems(all_points, model))
    for point in all_points:
        for name, refresh in (("on_write", point.on_write), ("on_change", point.on_change)):
            issue = _selection_problem(points, refresh.targets) if refresh is not None else None
            if issue:
                problems.append(f"{point.key!r}: {name} target {issue}")
        if point.on_write is not None and point.on_write.until_stable is not None \
                and not timing.on_write_delay(point) > 0:
            problems.append(f"{point.key!r}: on_write re-reads until stable, which needs a positive "
                            f"read-back delay to re-read at")
    problems.extend(f"{point.key!r}: {issue}" for point in all_points for issue in model.problems(point))
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
