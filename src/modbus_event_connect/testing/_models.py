"""Checks a device repository runs over its models: every variant, and every transform."""
from __future__ import annotations

from collections.abc import Sequence

from .._device import Identity
from .._model import Instances, Model, Section, problems, resolve
from .._point import Point

_ROUND_TRIP_SAMPLES = (-7.5, -1.0, 0.0, 0.5, 1.0, 7.0, 60.0, 100.0, 1000.0)
"""Values a transform must bring back unchanged: both signs, a fraction, and the sizes that
unit conversions work with."""


def assert_models_valid(*models: Model, identities: Sequence[Identity]) -> None:
    """Resolve every model against every identity, and check what resolving does not.

    Raises:
        AssertionError: listing every problem found - a model that does not resolve, a transform
            that does not round-trip, or a section no identity included, which is left untested.
        ValueError: `identities` is empty.
    """
    if not identities:
        raise ValueError("assert_models_valid needs at least one identity to check models against")

    findings: list[str] = []
    for model in models:
        included = [False] * len(model.sections)
        for identity in identities:
            prefix = f"{model.name} / {identity}"
            found = problems(model, identity)
            findings.extend(f"{prefix}: {problem}" for problem in found)
            for index, section in enumerate(model.sections):
                included[index] = included[index] or _includes(section, identity)
            if not found:
                points = resolve(model, identity).points.values()
                findings.extend(f"{prefix}: {issue}" for issue in map(_transform_problem, points) if issue)
        for index, section in enumerate(model.sections):
            if section.when is not None and not included[index]:
                findings.append(f"{model.name}: {_label(index, section)} never resolved - untested")

    if findings:
        raise AssertionError("\n".join(dict.fromkeys(findings)))


def _includes(section: Section | Instances, identity: Identity) -> bool:
    try:
        return bool(section.when is None or section.when(identity))
    except Exception:
        return False            # reported by problems()


def _label(index: int, section: Section | Instances) -> str:
    if isinstance(section, Instances):
        return f"sections[{index}] (Instances label={section.label!r})"
    return f"sections[{index}] (Section)"


def _transform_problem(point: Point) -> str | None:
    """What is wrong with `point`'s transform, or None if it round-trips every sample."""
    transform = point.transform
    if transform is None:
        return None
    name = transform.name or "transform"
    for x in _ROUND_TRIP_SAMPLES:
        try:
            back = transform.write(transform.read(x))
        except Exception as err:
            return f"{point.key!r}: {name} raised {err!r} round-tripping {x}"
        if abs(back - x) > 1e-9 * max(1.0, abs(x)):
            return f"{point.key!r}: {name} does not round-trip: write(read({x})) = {back}, expected {x}"
    return None
