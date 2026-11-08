"""Checks a device repository runs over its models and their profiles: every variant, and every
transform."""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .._device import Identity
from .._model import Model, Profile, applies, problems, resolve, section_label
from .._point import Point

_ROUND_TRIP_SAMPLES = (-7.5, -1.0, 0.0, 0.5, 1.0, 7.0, 60.0, 100.0, 1000.0)
"""Values a transform must bring back unchanged: both signs, a fraction, and the sizes that
unit conversions work with."""


def assert_models_valid(models: Sequence[Model], profiles: Sequence[Profile], *,
                        identities: Sequence[Identity]) -> None:
    """Resolve the profile of every model against every identity, and check what resolving does not.

    Raises:
        AssertionError: listing every problem found - a model without a profile, a profile of a
            model not in `models`, two profiles of one model, a model that does not resolve, a
            transform that does not round-trip, or a section no identity included, which is left
            untested.
        ValueError: `identities` is empty.
    """
    if not identities:
        raise ValueError("assert_models_valid needs at least one identity to check models against")

    findings: list[str] = []
    for profile in profiles:
        if not any(model is profile.model for model in models):
            findings.append(f"a profile of {profile.model.name}, which is not among the models")
    for model in models:
        of_model = [profile for profile in profiles if profile.model is model]
        if not of_model:
            findings.append(f"{model.name}: no profile")
        elif len(of_model) > 1:
            findings.append(f"{model.name}: {len(of_model)} profiles, where a device takes one")
        for profile in of_model[:1]:
            findings.extend(_profile_findings(profile, identities))

    if findings:
        raise AssertionError("\n".join(dict.fromkeys(findings)))


def _profile_findings(profile: Profile, identities: Sequence[Identity]) -> list[str]:
    """Every problem the model of `profile` has, against every identity."""
    model = profile.model
    findings: list[str] = []
    included = [False] * len(model.sections)
    for identity in identities:
        prefix = f"{model.name} / {identity}"
        found = problems(profile, identity)
        findings.extend(f"{prefix}: {problem}" for problem in found)
        for index, section in enumerate(model.sections):
            label = section_label(index, section)
            included[index] = included[index] or applies(section, identity, label, [])
        if not found:
            points = resolve(profile, identity).points.values()
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
