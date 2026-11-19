"""What a device is, and how it is talked to: a model - its identity points, sections of points,
the scan steps, its timing and what its protocol needs to know of it. Resolving a model against
one device's identity, at a level, produces the points that device actually has."""
from __future__ import annotations

import dataclasses
from abc import ABC, abstractmethod
from collections import Counter
from collections.abc import Awaitable, Callable, Hashable, Iterable, Mapping, Sequence
from dataclasses import KW_ONLY, dataclass, field
from types import MappingProxyType
from typing import Any, Protocol

from event_connect import (
    DataValue,
    Identity,
    Level,
    ModelError,
    PollRate,
    Retry,
    Selector,
    Timing,
    candidates_in_place,
    points_at,
    select,
)

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
            given where it does not, a candidate among the identity points, a candidate that
            replaces a key no other point of its sections has, or one that replaces its own key
            with another type, two candidates replacing one point, a refresh naming by key a point
            a candidate of another key replaces, and whatever its protocol finds.
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
    every instance, whatever the identity and the level."""
    _keys_at: Mapping[Level, frozenset[str]] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "sections", tuple(self.sections))
        object.__setattr__(self, "identity_points", tuple(self.identity_points))
        object.__setattr__(self, "scan_steps", tuple(self.scan_steps))
        object.__setattr__(self, "poll_intervals", MappingProxyType(dict(self.poll_intervals)))
        declared = _declared_points(self)
        object.__setattr__(self, "keys", frozenset(point.key for point in declared))
        object.__setattr__(self, "_keys_at", MappingProxyType({
            level: self.keys if level is Level.TEST else frozenset(point.key for point in points_at(declared, level))
            for level in Level}))
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
        found += _scan_check_problems(self) + _interval_problems(self, declared) + _candidate_problems(self)
        found += self.declaration_problems(declared)
        if found:
            listed = "\n".join(f"  - {problem}" for problem in found)
            raise ModelError(f"model {self.name!r} is not valid:\n{listed}")

    def keys_at(self, level: Level) -> frozenset[str]:
        """Every key a unit of this model may have at `level`, whatever the identity: at TEST every
        key, a key a candidate replaces included, as the candidate may not apply to the unit."""
        return self._keys_at[level]

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


def _candidate_problems(model: Model) -> list[str]:
    """Identity points that are candidates; candidates that replace no other point of the model's
    sections, their own key with another type, or a point another candidate replaces too; and
    refreshes that name by key a point a candidate of another key replaces."""
    found = [f"identity point {point.key!r} is a candidate, but identity points are read at every level"
             for point in model.identity_points if point.candidate is not None]
    sections = _declared_points(model)[len(model.identity_points):]
    replaceable: dict[str, set[type[object]]] = {}
    for point in sections:
        if point.candidate is None:
            replaceable.setdefault(point.key, set()).add(point.key.type)
    replacing: dict[str, list[str]] = {}
    for point in sections:
        replaces = point.candidate.replaces if point.candidate is not None else None
        if replaces is None:
            continue
        replacing.setdefault(replaces, []).append(point.key)
        if replaces not in replaceable:
            found.append(f"{point.key!r}: the candidate replaces {replaces!r}, which no point of the model's "
                         f"sections that is not a candidate has")
        elif replaces == point.key and replaceable[replaces] != {point.key.type}:
            # A consumer stores the key with its type, so one key must keep it at every level.
            found.append(f"{point.key!r}: the candidate replaces its own key, but holds a {point.key.type.__name__} "
                         f"where the point it replaces holds another type")
    found.extend(f"{replaced!r} is replaced by more than one candidate: {', '.join(map(repr, keys))}"
                 for replaced, keys in replacing.items() if len(keys) > 1)
    # At the Test level such a point is gone: a refresh naming it by key would read nothing there.
    renamed = {replaced for replaced, keys in replacing.items() if any(key != replaced for key in keys)}
    for point in _declared_points(model):
        for name, refresh in (("on_write", point.on_write), ("on_change", point.on_change)):
            if refresh is not None and isinstance(refresh.targets, tuple):
                for target in sorted(renamed.intersection(refresh.targets)):
                    found.append(f"{point.key!r}: {name} names {target!r}, which a candidate of another key "
                                 f"replaces; name it by labels")
    return found


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
    """A model resolved against one identity at one level: matched sections, expanded, with the
    level's candidates in place, checked, ready to use."""
    model: Model
    identity: Identity
    points: Mapping[str, Point[Any]]
    """Every resolved point, keyed by `key`, in declaration order: those the level uses, and those
    it leaves out for their certainty or as a candidate of another key replaces them, which a scan
    or a refresh may still name."""
    instances: Mapping[str, tuple[int, ...]]
    """label -> the instance numbers resolved for it (failed factories excluded)."""
    level: Level
    left_out: Mapping[str, str]
    """The keys of `points` the level leaves out, for their certainty or as a candidate of another
    key replaces them, with the reason."""

    @property
    def used(self) -> Mapping[str, Point[Any]]:
        """The points the level uses: `points`, less those `left_out`, in declaration order."""
        return {key: point for key, point in self.points.items() if key not in self.left_out}

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
    left_out: dict[str, str]
    problems: list[str]
    """Everything wrong with the model against this identity at this level; empty when nothing is."""


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


def _resolve(model: Model, identity: Identity, level: Level) -> _Resolution:
    """Everything `resolve()` and `problems()` share: expand `model` against `identity`, put the
    candidates of `level` in place, and check every validation rule, without raising."""
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

    present = candidates_in_place(all_points, level)
    kept = {id(point) for point in present}
    present_keys = {point.key for point in present}
    # A point a candidate of another key replaces stays, left out, so that a scan may still read it.
    replaced = {id(point): point for point in all_points
                if point.candidate is None and id(point) not in kept and point.key not in present_keys}
    replacer = {str(point.candidate.replaces): point.key for point in present
                if point.candidate is not None and point.candidate.replaces is not None}
    points: dict[str, Point[Any]] = {point.key: point for point in all_points
                                     if id(point) in kept or id(point) in replaced}
    problems.extend(_cross_problems(model, present, points))
    left_out: dict[str, str] = {point.key: f"{point.certainty} points are left out at the {level} level"
                                for point in present if not level.admits(point.certainty)}
    left_out.update({point.key: f"replaced by the candidate {replacer[point.key]!r} at the {level} level"
                     for point in replaced.values()})
    return _Resolution(points=points, instances=instances, left_out=left_out, problems=problems)


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


def resolve(model: Model, identity: Identity, level: Level) -> ResolvedModel:
    """Resolve `model` against `identity` at `level`, raising `ModelError` listing every problem
    found."""
    result = _resolve(model, identity, level)
    if result.problems:
        listed = "\n".join(f"  - {problem}" for problem in result.problems)
        raise ModelError(f"model {model.name!r} does not resolve against this identity at the {level} level:\n"
                         f"{listed}")
    return ResolvedModel(
        model=model,
        identity=identity,
        points=MappingProxyType(result.points),
        instances=MappingProxyType(result.instances),
        level=level,
        left_out=MappingProxyType(result.left_out),
    )


def problems(model: Model, identity: Identity, level: Level) -> list[str]:
    """The same checks as `resolve()`, without raising - every problem `model` has against
    `identity` at `level`, or an empty list when it has none."""
    return _resolve(model, identity, level).problems
