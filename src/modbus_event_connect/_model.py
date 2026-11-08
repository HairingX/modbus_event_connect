"""What a device is, and how it is talked to: a model - an identity, sections of points, the
scan steps and its timing - and its profile for one protocol. Resolving a model's profile against
one device's identity produces the points that device actually has."""
from __future__ import annotations

import dataclasses
from collections import Counter
from collections.abc import Awaitable, Callable, Hashable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Protocol

from event_connect import DataValue, ModelError, Selector, SystemClock, Timing, select

from ._device import Identity
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
    """What a device is, as data: sections of points, identity points, scan steps and timing.

    It says nothing of a protocol: its profile does.
    """
    name: str
    manufacturer: str
    sections: tuple[Section | RepeatedSection, ...]
    timing: Timing
    """When its points are read, how long a write takes to show, and how long it may stay silent."""
    identity_points: tuple[Point[Any], ...] = ()
    """Read before the model resolves, to add to what the handshake says about the device."""
    scan_steps: tuple[ScanStep, ...] = ()

    def __init__(self, name: str, manufacturer: str, sections: Sequence[Section | RepeatedSection], *,
                 timing: Timing,
                 identity_points: Sequence[Point[Any]] = (), scan_steps: Sequence[ScanStep] = ()) -> None:
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "manufacturer", manufacturer)
        object.__setattr__(self, "sections", tuple(sections))
        object.__setattr__(self, "identity_points", tuple(identity_points))
        object.__setattr__(self, "scan_steps", tuple(scan_steps))
        object.__setattr__(self, "timing", timing)


ModelSelector = Callable[[Identity], "Model | None"]
"""Picks a model from an identity - e.g. a decision table over handshake values."""


# ============================================================================== profile


@dataclass(frozen=True, kw_only=True)
class Profile:
    """How `model` is talked to over one protocol.

    A protocol subclasses it with what it needs to know of the model's device; this base knows
    nothing of a protocol, and takes every address as it is.
    """
    model: Model

    def problems(self, point: Point[Any]) -> list[str]:
        """What is wrong with `point` over this protocol. Empty when nothing is."""
        return []

    def address(self, access: Access) -> int:
        """The address sent for `access`.

        Raises:
            ValueError: it has none.
        """
        return access.address


def checked_profiles[P: Profile](profiles: Sequence[P]) -> tuple[P, ...]:
    """`profiles`, as a tuple, once it is known that there is one and that no two are of one model.

    Raises:
        ModelError: there is none, or two are of one model.
    """
    if not profiles:
        raise ModelError("a device needs the profile of every model it may meet, and was given none")
    for index, profile in enumerate(profiles):
        if any(other.model is profile.model for other in profiles[:index]):
            raise ModelError(f"two profiles of the model {profile.model.name!r}: a device talks to a "
                             f"model one way")
    return tuple(profiles)


def profile_of[P: Profile](profiles: Sequence[P], model: Model) -> P:
    """The profile of `model` among `profiles`.

    Raises:
        ModelError: none is of `model`.
    """
    for profile in profiles:
        if profile.model is model:
            return profile
    raise ModelError(f"no profile of the model {model.name!r}: give the device the profile of every "
                     f"model it may meet")


# ======================================================================= resolved model


@dataclass(frozen=True)
class ResolvedModel:
    """A model's profile resolved against one identity: matched sections, expanded, checked,
    ready to use."""
    profile: Profile
    identity: Identity
    points: Mapping[str, Point[Any]]
    """Every resolved point, keyed by `key`, in declaration order."""
    instances: Mapping[str, tuple[int, ...]]
    """label -> the instance numbers resolved for it (failed factories excluded)."""

    @property
    def model(self) -> Model:
        """The model resolved."""
        return self.profile.model

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


def _overlap_problems(all_points: Sequence[Point[Any]], profile: Profile) -> list[str]:
    """Partial overlaps of addresses within one access space, read and write sides separately."""
    found: list[str] = []
    for side in ("read", "write"):
        by_space: dict[Hashable, list[tuple[str, int, int]]] = {}
        for point in all_points:
            access = point.read if side == "read" else point.write
            if access is None or access.bits:
                continue                                    # bit spaces: same address is fine
            try:
                start = profile.address(access)
            except ValueError:
                continue                                    # the profile reports it
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


def _resolve(profile: Profile, identity: Identity) -> _Resolution:
    """Everything `resolve()` and `problems()` share: expand the profile's model against
    `identity` and check every validation rule, without raising."""
    model = profile.model
    problems = _model_problems(model)
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
    problems.extend(_cross_problems(profile, all_points, points))
    return _Resolution(points=points, instances=instances, problems=problems)


def _model_problems(model: Model) -> list[str]:
    problems: list[str] = []
    if not model.name:
        problems.append("the model name must not be empty")
    if not model.manufacturer:
        problems.append("the model manufacturer must not be empty")
    try:
        model.timing.scheduler(SystemClock())
    except ValueError as err:
        problems.append(f"timing: {err}")
    return problems


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


def _cross_problems(profile: Profile, all_points: Sequence[Point[Any]],
                    points: Mapping[str, Point[Any]]) -> list[str]:
    """What is wrong between points: duplicates, overlaps, targets, protocol limits."""
    timing = profile.model.timing
    problems = [f"duplicate key {key!r} ({count} times)"
                for key, count in Counter(point.key for point in all_points).items() if count > 1]
    problems.extend(_overlap_problems(all_points, profile))
    for point in all_points:
        for name, refresh in (("on_write", point.on_write), ("on_change", point.on_change)):
            issue = _selection_problem(points, refresh.targets) if refresh is not None else None
            if issue:
                problems.append(f"{point.key!r}: {name} target {issue}")
        if point.on_write is not None and point.on_write.until_stable is not None \
                and not timing.on_write_delay(point) > 0:
            problems.append(f"{point.key!r}: on_write re-reads until stable, which needs a positive "
                            f"read-back delay to re-read at")
    problems.extend(f"{point.key!r}: {issue}" for point in all_points for issue in profile.problems(point))
    return problems


# ==================================================================================== api


def resolve(profile: Profile, identity: Identity) -> ResolvedModel:
    """Resolve the model of `profile` against `identity`, raising `ModelError` listing every
    problem found."""
    result = _resolve(profile, identity)
    if result.problems:
        listed = "\n".join(f"  - {problem}" for problem in result.problems)
        raise ModelError(f"model {profile.model.name!r} does not resolve against this identity:\n{listed}")
    return ResolvedModel(
        profile=profile,
        identity=identity,
        points=MappingProxyType(result.points),
        instances=MappingProxyType(result.instances),
    )


def problems(profile: Profile, identity: Identity) -> list[str]:
    """The same checks as `resolve()`, without raising - every problem the model of `profile` has
    against `identity`, or an empty list when it has none."""
    return _resolve(profile, identity).problems
