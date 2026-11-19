"""Checks a device repository runs over its models: every variant, and every transform."""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from event_connect import Identity, Level

from .._model import Model, applies, problems, resolve, section_label
from .._point import Point

_ROUND_TRIP_SAMPLES = (-7.5, -1.0, 0.0, 0.5, 1.0, 7.0, 60.0, 100.0, 1000.0)
"""Values a transform must bring back unchanged: both signs, a fraction, and the sizes that
unit conversions work with."""


def assert_models_valid(models: Sequence[Model], *, identities: Sequence[Identity]) -> None:
    """Resolve every model against every identity at every level, and check what resolving does
    not.

    Raises:
        AssertionError: listing every problem found - a model given twice, a model that does not
            resolve, a transform that does not round-trip, or a section no identity included,
            which is left untested.
        ValueError: `identities` is empty.
    """
    if not identities:
        raise ValueError("assert_models_valid needs at least one identity to check models against")

    findings: list[str] = []
    for index, model in enumerate(models):
        if any(other is model for other in models[:index]):
            findings.append(f"{model.name}: given twice")
            continue
        findings.extend(_model_findings(model, identities))

    if findings:
        raise AssertionError("\n".join(dict.fromkeys(findings)))


def _model_findings(model: Model, identities: Sequence[Identity]) -> list[str]:
    """Every problem `model` has, against every identity."""
    findings: list[str] = []
    included = [False] * len(model.sections)
    for identity in identities:
        for index, section in enumerate(model.sections):
            label = section_label(index, section)
            included[index] = included[index] or applies(section, identity, label, [])
        for level in Level:
            prefix = f"{model.name} / {identity} / {level}"
            found = problems(model, identity, level)
            findings.extend(f"{prefix}: {problem}" for problem in found)
            if not found:
                points = resolve(model, identity, level).points.values()
                findings.extend(f"{prefix}: {issue}" for issue in map(_transform_problem, points) if issue)
    for index, section in enumerate(model.sections):
        if section.when is not None and not included[index]:
            findings.append(f"{model.name}: {section_label(index, section)} never resolved - untested")
    return findings


def _transform_problem(point: Point[Any]) -> str | None:
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
